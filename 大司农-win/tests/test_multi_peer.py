"""多设备并发相关的回归测试。

覆盖三件在「多台设备同时传输」下会出事的地方：
1. 同名文件并发接收时必须各自拿到独占的 .part（否则两把锁写同一个文件 → 损坏）；
2. 数据连接必须来自这笔传输的对端（只靠 transfer_id/file_id 不够）；
3. 接收进度要节流（每 64 KiB 一次会打爆事件与 SQLite）。
"""
import hashlib
import threading
import time

from dasinong_core.config import Config
from dasinong_core.events import EventBus, EventType
from dasinong_core.models import FileEntry, TransferState
from dasinong_core.network import resume as resumemod
from dasinong_core.network import transfer as transfer_mod
from dasinong_core.network.transfer import TransferManager, _Recv, _RecvFile
from dasinong_core.protocol.frame import FrameType, pack_data_hello

SHA = "aa" * 32


def _tm(tmp_path) -> TransferManager:
    cfg = Config(udp_port=0, tcp_port=0, data_dir=tmp_path, receive_dir=tmp_path / "recv",
                 device_name="T", device_id="dev-test-0000")
    cfg.ensure_dirs()
    return TransferManager(cfg, EventBus(), transport=None, resolve_peer=lambda pid: None)


def _entry(name: str, size: int = 100) -> FileEntry:
    return FileEntry(file_id="f1", name=name, rel_path=name, size=size, content_id=SHA)


# --------------------------------------------------------------- 同名占位
def test_concurrent_same_name_offers_get_distinct_part_paths(tmp_path):
    """两台设备同时发 `IMG.jpg`：必须拿到不同的 part/final，绝不能共用。"""
    tm = _tm(tmp_path)
    results = []
    barrier = threading.Barrier(2)

    def reserve(n: int) -> None:
        barrier.wait()
        results.append(tm._reserve_target("IMG.jpg", _entry("IMG.jpg")))

    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    finals = {r[0] for r in results}
    parts = {r[1] for r in results}
    assert len(finals) == 2, f"最终名撞了：{finals}"
    assert len(parts) == 2, f".part 撞了：{parts}"
    assert not any(r[2] for r in results), "新建的占位不该被判成续传"
    # 两个占位文件都真实存在且互不覆盖
    for _, part, _ in results:
        assert part.exists()


def test_second_reserve_without_release_moves_to_next_name(tmp_path):
    """同一路径被在途任务占着时，第二个任务要自动改名（而不是抢同一个文件）。"""
    tm = _tm(tmp_path)
    final1, part1, _ = tm._reserve_target("a.bin", _entry("a.bin"))
    final2, part2, _ = tm._reserve_target("a.bin", _entry("a.bin"))
    assert final1.name == "a.bin" and final2.name == "a (1).bin"
    assert part1 != part2


def test_release_then_resume_claims_existing_part(tmp_path):
    """释放占用后，同一个 part 应该能被**续传**认领（而不是又新建一个）。"""
    tm = _tm(tmp_path)
    final, part, _ = tm._reserve_target("v.bin", _entry("v.bin"))
    recv = _Recv(transfer_id="t1", peer_id="p1", files={
        "f1": _RecvFile(entry=_entry("v.bin"), part_path=part, final_path=final),
    })
    tm._release_recv(recv)

    # 模拟上次中断留下的续传信息
    resumemod.save(part, resumemod.PartMeta(
        transfer_id="t1", file_id="f1", name="v.bin", size=100, sha256=SHA,
        chunk_size=1 << 16, total_chunks=1, received=[(0, 49)],
    ))
    final2, part2, resumed = tm._reserve_target("v.bin", _entry("v.bin"))
    assert resumed, "应该认领已有 part 走续传"
    assert part2 == part and final2 == final


def test_foreign_orphan_part_is_not_adopted(tmp_path):
    """别人的在途 .part（没有匹配的 meta）不该被我们抢用，应该换个名字。"""
    tm = _tm(tmp_path)
    tm.cfg.receive_dir.mkdir(parents=True, exist_ok=True)
    orphan = tm.cfg.receive_dir / "x.bin.part"
    orphan.write_bytes(b"someone else's in-flight data")

    final, part, resumed = tm._reserve_target("x.bin", _entry("x.bin"))
    assert not resumed
    assert part != orphan
    assert orphan.read_bytes() == b"someone else's in-flight data"   # 没被动过


# --------------------------------------------------------------- 对端校验
class _FakeSock:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _register_recv(tm: TransferManager, tmp_path, peer_id="peer-A") -> tuple:
    final = tmp_path / "recv" / "k.bin"
    part = final.with_suffix(final.suffix + ".part")
    recv = _Recv(transfer_id="tid-1", peer_id=peer_id, files={
        "fid-1": _RecvFile(entry=_entry("k.bin"), part_path=part, final_path=final),
    })
    tm._recvs["tid-1"] = recv
    return recv


