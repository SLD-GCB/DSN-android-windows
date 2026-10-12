"""传输引擎：并行分块发送 + 分块接收 + 断点续传。

协议见 protocol/PROTOCOL.md §6/§7/§8/§9：
- 控制连接上跑 offer/accept/complete/done 等 JSON；
- 每条数据连接首帧 DATA_HELLO 绑定 (transfer, file)，随后只发 CHUNK；
- 接收端先写 .part（稀疏文件 + 随机写），收满后 fsync、校验 sha256、原子改名；
- 续传：accept 携带 received_ranges，发送端跳过已收区间。
"""
from __future__ import annotations

import hashlib
import os
import socket
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..config import Config
from ..events import EventBus, EventType
from ..models import FileEntry, Peer, Transfer, TransferState
from ..protocol import messages
from ..protocol.constants import (
    CHUNK_SIZE,
    CONNECT_TIMEOUT,
    DEFAULT_PARALLEL,
    DEFAULT_WINDOW_BYTES,
    MAX_PARALLEL,
)
from ..protocol.frame import (
    FrameType,
    ProtocolError,
    pack_chunk,
    pack_data_done,
    pack_data_error,
    pack_data_hello,
    parse_chunk,
    parse_data_error,
    parse_data_hello,
    read_frame,
    write_frame,
)
from ..utils import (
    complement_ranges,
    guess_kind,
    merge_ranges,
    sanitize_rel_path,
    total_range_len,
)
from . import resume as resumemod

ByteRange = Tuple[int, int]

_SHA_BUF = 1 << 20
#: 发送前等待对端地址出现的宽限时间（连接刚建立/正在替换时可能短暂查不到）
_PEER_WAIT = 5.0
#: 发 offer 时等控制连接回来的时间（文字有待发队列，文件要有同等耐心）
_OFFER_WAIT = 20.0
_META_SAVE_INTERVAL = 0.5
_PROGRESS_INTERVAL = 0.3


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(_SHA_BUF), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- 内部结构
@dataclass
class _SendFile:
    entry: FileEntry
    path: Path
    remaining: List[ByteRange] = field(default_factory=list)
    sent: int = 0
    completed: bool = False


@dataclass
class _Send:
    transfer_id: str
    peer_id: str
    files: List[_SendFile]
    accepted: bool = False
    failed: bool = False


@dataclass
class _RecvFile:
    entry: FileEntry
    part_path: Path
    final_path: Path
    ranges: List[ByteRange] = field(default_factory=list)
    written: int = 0
    completed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    _last_meta: float = 0.0
    #: 还开着的写句柄数。落盘前必须等它归零：Windows 上句柄没释放，
    #: os.replace 会直接报 WinError 32（另一个程序正在使用此文件）。
    writers: int = 0


@dataclass
class _Recv:
    transfer_id: str
    peer_id: str
    files: Dict[str, _RecvFile]
    total: int = 0


class TransferManager:
    def __init__(
        self,
        cfg: Config,
        bus: EventBus,
        transport,
        resolve_peer: Callable[[str], Optional[Peer]],
        auto_accept: bool = True,
        tunnel_provider=None,
        on_need_session=None,
    ) -> None:
        self.cfg = cfg
        self.bus = bus
        self.transport = transport
        self.resolve_peer = resolve_peer
        self.auto_accept = auto_accept
        self._tunnel_provider = tunnel_provider
        #: 发送需要控制连接时回调（让引擎立刻重连，而不是等下一次退避）
        self.on_need_session = on_need_session

        self._sends: Dict[str, _Send] = {}
        self._recvs: Dict[str, _Recv] = {}
        self._ui: "OrderedDict[str, Transfer]" = OrderedDict()   # file_id -> Transfer
        self._sha_cache: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._progress_at: Dict[str, float] = {}
        self._progress_pct_recv: Dict[str, int] = {}
        self._stop = threading.Event()

        self._send_locks: Dict[str, threading.Lock] = {}

        # 接收目标分配：多台设备并发发同名文件时，必须保证每个任务拿到**独占**的
        # final/part，否则两把不同的 rf.lock 会去写同一个 .part，内容直接损坏。
        self._reserve_lock = threading.RLock()   # 用 RLock：万一以后出现同线程嵌套也不会自锁
        self._claimed_parts: set = set()      # 已被在途任务占用的 .part
        self._claimed_finals: set = set()     # 已被在途任务预定的最终路径

    # ------------------------------------------------------------- 生命周期
    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------- 发送
    def send_paths(self, peer_id: str, paths: List[Path]) -> Optional[str]:
        entries: List[_SendFile] = []
        for path, root_name, rel_path in _expand(paths):
            try:
                entry = self._make_entry(path, root_name, rel_path)
            except OSError:
                continue
            entries.append(_SendFile(entry=entry, path=path))
        if not entries:
            return None

        transfer_id = str(uuid.uuid4())
        send = _Send(transfer_id=transfer_id, peer_id=peer_id, files=entries)
        with self._lock:
            self._sends[transfer_id] = send
            self._send_locks[transfer_id] = threading.Lock()
            for sf in entries:
                self._publish(send_file_transfer(send, sf, "send", TransferState.PENDING))
        # 放后台线程：发 offer 可能要等控制连接回来（最多 20 秒），不能卡住调用方
        # （Windows 端 send_files 是从 UI 线程调的）。
        threading.Thread(target=self._send_offer_with_retry, args=(send,), daemon=True).start()
        return transfer_id

    def _make_entry(self, path: Path, root_name: Optional[str], rel_path: str) -> FileEntry:
        st = path.stat()
        cached = self._sha_cache.get(self._cache_key(path, st))
        digest = cached or sha256_file(path)
        self._sha_cache[self._cache_key(path, st)] = digest
        size = st.st_size
        chunk = CHUNK_SIZE
        total_chunks = max(1, (size + chunk - 1) // chunk)
        return FileEntry(
            file_id=str(uuid.uuid4()),
            name=path.name,
            rel_path=rel_path,
            root_name=root_name,
            size=size,
            kind=guess_kind(path.name),
            content_id=digest,
            chunk_size=chunk,
            total_chunks=total_chunks,
            preview=None,
            mtime=st.st_mtime,
        )

    @staticmethod
    def _cache_key(path: Path, st) -> str:
        return f"{path}|{st.st_mtime_ns}|{st.st_size}"

    def _send_offer(self, send: _Send) -> bool:
        env = messages.envelope(
            "offer", self.cfg.device_id, send.peer_id,
            {"transfer_id": send.transfer_id, "files": [sf.entry.to_dict() for sf in send.files]},
        )
        return self.transport.send_to(send.peer_id, env)

    def _send_offer_with_retry(self, send: _Send) -> None:
        """发 offer：控制连接一时不可用就**等它回来**，别当场判死。

        文字消息有「待发队列」兜着，所以热点下会话短暂断开也能补发成功；
        文件以前是发 offer 失败就立刻 FAILED —— 于是「文字能过、文件过不去」。
        这里给文件同样的待遇：等 [OFFER_WAIT] 秒（并催一次重连），到点还没连上才算失败。
        """
        if self.on_need_session is not None:
            try:
                self.on_need_session(send.peer_id)
            except Exception:
                pass
        deadline = time.time() + _OFFER_WAIT
        waiting = False
        while not self._stop.is_set() and not send.failed:
            if self._send_offer(send):
                return
            if not waiting:
                waiting = True
                with self._lock:
                    for sf in send.files:
                        self._publish(send_file_transfer(
                            send, sf, "send", TransferState.PENDING, error="等待控制连接…"))
            if time.time() >= deadline:
                break
            # 每轮都催一下重连，恢复得快一些
            if self.on_need_session is not None:
                try:
                    self.on_need_session(send.peer_id)
                except Exception:
                    pass
            time.sleep(0.5)
        with self._lock:
            for sf in send.files:
                self._publish(send_file_transfer(
                    send, sf, "send", TransferState.FAILED,
                    error=f"控制连接不可用（已等 {int(_OFFER_WAIT)} 秒）"))
        send.failed = True

    def _on_accept(self, sess, env: dict) -> None:
        data = env.get("data") or {}
        tid = data.get("transfer_id")
        with self._lock:
            send = self._sends.get(tid)
        if send is None:
            return
        received_map = {}
        for item in data.get("files") or []:
            if isinstance(item, dict) and item.get("accepted", True):
                received_map[item.get("file_id")] = item.get("received_ranges") or []
        send.accepted = True
        for sf in send.files:
            ranges = merge_ranges(_as_ranges(received_map.get(sf.entry.file_id, [])))
            sf.remaining = complement_ranges(sf.entry.size, ranges)
            sf.sent = sf.entry.size - total_range_len(sf.remaining)
            with self._lock:
                self._publish(send_file_transfer(send, sf, "send", TransferState.ACTIVE))
        threading.Thread(target=self._run_send, args=(send,), daemon=True).start()

    def _run_send(self, send: _Send) -> None:
        peer = self.resolve_peer(send.peer_id)
        if peer is None:
            # 刚连上/正在替换连接时可能短暂查不到地址：给一小段宽限再判死，
            # 否则「多设备同时连」时一次抖动就让整次发送失败。
            deadline = time.time() + _PEER_WAIT
            while peer is None and time.time() < deadline and not self._stop.is_set():
                time.sleep(0.25)
                peer = self.resolve_peer(send.peer_id)
        if peer is None:
            with self._lock:
                for sf in send.files:
                    self._publish(send_file_transfer(send, sf, "send", TransferState.FAILED, error="对端不可达"))
            return
        # 最多重试 3 轮：单个数据连接失败留下的残余块由后续轮次重发
        for _round in range(3):
            threads: List[threading.Thread] = []
            for sf in send.files:
                if not sf.remaining or sf.completed:
                    continue
                slots = max(1, min(DEFAULT_PARALLEL, _count_pieces(sf.remaining, sf.entry.chunk_size)))
                for _ in range(slots):
                    t = threading.Thread(target=self._send_worker, args=(send, sf, peer), daemon=True)
                    t.start()
                    threads.append(t)
            for t in threads:
                t.join()
            if all(not sf.remaining for sf in send.files):
                break
        # 全部块已发完，对每个文件发 complete
        for sf in send.files:
            if not sf.remaining and not sf.completed:
                sf.completed = True
                self.transport.send_to(send.peer_id, messages.envelope(
                    "complete", self.cfg.device_id, send.peer_id,
                    {"transfer_id": send.transfer_id, "file_id": sf.entry.file_id},
                ))
            elif sf.remaining:
                with self._lock:
                    self._publish(send_file_transfer(send, sf, "send", TransferState.FAILED, error="部分块发送失败"))

    def _current_tunnel(self):
        if self._tunnel_provider is None:
            return None
        try:
            return self._tunnel_provider()
        except Exception:
            return None

    def _open_data_socket(self, ip: str, port: int) -> socket.socket:
        """开一条数据连接；口令非空时包 TLS + 握手。"""
        sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        tunnel = self._current_tunnel()
        if tunnel is not None and getattr(tunnel, "enabled", False):
            sock = tunnel.connect(sock, ip)
        return sock

    def _send_worker(self, send: _Send, sf: _SendFile, peer: Peer) -> None:
        sock: Optional[socket.socket] = None
        try:
            sock = self._open_data_socket(peer.ip, peer.tcp_port)
            write_frame(
                sock, FrameType.DATA_HELLO,
                pack_data_hello(self.cfg.device_id, send.transfer_id, sf.entry.file_id),
            )
            with open(sf.path, "rb") as f:
                while not self._stop.is_set():
                    piece = self._take_piece(send, sf)
                    if piece is None:
                        break
                    offset, length = piece
                    f.seek(offset)
                    data = f.read(length)
                    if not data:
                        break
                    write_frame(sock, FrameType.CHUNK, pack_chunk(offset, data))
                    with self._lock:
                        sf.sent += len(data)
                    self._maybe_progress(send, sf)
            digest = bytes.fromhex(sf.entry.content_id)
            write_frame(sock, FrameType.DATA_DONE, pack_data_done(sf.entry.size, digest))
        except (OSError, ProtocolError):
            with self._lock:
                self._publish(send_file_transfer(send, sf, "send", TransferState.PAUSED, error="连接中断"))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _take_piece(self, send: _Send, sf: _SendFile) -> Optional[Tuple[int, int]]:
        lock = self._send_locks.get(send.transfer_id)
        if lock is None:
            return None
        with lock:
            if not sf.remaining:
                return None
            s, e = sf.remaining[0]
            length = min(sf.entry.chunk_size, e - s + 1)
            if s + length > e:
                sf.remaining.pop(0)
            else:
                sf.remaining[0] = (s + length, e)
            return s, length

    # ------------------------------------------------------------- 接收
    @staticmethod
    def _candidate(parent: Path, stem: str, suffix: str, i: int) -> Path:
        """第 i 个候选名：i=0 原名，往后 `名字 (1).jpg`、`名字 (2).jpg`…"""
        return parent / (f"{stem} ({i}){suffix}" if i else f"{stem}{suffix}")

    def _reserve_target(self, rel: str, entry: FileEntry) -> Tuple[Path, Path, bool]:
        """原子地分配这次接收的 (final, part, 是否续传)。

        为什么不能只查 final：final 要到传完才出现，传输期间只有 .part。
        两台设备同时发 `IMG.jpg` 时，两个任务都会看到 final 不存在 → 选中同一个
        final / 同一个 .part，而它们各自有独立的 rf.lock —— 于是两把锁写同一个文件。
        这里在锁内选名，并用 O_EXCL 独占创建 .part 占位：
        - 占位失败（别人先抢到）→ 换下一个候选名；
        - .part 已存在但 `.partmeta` 的 content_id/size 与本次吻合 → 认领它（续传）；
        - .part 已存在且不吻合 → 换下一个候选名（别去糟蹋别人的在途文件）。
        """
        p = self.cfg.receive_dir / rel
        parent, stem, suffix = p.parent, p.stem, p.suffix
        with self._reserve_lock:
            i = 0
            while True:
                final = self._candidate(parent, stem, suffix, i)
                part = final.with_suffix(final.suffix + ".part")
                free_final = not final.exists() and final not in self._claimed_finals
                claimed = part in self._claimed_parts
                if free_final and not claimed:
                    if not part.exists():
                        try:
                            parent.mkdir(parents=True, exist_ok=True)
                            with open(part, "xb"):      # O_EXCL：并发时只有一个能成功
                                pass
                            self._claimed_parts.add(part)
                            self._claimed_finals.add(final)
                            return final, part, False
                        except FileExistsError:
                            pass                        # 输给了别人 → 换个名字
                    else:
                        meta = resumemod.load(part)
                        if (meta and meta.sha256 == entry.content_id
                                and meta.size == entry.size):
                            self._claimed_parts.add(part)
                            self._claimed_finals.add(final)
                            return final, part, True
                i += 1
                if i > 9999:
                    raise OSError(f"无法为 {rel} 分配唯一文件名")

    def _unique_final_locked(self, path: Path) -> Path:
        """落盘时再取一次唯一名（同样要避开别的在途任务预定的名字）。调用方需持有 _reserve_lock。"""
        parent, stem, suffix = path.parent, path.stem, path.suffix
        i = 0
        while True:
            cand = self._candidate(parent, stem, suffix, i)
            if not cand.exists() and cand not in self._claimed_finals:
                return cand
            i += 1
            if i > 9999:
                raise OSError(f"无法为 {path} 分配唯一文件名")

    def _release_recv(self, recv: "_Recv") -> None:
        """释放这批接收占用的名字（失败/取消/完成后调用），否则重试时无法续传。"""
        with self._reserve_lock:
            for rf in recv.files.values():
                self._claimed_parts.discard(rf.part_path)
                self._claimed_finals.discard(rf.final_path)

    def _on_offer(self, sess, env: dict) -> None:
        data = env.get("data") or {}
        tid = data.get("transfer_id")
        files = data.get("files") or []
        if not tid or not files:
            return
        recv = _Recv(transfer_id=tid, peer_id=sess.peer_id, files={})
        accept_files = []
        for fd in files:
            if not isinstance(fd, dict):
                continue
            entry = _entry_from_dict(fd)
            rel = sanitize_rel_path(entry.rel_path or entry.name)
            try:
                final, part, resumed = self._reserve_target(rel, entry)
            except OSError:
                continue

            ranges: List[ByteRange] = []
            if resumed:
                meta = resumemod.load(part)
                if meta and meta.sha256 == entry.content_id and meta.size == entry.size:
                    ranges = merge_ranges(meta.received)
            if not part.exists() or part.stat().st_size != entry.size:
                try:
                    with open(part, "r+b") as f:
                        f.truncate(entry.size)
                except OSError:
                    pass
                ranges = []
            rf = _RecvFile(
                entry=entry, part_path=part, final_path=final,
                ranges=ranges, written=total_range_len(ranges),
            )
            recv.files[entry.file_id] = rf
            recv.total += entry.size
            accept_files.append({
                "file_id": entry.file_id,
                "accepted": True,
                "resume": bool(ranges),
                "received_ranges": [[s, e] for s, e in ranges],
            })
            self._publish(send_file_transfer_recv(
                recv, rf, TransferState.ACTIVE if ranges else TransferState.PENDING))
        with self._lock:
            self._recvs[tid] = recv
        sess.send(messages.envelope("accept", self.cfg.device_id, sess.peer_id, {
            "transfer_id": tid,
            "files": accept_files,
            "max_parallel": DEFAULT_PARALLEL,
            "window_bytes": DEFAULT_WINDOW_BYTES,
        }))

    def handle_data_connection(self, sock: socket.socket, ftype: int, body: bytes, addr: tuple) -> None:
        if ftype != FrameType.DATA_HELLO:
            sock.close()
            return
        try:
            dev, tid, fid = parse_data_hello(body)
        except ProtocolError:
            sock.close()
            return
        with self._lock:
            recv = self._recvs.get(tid)
            rf = recv.files.get(fid) if recv else None
        if recv is None or rf is None:
            sock.close()
            return
        # 数据连接必须来自这笔传输的对端：多设备并发时，光靠 (transfer_id, file_id)
        # 不够——UUID 之外再加一道 device_id 校验，防止串流/注入。
        if dev and recv.peer_id and dev != recv.peer_id:
            sock.close()
            return
        threading.Thread(target=self._recv_worker, args=(sock, recv, rf), daemon=True).start()

    def _recv_worker(self, sock: socket.socket, recv: _Recv, rf: _RecvFile) -> None:
        f = None
        with rf.lock:
            rf.writers += 1
        try:
            f = open(rf.part_path, "r+b")
            while not self._stop.is_set():
                frame = read_frame(sock)
                if frame is None:
                    break
                ft, b = frame
                if ft == FrameType.CHUNK:
                    offset, payload = parse_chunk(b)
                    if not payload:
                        continue
                    with rf.lock:
                        f.seek(offset)
                        f.write(payload)
                        rf.ranges = merge_ranges(rf.ranges + [(offset, offset + len(payload) - 1)])
                        rf.written = total_range_len(rf.ranges)
                        self._save_meta(rf)
                    # 进度节流 + **不在锁里发布**：接收侧原本每 64 KiB 就发一次事件，
                    # 而事件会同步写一次 SQLite；多设备并发时会全部挤在全局锁上。
                    self._maybe_progress_recv(recv, rf)
                elif ft == FrameType.DATA_DONE:
                    break
                elif ft == FrameType.DATA_ERROR:
                    break
            if f is not None:
                f.flush()
                os.fsync(f.fileno())
        except (OSError, ProtocolError):
            pass
        finally:
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
            with rf.lock:
                rf.writers -= 1          # 句柄已释放，落盘那边可以继续了
            try:
                sock.close()
            except OSError:
                pass

    def _save_meta(self, rf: _RecvFile) -> None:
        now = time.time()
        if now - rf._last_meta < _META_SAVE_INTERVAL:
            return
        rf._last_meta = now
        resumemod.save(rf.part_path, resumemod.PartMeta(
            transfer_id="", file_id=rf.entry.file_id, name=rf.entry.name,
            size=rf.entry.size, sha256=rf.entry.content_id,
            chunk_size=rf.entry.chunk_size, total_chunks=rf.entry.total_chunks,
            received=list(rf.ranges),
        ))

    def _on_complete(self, sess, env: dict) -> None:
        data = env.get("data") or {}
        tid = data.get("transfer_id")
        fid = data.get("file_id")
        with self._lock:
            recv = self._recvs.get(tid)
            rf = recv.files.get(fid) if recv else None
        if recv is None or rf is None or rf.completed:
            return
        # 控制连接上的 complete 与数据连接上的字节流无顺序保证：
        # 可能在数据尚未写完、甚至数据连接尚未登记时就到达，因此另起线程等待收齐再校验。
        threading.Thread(
            target=self._finalize_recv, args=(sess, recv, rf, tid, fid), daemon=True
        ).start()

    def _finalize_recv(self, sess, recv: _Recv, rf: _RecvFile, tid: str, fid: str) -> None:
        deadline = time.time() + 120.0
        while time.time() < deadline and rf.written < rf.entry.size and not self._stop.is_set():
            time.sleep(0.05)
        if rf.completed:
            return
        if rf.written < rf.entry.size:
            with self._lock:
                self._publish(send_file_transfer_recv(
                    recv, rf, TransferState.PAUSED,
                    error=f"未收齐（{rf.written}/{rf.entry.size}）"))
            return
        with rf.lock:
            resumemod.save(rf.part_path, resumemod.PartMeta(
                transfer_id=tid, file_id=fid, name=rf.entry.name, size=rf.entry.size,
                sha256=rf.entry.content_id, chunk_size=rf.entry.chunk_size,
                total_chunks=rf.entry.total_chunks, received=list(rf.ranges),
            ))
        # 落盘前先等这个文件的所有数据连接把手柄关掉。
        # Windows 上句柄没释放时 os.replace 会报 WinError 32（另一个程序正在使用此文件），
        # 文件就永远落不下来 —— 现象正是「文件传不过去、但文字消息正常」。
        # 多路并行（每文件 3 条连接）时最后收尾的那个 worker 还没 close，就会撞上。
        drain_deadline = time.time() + 10.0
        while rf.writers > 0 and time.time() < drain_deadline:
            time.sleep(0.02)
        actual = sha256_file(rf.part_path)
        if actual != rf.entry.content_id:
            with self._lock:
                self._publish(send_file_transfer_recv(recv, rf, TransferState.FAILED, error="sha256 校验失败"))
            sess.send(messages.envelope("error", self.cfg.device_id, recv.peer_id, {
                "transfer_id": tid, "file_id": fid, "code": 1, "message": "checksum mismatch",
            }))
            return
        # 选最终名 + 改名放在 reserve 锁里：两个任务同时传完时也不会抢到同一个名字。
        # 注意先把「自己的预定」放开，否则 _unique_final_locked 会把我们本来就想要的
        # 名字当成别人的占用，导致每个文件都被改成 `名字 (1).ext`。
        # ⚠ 锁序：这里只拿 _reserve_lock，**不能**在持有它的时候去 _publish（那会拿 _lock），
        # 否则和「持 _lock 调 _release_recv（拿 _reserve_lock）」的路径形成 ABBA 死锁。
        old_final = rf.final_path
        err = None
        with self._reserve_lock:
            self._claimed_finals.discard(old_final)
            final = self._unique_final_locked(old_final)
            self._claimed_finals.add(final)      # 占住这个名字，避免同时传完的两个任务互抢
            try:
                os.replace(rf.part_path, final)
            except OSError as exc:
                err = str(exc)
            self._claimed_parts.discard(rf.part_path)
            self._claimed_finals.discard(final)  # 已经落盘，后续靠文件存在性判断即可
        if err is not None:
            self._publish(send_file_transfer_recv(recv, rf, TransferState.FAILED, error=err))
            return
        resumemod.clear(rf.part_path)
        rf.completed = True
        rf.final_path = final
        self._publish(send_file_transfer_recv(recv, rf, TransferState.COMPLETED))
        sess.send(messages.envelope("done", self.cfg.device_id, recv.peer_id, {
            "transfer_id": tid, "file_id": fid,
        }))
        if all(r.completed for r in recv.files.values()):
            with self._lock:
                self._recvs.pop(tid, None)

    def _on_done(self, sess, env: dict) -> None:
        data = env.get("data") or {}
        tid = data.get("transfer_id")
        fid = data.get("file_id")
        with self._lock:
            send = self._sends.get(tid)
            if send is None:
                return
            sf = next((s for s in send.files if s.entry.file_id == fid), None)
            if sf is None:
                return
            self._publish(send_file_transfer(send, sf, "send", TransferState.COMPLETED))
            if all(s.completed for s in send.files):
                self._sends.pop(tid, None)
                self._send_locks.pop(tid, None)

    def _on_reject(self, sess, env: dict) -> None:
        data = env.get("data") or {}
        tid = data.get("transfer_id")
        reason = data.get("reason", "")
        with self._lock:
            send = self._sends.get(tid)
            if send is None:
                return
            for sf in send.files:
                self._publish(send_file_transfer(send, sf, "send", TransferState.REJECTED, error=reason))
            self._sends.pop(tid, None)

    def _on_cancel(self, sess, env: dict) -> None:
        data = env.get("data") or {}
        tid = data.get("transfer_id")
        with self._lock:
            recv = self._recvs.pop(tid, None)
            if recv is not None:
                for rf in recv.files.values():
                    self._publish(send_file_transfer_recv(recv, rf, TransferState.CANCELLED))
            send = self._sends.pop(tid, None)
            if send is not None:
                for sf in send.files:
                    self._publish(send_file_transfer(send, sf, "send", TransferState.CANCELLED))
        # 锁外释放：_release_recv 要拿 _reserve_lock，持 _lock 去拿会和 _on_complete 形成 ABBA
        if recv is not None:
            self._release_recv(recv)

    def cancel(self, transfer_id: str) -> None:
        sess = None
        with self._lock:
            send = self._sends.get(transfer_id)
            recv = self._recvs.get(transfer_id)
            peer_id = send.peer_id if send else (recv.peer_id if recv else None)
        if peer_id:
            sess = self.transport.get_session(peer_id)
        if sess is not None:
            sess.send(messages.envelope("cancel", self.cfg.device_id, peer_id, {"transfer_id": transfer_id}))
        with self._lock:
            if send is not None:
                for sf in send.files:
                    self._publish(send_file_transfer(send, sf, "send", TransferState.CANCELLED))
                self._sends.pop(transfer_id, None)
            if recv is not None:
                for rf in recv.files.values():
                    self._publish(send_file_transfer_recv(recv, rf, TransferState.CANCELLED))
                self._recvs.pop(transfer_id, None)
        if recv is not None:
            self._release_recv(recv)

    # ------------------------------------------------------------- 控制派发
    def handle_control(self, sess, env: dict) -> None:
        mtype = env.get("type")
        if mtype == "offer":
            self._on_offer(sess, env)
        elif mtype == "accept":
            self._on_accept(sess, env)
        elif mtype == "reject":
            self._on_reject(sess, env)
        elif mtype == "complete":
            self._on_complete(sess, env)
        elif mtype == "done":
            self._on_done(sess, env)
        elif mtype == "cancel":
            self._on_cancel(sess, env)
        elif mtype in ("resume_ack", "window_update", "chunk_ack", "progress"):
            return  # v2 预留，接收端据此可做窗口控制；当前忽略
        elif mtype == "error":
            self._on_error(env)

    def _on_error(self, env: dict) -> None:
        data = env.get("data") or {}
        tid = data.get("transfer_id")
        msg = data.get("message", "error")
        with self._lock:
            send = self._sends.get(tid)
            if send is not None:
                for sf in send.files:
                    self._publish(send_file_transfer(send, sf, "send", TransferState.FAILED, error=msg))
            recv = self._recvs.get(tid)
            if recv is not None:
                for rf in recv.files.values():
                    self._publish(send_file_transfer_recv(recv, rf, TransferState.FAILED, error=msg))
        if recv is not None:
            self._release_recv(recv)

    # ------------------------------------------------------------- 事件发布
    def _publish(self, transfer: Optional[Transfer]) -> None:
        if transfer is None:
            return
        fid = transfer.file.file_id if transfer.file else transfer.transfer_id
        with self._lock:
            is_new = fid not in self._ui
            self._ui[fid] = transfer
            while len(self._ui) > 500:
                self._ui.popitem(last=False)
        # bus.publish 是同步的，订阅方会写 SQLite —— 一定放在锁外发
        self.bus.publish(EventType.TRANSFER_ADDED if is_new else EventType.TRANSFER_UPDATED, transfer)

    def _maybe_progress(self, send: _Send, sf: _SendFile) -> None:
        key = f"{send.transfer_id}:{sf.entry.file_id}:s"
        now = time.time()
        if now - self._progress_at.get(key, 0.0) < _PROGRESS_INTERVAL:
            return
        self._progress_at[key] = now
        self._publish(send_file_transfer(send, sf, "send", TransferState.ACTIVE))

    def _maybe_progress_recv(self, recv: "_Recv", rf: "_RecvFile") -> None:
        """接收侧进度节流：≥ [PROGRESS_INTERVAL] 且**整数百分比变化**才发一次事件。

        接收侧是「每 64 KiB 一块」，不做节流会变成每秒上千次事件 + 上千次 SQLite 写入，
        多设备并发时直接把 IO 和锁打满。
        """
        key = f"{recv.transfer_id}:{rf.entry.file_id}:r"
        now = time.time()
        total = rf.entry.size or 0
        pct = int(100 * rf.written / total) if total else 0
        if self._progress_pct_recv.get(key) == pct and now - self._progress_at.get(key, 0.0) < _PROGRESS_INTERVAL:
            return
        if now - self._progress_at.get(key, 0.0) < _PROGRESS_INTERVAL:
            return
        self._progress_at[key] = now
        self._progress_pct_recv[key] = pct
        self._publish(send_file_transfer_recv(recv, rf, TransferState.ACTIVE))

    def transfers(self) -> List[Transfer]:
        with self._lock:
            return list(self._ui.values())


# ---------------------------------------------------------------- 辅助函数
def _expand(paths: List[Path]) -> List[Tuple[Path, Optional[str], str]]:
    """展开输入路径为 (文件路径, root_name, rel_path)。

    文件夹递归展开，rel_path = "顶层文件夹名/相对子路径"（保留目录层级）。
    """
    out: List[Tuple[Path, Optional[str], str]] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            root = p.name
            for child in sorted(p.rglob("*")):
                if child.is_file():
                    rel = f"{root}/{child.relative_to(p).as_posix()}"
                    out.append((child, root, rel))
        elif p.is_file():
            out.append((p, None, p.name))
    return out


def _count_pieces(ranges: List[ByteRange], chunk_size: int) -> int:
    total = total_range_len(ranges)
    return max(1, (total + chunk_size - 1) // chunk_size)


def _as_ranges(items) -> List[ByteRange]:
    out: List[ByteRange] = []
    for it in items or []:
        try:
            s, e = it
            out.append((int(s), int(e)))
        except (TypeError, ValueError):
            continue
    return out


def _entry_from_dict(fd: dict) -> FileEntry:
    return FileEntry(
        file_id=fd.get("file_id") or str(uuid.uuid4()),
        name=fd.get("name") or "unnamed",
        rel_path=fd.get("rel_path") or fd.get("name") or "unnamed",
        root_name=fd.get("root_name"),
        size=int(fd.get("size", 0)),
        kind=fd.get("kind", "file"),
        content_id=fd.get("content_id", ""),
        chunk_size=int(fd.get("chunk_size", CHUNK_SIZE)),
        total_chunks=int(fd.get("total_chunks", 0)),
        preview=fd.get("preview"),
        mtime=float(fd.get("mtime", 0.0)),
    )


def send_file_transfer(send: _Send, sf: _SendFile, direction: str, state: TransferState, error: Optional[str] = None) -> Transfer:
    return Transfer(
        transfer_id=send.transfer_id,
        peer_id=send.peer_id,
        file=sf.entry,
        direction=direction,
        state=state,
        sent=sf.sent,
        received=sf.sent,
        local_path=str(sf.path),      # UI 用来内联预览发出的图片
        error=error,
    )


def send_file_transfer_recv(recv: _Recv, rf: _RecvFile, state: TransferState, error: Optional[str] = None) -> Transfer:
    return Transfer(
        transfer_id=recv.transfer_id,
        peer_id=recv.peer_id,
        file=rf.entry,
        direction="recv",
        state=state,
        received=rf.written if state != TransferState.COMPLETED else rf.entry.size,
        received_ranges=list(rf.ranges),
        final_path=str(rf.final_path),
        error=error,
    )