def test_data_connection_from_wrong_peer_is_rejected(tmp_path, monkeypatch):
    tm = _tm(tmp_path)
    _register_recv(tm, tmp_path, peer_id="peer-A")
    started = []
    monkeypatch.setattr(tm, "_recv_worker", lambda *a: started.append(a))

    sock = _FakeSock()
    body = pack_data_hello("peer-B", "tid-1", "fid-1")      # 冒充别人
    tm.handle_data_connection(sock, FrameType.DATA_HELLO, body, ("192.168.2.99", 1))

    assert sock.closed, "对端不匹配必须关掉连接"
    assert not started, "不该为冒充的连接启动接收线程"


def test_data_connection_from_right_peer_is_accepted(tmp_path, monkeypatch):
    tm = _tm(tmp_path)
    _register_recv(tm, tmp_path, peer_id="peer-A")
    started = []
    monkeypatch.setattr(tm, "_recv_worker", lambda *a: started.append(a))

    sock = _FakeSock()
    body = pack_data_hello("peer-A", "tid-1", "fid-1")
    tm.handle_data_connection(sock, FrameType.DATA_HELLO, body, ("192.168.2.20", 1))

    assert not sock.closed
    assert len(started) == 1


# --------------------------------------------------------------- 进度节流
def test_receive_progress_is_throttled_and_not_held_in_lock(tmp_path):
    """接收侧每块都发事件会把 SQLite 打死；这里验证节流生效。"""
    tm = _tm(tmp_path)
    events = []
    tm.bus.subscribe(lambda e: events.append(e), EventType.TRANSFER_UPDATED)
    tm.bus.subscribe(lambda e: events.append(e), EventType.TRANSFER_ADDED)

    recv = _register_recv(tm, tmp_path)
    rf = recv.files["fid-1"]
    rf.entry = _entry("k.bin", size=1000)

    for written in range(1, 101):          # 模拟 100 次进度事件
        rf.written = written
        tm._maybe_progress_recv(recv, rf)

    assert len(events) == 1, f"1 秒内的密集进度应被节流成 1 次，实际 {len(events)} 次"
    assert events[0].data.state == TransferState.ACTIVE


# --------------------------------------------------------------- 落盘前等写句柄关闭
class _FakeSess:
    """_on_complete 只需要一个能 send 的会话。"""

    peer_id = "peer-A"

    def __init__(self) -> None:
        self.sent = []

    def send(self, env) -> None:
        self.sent.append(env)


def test_finalize_waits_for_open_write_handles(tmp_path, monkeypatch):
    """校验/改名之前必须等这个文件的写句柄全部关闭。

    回归点：多路并行接收时，最后收尾的那条数据连接可能还没 close 就去落盘 ——
    Windows 上 os.replace 会直接报「另一个程序正在使用此文件」(WinError 32)，
    文件永远落不下来；现象正是「文件传不过去、但文字消息正常」。
    """
    tm = _tm(tmp_path)
    payload = b"hello-world"
    content = hashlib.sha256(payload).hexdigest()
    final = tmp_path / "recv" / "k.bin"
    part = final.with_suffix(final.suffix + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(payload)

    entry = FileEntry(file_id="f1", name="k.bin", rel_path="k.bin",
                      size=len(payload), content_id=content)
    rf = _RecvFile(entry=entry, part_path=part, final_path=final)
    recv = _Recv(transfer_id="t1", peer_id="peer-A", files={"f1": rf})
    tm._recvs["t1"] = recv

    # 字节已经「收齐」了（written == size）——但数据连接可能还没关句柄，
    # 这正是原版逻辑的漏洞：只等字节数，不等句柄。
    rf.written = len(payload)
    rf.ranges = [(0, len(payload) - 1)]
    rf.writers = 1
    seen = []

    def fake_sha256(path):
        seen.append(rf.writers)      # 校验那一刻，写句柄必须已经归零
        return content

    monkeypatch.setattr(transfer_mod, "sha256_file", fake_sha256)

    def release_later() -> None:
        time.sleep(0.3)
        rf.writers = 0               # 那条连接收尾并关闭句柄

    threading.Thread(target=release_later, daemon=True).start()

    tm._on_complete(_FakeSess(), {"data": {"transfer_id": "t1", "file_id": "f1"}})

    deadline = time.time() + 5
    while time.time() < deadline and not final.exists():
        time.sleep(0.05)

    assert seen == [0], f"校验时写句柄应已归零，实际 {seen}"
    assert final.exists(), "文件应已落盘"
    assert final.read_bytes() == payload
    assert not part.exists(), ".part 应已被改名"
