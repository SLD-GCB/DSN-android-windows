"""NodeEngine：编排发现 / 网关中继 / 控制连接 / 传输，对外暴露高层 API。

UI 只与本类交互；事件通过 EventBus 发布。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from dataclasses import replace
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config, load_prefs, save_prefs
from .events import EventBus, EventType
from .models import ChatMessage, FileEntry, MsgKind, Peer, PeerState, Transfer, TransferState
from .network.tunnel import Tunnel
from .network.control import ControlSession, TransportManager
from .network.discovery import DiscoveryService
from .network.gateway_rendezvous import GatewayRendezvous
from .network.transfer import TransferManager
from .protocol import messages
from .protocol.constants import (
    PEER_TIMEOUT,
    PING_INTERVAL,
    RECONNECT_BACKOFF,
    RECONNECT_JITTER,
)

# 控制报文里与传输相关的类型（交给 transfer 引擎处理）
_TRANSFER_TYPES = {
    "offer", "accept", "reject", "resume_ack", "window_update",
    "chunk_ack", "progress", "complete", "done", "cancel", "error",
}

_DEDUP_MAX = 2048

# 非 canon 一方在发现对端后等待多久才兜底主动反连（应对单向防火墙/路由）
_FALLBACK_DELAY = 8.0

#: 记住对端地址的时长：发现表（UDP）把对端剪掉后，靠它继续发文件/重连
_ENDPOINT_TTL = 60.0

# 手动 IP 会话的临时 id 前缀（设备被发现后会被替换成真实 device_id）
_MANUAL_PREFIX = "ip:"


class NodeEngine:
    def __init__(self, cfg: Config, bus: Optional[EventBus] = None) -> None:
        self.cfg = cfg
        self.bus = bus or EventBus()

        # 隧道（口令）：环境变量优先，其次 SQLite 设置；空口令 = 不加密
        self.tunnel = Tunnel(self._passphrase_from_env())

        self.discovery = DiscoveryService(
            cfg, self.bus, gateway_enabled=True, tunnel_provider=lambda: self.tunnel
        )
        self.gateway = GatewayRendezvous(
            cfg, self.bus, self.discovery, enabled=True, tunnel_provider=lambda: self.tunnel
        )
        self.transport = TransportManager(
            cfg, self.bus, on_control=self._on_control, on_data=self._on_data,
            tunnel_provider=lambda: self.tunnel,
            on_learn_peer=self._learn_incoming_peer,
        )
        self.transfer = TransferManager(
            cfg, self.bus, self.transport, resolve_peer=self.get_peer,
            tunnel_provider=lambda: self.tunnel,
            on_need_session=self._kick_session,
        )
        self.store = None      # 由 attach_store 注入

        self._stop = threading.Event()
        self._maintenance_thread: Optional[threading.Thread] = None

        self._seen_text: "OrderedDict[str, bool]" = OrderedDict()
        self._backoff_until: Dict[str, float] = {}
        self._endpoints: Dict[str, tuple] = {}   # device_id -> (Peer, 最近见到的时间)
        # 记住「连上过」的对端地址（落盘）：手机开热点时 UDP 发现不到，
        # 下次启动可以直接拨回去，不必每次手输 IP。
        self._known_peers: Dict[str, dict] = self._load_known_peers()
        self._attempts: Dict[str, int] = {}
        self._no_session_since: Dict[str, float] = {}
        self._last_ping: Dict[str, float] = {}
        self._outbox: List[tuple] = []
        self._outbox_lock = threading.Lock()
        self._chat_notified: set = set()   # 已往会话里补过消息的接收文件
        self._state_lock = threading.RLock()
        self.start_errors: List[str] = []

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        # 逐个启动，任何一个失败都不影响其余子系统（否则会表现为
        # 「能发现设备但永远建不起 TCP 连接」）。
        self.start_errors = []
        steps = (
            ("发现服务", self.discovery.start),
            ("网关中继", self.gateway.start),
            ("TCP 传输", self.transport.start),
        )
        for label, fn in steps:
            try:
                fn()
            except Exception as exc:
                msg = f"{label}启动失败：{exc}"
                self.start_errors.append(msg)
                self.bus.publish(EventType.LOG, msg)
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop, name="node-maintenance", daemon=True
        )
        self._maintenance_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.transfer is not None:
            try:
                self.transfer.stop()
            except Exception:
                pass
        try:
            self.transport.stop()
        except Exception:
            pass
        try:
            self.gateway.stop()
        except Exception:
            pass
        try:
            self.discovery.stop()
        except Exception:
            pass

    # ------------------------------------------------------------- 查询
    def peers(self) -> List[Peer]:
        return self.discovery.snapshot()

    def get_peer(self, device_id: str) -> Optional[Peer]:
        """解析对端地址（数据连接就靠它）。

        顺序：发现表 → 最近见过的地址 → **活着的控制连接的远端地址**。
        最后那条是关键：手机开热点时发不出广播，发现表里永远没有它，
        那就用控制连接的远端 IP 兜底 —— 只要控制通道在，数据就能发出去。
        """
        peer = self.discovery.get(device_id)
        if peer is not None:
            self._remember_endpoint(peer)
            return peer
        cached = self._endpoints.get(device_id)
        sess = self.transport.get_session(device_id)
        if cached is not None and sess is not None:
            return cached[0]
        if sess is not None:
            live_ip = sess.remote_ip()
            if live_ip:
                name = cached[0].name if cached is not None else device_id[:8]
                tcp_port = cached[0].tcp_port if cached is not None else self.cfg.tcp_port
                peer = Peer(
                    device_id=device_id, name=name, ip=live_ip,
                    tcp_port=int(tcp_port or self.cfg.tcp_port),
                    udp_port=self.cfg.udp_port, state=PeerState.ONLINE,
                )
                self._remember_endpoint(peer)
                return peer
        return None

    def _remember_endpoint(self, peer: Peer, now: Optional[float] = None) -> None:
        """记住最近一次见到的对端地址（发现表被剪掉后的兜底）。"""
        self._endpoints[peer.device_id] = (peer, time.time() if now is None else now)

    # ------------------------------------------------------------- 已知设备记忆
    @staticmethod
    def _load_known_peers() -> Dict[str, dict]:
        try:
            raw = load_prefs().get("known_peers") or ""
            if not raw:
                return {}
            data = json.loads(raw)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("ip")}
        except Exception:
            pass
        return {}

    def _remember_peer(self, peer: Peer) -> None:
        """把「确实连上过」的对端地址落盘，供下次启动直连回去。"""
        entry = {
            "ip": peer.ip,
            "tcp_port": peer.tcp_port,
            "name": peer.name,
            "device_type": peer.device_type,
        }
        if self._known_peers.get(peer.device_id) == entry:
            return
        self._known_peers[peer.device_id] = entry
        while len(self._known_peers) > 50:
            self._known_peers.pop(next(iter(self._known_peers)), None)
        try:
            save_prefs(known_peers=json.dumps(self._known_peers, ensure_ascii=False))
        except Exception:
            pass

    def is_connected(self, device_id: str) -> bool:
        return self.transport.get_session(device_id) is not None

    def add_manual_ip(self, ip: str) -> None:
        self.discovery.add_manual_target(ip)
        self.gateway.add_candidate(ip)

    # ------------------------------------------------------------- 诊断
    def diagnose_peer(self, peer_id: str) -> str:
        """对某台设备做一次可读的连通性诊断，用于排查「能发现但发不出」。"""
        peer = self.get_peer(peer_id)
        if peer is None:
            return f"{peer_id[:8]}：不在发现列表中"
        parts = [f"{peer.name} @ {peer.ip}:{peer.tcp_port}"]
        parts.append("控制连接=已建立" if self.is_connected(peer_id) else "控制连接=未建立")

        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        try:
            s.connect((peer.ip, peer.tcp_port))
            parts.append("TCP 探测=成功（对方端口可直达）")
        except socket.timeout:
            parts.append("TCP 探测=超时（对方很可能拦了入站 TCP，多为防火墙）")
        except ConnectionRefusedError:
            parts.append("TCP 探测=被拒绝（对方端口没在监听：程序没运行或端口不一致）")
        except OSError as exc:
            parts.append(f"TCP 探测=失败（{exc}）")
        finally:
            try:
                s.close()
            except OSError:
                pass
        return "  ".join(parts)

    def diagnose(self) -> str:
        """整体网络诊断报告。"""
        from .network import interfaces as ifaces_mod

        lines: List[str] = []
        if self.start_errors:
            lines.append("启动异常：")
            for err in self.start_errors:
                lines.append("  ! " + err)
        lines.append(f"设备名：{self.cfg.device_name}")
        lines.append(f"设备 ID：{self.cfg.device_id}")
        lines.append(f"监听：UDP {self.cfg.udp_port} / TCP {self.cfg.tcp_port}"
                     f"（TCP 服务端{'运行中' if self.transport.is_listening else '未运行'}）")
        lines.append("本机网卡：")
        for i in ifaces_mod.list_ipv4_interfaces():
            lines.append(f"  - {i.name}  {i.ip}/{i.netmask}  广播 {i.broadcast}")
        gw = ifaces_mod.default_gateway()
        lines.append(f"默认网关：{gw or '未获取到'}")
        peers = self.peers()
        if not peers:
            lines.append("未发现任何设备（检查是否在同一网段/热点）。")
        else:
            lines.append(f"发现 {len(peers)} 台设备：")
            for p in peers:
                lines.append("  " + self.diagnose_peer(p.device_id))
        return "\n".join(lines)

    # ------------------------------------------------------------- 目录配置
    def set_receive_dir(self, path: str) -> Path:
        """改接收目录：**立即生效**（并写进固定位置的指针文件）。"""
        new = self.cfg.with_receive_dir(Path(path))
        self.cfg = new
        save_prefs(receive_dir=str(new.receive_dir))
        self.bus.publish(EventType.LOG, f"接收目录已改为：{new.receive_dir}")
        return new.receive_dir

    def set_data_dir(self, path: str) -> Path:
        """改数据目录：写进指针文件，**重启后生效**（数据库、身份等都在数据目录里）。"""
        target = Path(path).expanduser()
        target.mkdir(parents=True, exist_ok=True)
        save_prefs(data_dir=str(target))
        self.bus.publish(EventType.LOG, f"数据目录已设为：{target}（重启后生效）")
        return target

    # ------------------------------------------------------------- 隧道 / 口令
    @staticmethod
    def _passphrase_from_env() -> str:
        return os.environ.get("DASINONG_PASSPHRASE", "") or ""

    def passphrase(self) -> str:
        return self.tunnel.passphrase

    def fingerprint(self) -> str:
        """本机隧道公钥指纹（口令为空则空串）。供人工核对。"""
        return self.tunnel.fingerprint

    def is_encrypted(self) -> bool:
        return bool(self.tunnel.enabled)

    def set_passphrase(self, value: str) -> None:
        """设置/清除隧道口令：重建身份与 TLS 上下文，并断开旧会话用新口令重连。"""
        value = (value or "").strip()
        if value == self.tunnel.passphrase:
            return
        old = self.tunnel
        self.tunnel = Tunnel(value)
        old.close()
        if self.store is not None:
            try:
                self.store.setting_set("passphrase", value)
            except Exception:
                pass
        for sess in list(self.transport.sessions()):
            try:
                sess.close()
            except Exception:
                pass
        self.bus.publish(
            EventType.LOG,
            "隧道口令已更新，正在用新口令重连" if value else "已关闭加密（口令为空）",
        )

    # ------------------------------------------------------------- 持久化
    def attach_store(self, store) -> None:
        """挂载 SQLite 存储，并订阅传输事件自动落盘。"""
        self.store = store
        self.bus.subscribe(self._persist_transfer_event, EventType.TRANSFER_ADDED)
        self.bus.subscribe(self._persist_transfer_event, EventType.TRANSFER_UPDATED)
        # 收到的文件/图片完成后，往会话里补一条消息（否则接收端聊天里看不到）
        self.bus.subscribe(self._on_transfer_for_chat, EventType.TRANSFER_UPDATED)
        # 设备被发现后，把手动 IP 会话升级成真实 device_id
        self.bus.subscribe(self._on_peer_for_bind, EventType.PEER_ADDED)
        self.bus.subscribe(self._on_peer_for_bind, EventType.PEER_UPDATED)
        # 环境变量没给口令时，用上次保存的
        if not self._passphrase_from_env():
            try:
                saved = store.setting_get("passphrase", "") or ""
            except Exception:
                saved = ""
            if saved and saved != self.tunnel.passphrase:
                self.set_passphrase(saved)

    def _persist_transfer_event(self, event) -> None:
        if self.store is None or not isinstance(event.data, Transfer):
            return
        try:
            self.store.upsert_transfer(event.data)
        except Exception:
            pass

    def _on_transfer_for_chat(self, event) -> None:
        """接收完成 → 在会话里生成一条 file/image 消息（带本地落盘路径，UI 可直接显示缩略图）。"""
        t = event.data
        if not isinstance(t, Transfer) or t.direction != "recv" or t.state != TransferState.COMPLETED:
            return
        f = t.file
        if f is None or f.file_id in self._chat_notified:
            return
        self._chat_notified.add(f.file_id)
        entry = replace(f, local_path=t.final_path or f.local_path)
        msg = ChatMessage(
            msg_id=str(uuid.uuid4()),
            conv_id=t.peer_id,
            kind=MsgKind.IMAGE if entry.kind == "image" else MsgKind.FILE,
            outgoing=False,
            status="received",
            transfer_id=t.transfer_id,
            file=entry,
        )
        self._persist_message(msg)
        self.bus.publish(EventType.MESSAGE, msg)

    def history(self, peer_id: str) -> List[ChatMessage]:
        if self.store is None:
            return []
        return self.store.messages_for(peer_id)

    def clear_messages(self, peer_id: str) -> int:
        """清空某会话的聊天记录（会话条目保留，返回删掉的条数）。"""
        if self.store is None:
            return 0
        try:
            n = self.store.clear_messages(peer_id)
        except Exception:
            return 0
        self.bus.publish(EventType.LOG, f"已清空聊天记录（{n} 条）")
        return n

    def delete_message(self, msg_id: str) -> bool:
        """删掉一条消息（只删本机记录）。"""
        if self.store is None:
            return False
        try:
            return self.store.delete_message(msg_id) > 0
        except Exception:
            return False

    def recent_conversations(self) -> List[dict]:
        if self.store is None:
            return []
        return self.store.conversations()

    def hidden_conversation_ids(self) -> set:
        """被删除（隐藏）的会话 id：UI 据此过滤掉仍在线的对端。"""
        if self.store is None:
            return set()
        try:
            return self.store.hidden_conversation_ids()
        except Exception:
            return set()

    def touch_conversation(self, peer_id: str, title: Optional[str] = None) -> None:
        """新建 / 打开会话时登记，使其在列表里持久存在（对端离线也不消失）。"""
        if self.store is None:
            return
        try:
            self.store.ensure_conversation(peer_id, title or self._peer_name(peer_id))
        except Exception:
            pass

    def delete_conversation(self, peer_id: str) -> None:
        """删除会话：清空聊天记录，并让该对端不再自动出现在列表里。"""
        if self.store is None:
            return
        try:
            self.store.delete_conversation(peer_id)
        except Exception:
            pass

    def is_manual_conv(self, conv_id: str) -> bool:
        """是否是「手动 IP」合成会话（还没发现设备）。"""
        return conv_id.startswith(_MANUAL_PREFIX)

    def create_conversation_by_ip(self, ip: str) -> str:
        """按 IP 新建会话。

        - 该 IP 的设备若已发现 → 直接用真实 device_id 建会话；
        - 否则先建一个 `ip:<addr>` 的合成会话（立刻可见），同时主动单播探测；
          设备被发现后会自动升级成真实 device_id（消息一并迁移）。
        """
        clean = (ip or "").strip()
        if not clean:
            return ""
        self.discovery.add_manual_target(clean)
        self.gateway.add_candidate(clean)
        found = next((p for p in self.discovery.snapshot() if p.ip == clean), None)
        if found is not None:
            self.touch_conversation(found.device_id, found.name)
            return found.device_id
        conv_id = _MANUAL_PREFIX + clean
        if self.store is not None:
            try:
                self.store.ensure_conversation(conv_id, clean)
            except Exception:
                pass
        return conv_id

    def _peers_at_ip(self, ip: str) -> list:
        """这个 IP 上「我们已知」的设备（发现表 / 最近见到 / 记忆里）。"""
        out: list = []
        seen: set = set()
        for peer in self.discovery.snapshot():
            if peer.ip == ip:
                out.append(peer)
                seen.add(peer.device_id)
        for pid, (peer, _ts) in list(self._endpoints.items()):
            if peer.ip == ip and pid not in seen:
                out.append(peer)
                seen.add(pid)
        for pid, info in list(self._known_peers.items()):
            if info.get("ip") == ip and pid not in seen:
                out.append(Peer(
                    device_id=pid, name=info.get("name") or pid[:8], ip=ip,
                    tcp_port=int(info.get("tcp_port") or self.cfg.tcp_port),
                    udp_port=self.cfg.udp_port,
                    device_type=info.get("device_type") or "unknown",
                    state=PeerState.ONLINE,
                ))
                seen.add(pid)
        return out

    def connect_by_ip(self, ip: str, port: Optional[int] = None,
                      timeout: float = 6.0) -> tuple:
        """只知 IP 的**直连**：不依赖 UDP 发现（热点下广播发不出去）。

        返回 (是否成功, 说明)。成功后把对端登记进发现表：
        `PEER_ADDED` 会让「ip:<addr>」会话自动升级成真实设备（消息一并迁移），
        于是附近列表、传输、通知都能正常工作。

        已知 device_id 的对端走普通按 id 连接（不需要身份应答）；
        只有**完全陌生**的 IP 才用 `ip_only` 直连去问对方是谁。
        这样避免和后台维护循环同时拨号而产生两条连接（多出来的那条会被
        「保留 canon 方向」的规则关掉，反而让本次调用看起来失败）。
        """
        clean = (ip or "").strip()
        if not clean:
            return False, "请填写对方的 IP"
        known = self._peers_at_ip(clean)
        for peer in known:
            if self.transport.get_session(peer.device_id) is not None:
                self._remember_endpoint(peer)
                self._remember_peer(peer)
                return True, f"已连接到 {peer.name}"
        for peer in known:
            self.transport.connect_control(clean, peer.device_id, port or peer.tcp_port)
            if self.transport.get_session(peer.device_id) is not None:
                self._remember_endpoint(peer)
                self._remember_peer(peer)
                if self.discovery.get(peer.device_id) is None:
                    try:
                        self.discovery.register_peer(peer)
                    except Exception:
                        pass
                return True, f"已连接 {peer.name}"
            return False, f"连不上 {peer.name}（{clean}）"
        sess, info, err = self.transport.connect_direct(clean, port, timeout)
        if sess is None:
            return False, err or "直连失败"
        info = info or {}
        encrypted = bool(self.tunnel is not None and getattr(self.tunnel, "enabled", False))
        peer = Peer(
            device_id=info.get("device_id") or sess.peer_id,
            name=info.get("name") or (info.get("device_id") or sess.peer_id)[:8],
            ip=clean,
            tcp_port=int(info.get("tcp_port") or self.cfg.tcp_port),
            udp_port=int(info.get("udp_port") or self.cfg.udp_port),
            device_type=info.get("device_type") or "unknown",
            proto=int(info.get("proto") or 1),
            encrypted=encrypted,
            state=PeerState.ONLINE,
        )
        self._remember_endpoint(peer)
        self._remember_peer(peer)
        try:
            self.discovery.register_peer(peer)      # 发 PEER_ADDED → ip: 会话自动升级
        except Exception:
            pass
        self.bus.publish(EventType.LOG, f"已直连 {peer.name}（{clean}）")
        return True, f"已直连 {peer.name}"

    def _kick_session(self, peer_id: str) -> None:
        """有人要用控制连接了（比如发文件）→ 立刻重连，别等下一次退避。

        会话短暂掉线时，文件会等它回来；这里把等待时间从「最多 15 秒退避」压到「下一次维护循环」。
        """
        self._backoff_until.pop(peer_id, None)
        self._attempts.pop(peer_id, None)
        self._no_session_since[peer_id] = 0.0    # 立刻允许兜底反连

    def _learn_incoming_peer(self, info: dict, ip: str) -> None:
        """把「主动连进来」的对端登记进发现表 + 端点缓存。

        拨号方在 `connect` 里带了自己的身份（device_id/名字/端口），这里连来源 IP 一起记下来。
        不记的话被叫方虽然**能收**，却不知道对方在哪、**发不回去** ——
        手机当热点时对方发不出广播，这是唯一的信息来源。
        """
        did = str(info.get("device_id") or "")
        if not did or did == self.cfg.device_id or not ip:
            return
        peer = Peer(
            device_id=did,
            name=info.get("name") or did[:8],
            ip=ip,
            tcp_port=int(info.get("tcp_port") or self.cfg.tcp_port),
            udp_port=int(info.get("udp_port") or self.cfg.udp_port),
            device_type=info.get("device_type") or "unknown",
            proto=int(info.get("proto") or 1),
            state=PeerState.ONLINE,
        )
        self._remember_endpoint(peer)
        self._remember_peer(peer)
        try:
            self.discovery.register_peer(peer)
        except Exception:
            pass

    def _resolve_send_target(self, peer_id: str) -> Optional[str]:
        """把发送目标从「ip:<addr>」解析成真实 device_id（未发现则为 None）。"""
        if not peer_id.startswith(_MANUAL_PREFIX):
            return peer_id
        ip = peer_id[len(_MANUAL_PREFIX):]
        found = next((p for p in self.discovery.snapshot() if p.ip == ip), None)
        return found.device_id if found is not None else None

    def _on_peer_for_bind(self, event) -> None:
        peer = event.data
        if isinstance(peer, Peer):
            self._bind_manual_conversation(peer)

    def _bind_manual_conversation(self, peer: Peer) -> None:
        """发现设备后，把对应的手动 IP 会话升级成真实 device_id。"""
        old_id = _MANUAL_PREFIX + peer.ip
        if peer.device_id == old_id or self.store is None:
            return
        try:
            if not self.store.has_conversation(old_id):
                return
            self.store.rebind_conversation(old_id, peer.device_id, peer.name)
        except Exception:
            return
        # 待发队列里指向「ip:」的消息改指向真实设备
        with self._outbox_lock:
            for i, item in enumerate(self._outbox):
                pid, env, msg = item
                if pid == old_id:
                    try:
                        env["to"] = peer.device_id
                    except Exception:
                        pass
                    self._outbox[i] = (peer.device_id, env, msg)
        self.bus.publish(EventType.CONVERSATION_REBOUND, (old_id, peer.device_id))

    def _peer_name(self, peer_id: str) -> Optional[str]:
        peer = self.get_peer(peer_id)
        return peer.name if peer is not None else None

    # ------------------------------------------------------------- 文件传输
    def send_files(self, peer_id: str, paths: List[str]) -> Optional[str]:
        # 手动 IP 会话：设备若已发现就发往真实 device_id，否则提示稍后再试
        target = self._resolve_send_target(peer_id)
        if target is None:
            self.bus.publish(
                EventType.LOG, "还没发现该 IP 上的设备，暂时发不了文件，稍后再试"
            )
            return None
        return self.transfer.send_paths(target, [Path(p) for p in paths])

    def cancel_transfer(self, transfer_id: str) -> None:
        self.transfer.cancel(transfer_id)

    def delete_transfer(self, file_id: str, remove_file: bool = True) -> tuple:
        """删除一条传输记录。

        `remove_file=True` 时把这条记录对应的**那一个**落盘文件也删掉
        （final_path 是逐文件的，文件夹传输里其它文件不受影响）。
        传输进行中（等待/传输中）拒绝删除，先「取消」再删。返回 (是否成功, 说明)。
        """
        if self.store is None:
            return False, "存储未就绪"
        try:
            row = self.store.transfer(file_id)
        except Exception as exc:
            return False, f"读取记录失败：{exc}"
        if row is None:
            return False, "记录不存在（可能已被删除）"
        if str(row.get("state") or "") in ("pending", "active"):
            return False, "传输进行中，请先「取消」再删除"
        path = row.get("final_path")
        removed = False
        if remove_file and path:
            p = Path(path)
            try:
                if p.is_dir():
                    # 兜底：万一记录指向目录，只移除记录，不整棵树删掉
                    pass
                elif p.exists():
                    p.unlink()
                    removed = True
            except Exception as exc:
                return False, f"删除文件失败：{exc}"
        try:
            self.store.delete_transfer(file_id)
        except Exception as exc:
            return False, f"删除记录失败：{exc}"
        self.bus.publish(EventType.LOG, f"已删除传输：{row.get('name') or file_id}")
        return True, ("已删除文件" if removed else "已从列表移除")

    def transfers(self) -> List[Transfer]:
        return self.transfer.transfers()

    def persisted_transfers(self) -> List[Transfer]:
        """从数据库读出历史传输记录，供 UI 启动时回填列表。

        否则关掉程序再打开，传输页就只剩「本次会话」的记录（历史记录看不到）。
        记录里只存了 file_id/name/size/final_path，足够展示与删除。
        """
        if self.store is None:
            return []
        try:
            rows = self.store.transfers(limit=100000)
        except Exception:
            return []
        out: List[Transfer] = []
        for r in rows:
            try:
                state = TransferState(str(r.get("state") or "pending"))
            except ValueError:
                state = TransferState.FAILED
            direction = str(r.get("direction") or "send")
            done = int(r.get("received") or 0)
            file = FileEntry(
                file_id=str(r.get("file_id") or ""),
                name=str(r.get("name") or ""),
                rel_path=str(r.get("rel_path") or ""),
                size=int(r.get("size") or 0),
            )
            out.append(
                Transfer(
                    transfer_id=str(r.get("group_id") or r.get("file_id") or ""),
                    peer_id=str(r.get("peer_id") or ""),
                    file=file,
                    direction=direction,
                    state=state,
                    sent=done if direction == "send" else 0,
                    received=done if direction == "recv" else 0,
                    final_path=r.get("final_path"),
                    error=r.get("error"),
                    local_path=r.get("final_path"),
                )
            )
        return out

    def clear_transfers(self, remove_file: bool = True) -> tuple:
        """清空传输列表：删掉所有**已结束**的记录，进行中的跳过；返回 (是否成功, 说明)。

        `remove_file=True` 时把每条记录对应的落盘文件也删掉（与单条删除一致）。
        """
        if self.store is None:
            return False, "存储未就绪"
        try:
            rows = self.store.transfers(limit=100000)
        except Exception as exc:
            return False, f"读取记录失败：{exc}"
        ids: List[str] = []
        files = 0
        kept = 0
        for row in rows:
            if str(row.get("state") or "") in ("pending", "active"):
                kept += 1
                continue
            fid = row.get("file_id")
            if not fid:
                continue
            if remove_file:
                path = row.get("final_path")
                if path:
                    p = Path(path)
                    try:
                        if p.is_file():
                            p.unlink()
                            files += 1
                    except Exception:
                        pass  # 删不掉就只移除记录
            ids.append(str(fid))
        try:
            self.store.delete_transfers(ids)
        except Exception as exc:
            return False, f"清空记录失败：{exc}"
        self.bus.publish(
            EventType.LOG, f"已清空传输列表（{len(ids)} 条，删除文件 {files} 个）"
        )
        msg = f"已清空 {len(ids)} 条记录"
        if files:
            msg += f"，删除 {files} 个文件"
        if kept:
            msg += f"；{kept} 个进行中任务已保留"
        return True, msg

    # ------------------------------------------------------------- 文本消息
    def send_text(self, peer_id: str, text: str) -> Optional[ChatMessage]:
        client_id = str(uuid.uuid4())
        # 手动 IP 会话：设备若已发现就发往真实 device_id，否则先排队
        target = self._resolve_send_target(peer_id)
        send_to = target or peer_id
        env = messages.envelope(
            "text", self.cfg.device_id, send_to,
            {"text": text, "client_id": client_id},
        )
        ok = target is not None and self.transport.send_to(target, env)
        msg = ChatMessage(
            msg_id=client_id, conv_id=peer_id, kind=MsgKind.TEXT,
            text=text, outgoing=True, status="sent" if ok else "pending",
        )
        self._remember(client_id)
        self._persist_message(msg)
        # 连不上 / 还没发现设备都不丢消息：先入队，连上后自动补发
        if not ok:
            if target is None:
                self.bus.publish(
                    EventType.LOG, "还没发现该 IP 上的设备，消息已排队，发现后自动发送"
                )
            with self._outbox_lock:
                self._outbox.append((send_to, env, msg))
        self.bus.publish(EventType.MESSAGE, msg)
        return msg

    def _flush_outbox(self) -> None:
        if not self._outbox:
            return
        with self._outbox_lock:
            pending = list(self._outbox)
        for item in pending:
            peer_id, env, msg = item
            if not self.transport.send_to(peer_id, env):
                continue
            with self._outbox_lock:
                try:
                    self._outbox.remove(item)
                except ValueError:
                    pass
            # ⚠ 不要原地改 msg.status：UI 侧可能持有同一实例，判断“值没变”就不刷新。
            # 状态统一走 DB + MESSAGE_STATUS 事件。
            self._persist_message_status(msg.msg_id, "sent")
            self.bus.publish(EventType.MESSAGE_STATUS, {"msg_id": msg.msg_id, "status": "sent"})

    def _persist_message_status(self, msg_id: str, status: str) -> None:
        if self.store is not None:
            try:
                self.store.update_message_status(msg_id, status)
            except Exception:
                pass

    def _on_text(self, env: dict, peer_id: str) -> None:
        data = env.get("data") or {}
        client_id = data.get("client_id") or env.get("id")
        text = data.get("text", "")
        if client_id and self._is_duplicate(client_id):
            return
        msg_id = client_id or env.get("id") or str(uuid.uuid4())
        ts = (env.get("ts") or 0) / 1000.0 or time.time()
        msg = ChatMessage(
            msg_id=msg_id, conv_id=peer_id, kind=MsgKind.TEXT, text=text,
            ts=ts, outgoing=False, status="received",
        )
        self._persist_message(msg)
        self.bus.publish(EventType.MESSAGE, msg)
        sess = self.transport.get_session(peer_id)
        if sess is not None:
            sess.send(messages.envelope(
                "delivered", self.cfg.device_id, peer_id, {"msg_id": msg_id}
            ))

    def _on_delivered(self, env: dict) -> None:
        data = env.get("data") or {}
        msg_id = data.get("msg_id")
        if not msg_id:
            return
        # 同上：只写 DB + 发事件，不原地改内存里的 ChatMessage
        if self.store is not None:
            try:
                self.store.update_message_status(msg_id, "delivered")
            except Exception:
                pass
        self.bus.publish(EventType.MESSAGE_STATUS, {"msg_id": msg_id, "status": "delivered"})

    # ------------------------------------------------------------- 控制派发
    def _on_control(self, sess: ControlSession, env: dict) -> None:
        mtype = env.get("type")
        peer_id = sess.peer_id
        if mtype == "ping":
            sess.send(messages.envelope("pong", self.cfg.device_id, peer_id, {}))
            return
        if mtype == "pong":
            sess.last_pong = time.time()
            return
        if mtype == "text":
            self._on_text(env, peer_id)
            return
        if mtype == "delivered":
            self._on_delivered(env)
            return
        if mtype in _TRANSFER_TYPES and self.transfer is not None:
            try:
                self.transfer.handle_control(sess, env)
            except Exception:
                pass
            return
        # 未知类型：忽略

    def _on_data(self, sock, ftype: int, body: bytes, addr: tuple) -> None:
        if self.transfer is not None:
            self.transfer.handle_data_connection(sock, ftype, body, addr)
            return
        try:
            sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------- 维护循环
    def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            self._ensure_sessions(now)
            self._ping_sessions(now)
            self._flush_outbox()
            try:
                self.gateway.prune()
            except Exception:
                pass
            self._stop.wait(1.0)

    def _ensure_sessions(self, now: float) -> None:
        peers = list(self.discovery.snapshot())
        known_ids = {p.device_id for p in peers}
        # 补上「发现表被丢包剪掉、但我们最近还见过」的对端，保证还能重连
        for pid, (cached_peer, ts) in list(self._endpoints.items()):
            if pid not in known_ids and now - ts < _ENDPOINT_TTL:
                peers.append(cached_peer)
                known_ids.add(pid)
        # 再补上「以前连上过、地址已落盘」的对端：手机开热点时 UDP 永远发现不到，
        # 只有靠记忆里的地址直连回去。
        for pid, info in list(self._known_peers.items()):
            if pid in known_ids:
                continue
            try:
                peers.append(Peer(
                    device_id=pid,
                    name=info.get("name") or pid[:8],
                    ip=str(info["ip"]),
                    tcp_port=int(info.get("tcp_port") or self.cfg.tcp_port),
                    udp_port=self.cfg.udp_port,
                    device_type=info.get("device_type") or "unknown",
                    state=PeerState.ONLINE,
                ))
                known_ids.add(pid)
            except Exception:
                continue
        for peer in peers:
            pid = peer.device_id
            remember = False
            sess = self.transport.get_session(pid)
            if sess is not None:
                # 在线会话的远端地址才是真地址：发现表/记忆里可能是旧的（换网络后）。
                # 不修正的话，数据连接会拿着旧 IP（比如上一次 WLAN 的地址）去发，必然失败。
                live_ip = sess.remote_ip()
                if live_ip and live_ip != peer.ip:
                    peer = replace(peer, ip=live_ip)
                    try:
                        self.discovery.register_peer(peer)   # 修正地址（变更时会通知 UI）
                    except Exception:
                        pass
                self._remember_endpoint(peer, now)
                remember = True
                # 有连接就说明对方活着：
                # 1) 刷新 last_seen —— 否则 UDP 通不了的对端（手机热点）每 10 秒被剪一次，
                #    附近列表就会「删了又加」地闪；
                # 2) 真被剪掉了就补回去（附近列表也会跟着恢复）。
                if self.discovery.get(pid) is None:
                    try:
                        self.discovery.register_peer(peer)
                    except Exception:
                        pass
                else:
                    self.discovery.touch(pid)
                self._attempts.pop(pid, None)
                self._backoff_until.pop(pid, None)
                self._no_session_since.pop(pid, None)
            else:
                if now < self._backoff_until.get(pid, 0.0):
                    continue
                # 首选由 device_id 较小的一方发起（两端都连时也是它胜出）；
                # 但若等待 _FALLBACK_DELAY 秒仍无连接，另一侧也主动兜底反连，
                # 否则一旦“首选方向”被防火墙/路由挡住就永远连不上。
                since = self._no_session_since.setdefault(pid, now)
                is_canon = self.cfg.device_id < pid
                if not is_canon and now - since < _FALLBACK_DELAY:
                    continue
                sess = self.transport.connect_control(peer.ip, pid, peer.tcp_port)
                if sess is None:
                    n = self._attempts.get(pid, 0) + 1
                    self._attempts[pid] = n
                    delay = RECONNECT_BACKOFF[min(n - 1, len(RECONNECT_BACKOFF) - 1)]
                    delay *= 1 + random.uniform(-RECONNECT_JITTER, RECONNECT_JITTER)
                    # 只在「记忆里」而不在发现表里的对端：放慢重试，避免在别的网络里空拨
                    if self.discovery.get(pid) is None:
                        delay *= 4
                    self._backoff_until[pid] = now + delay
                else:
                    remember = True
            if remember:
                self._remember_peer(peer)

    def _ping_sessions(self, now: float) -> None:
        for sess in self.transport.sessions():
            if now - sess.last_pong > PEER_TIMEOUT:
                sess.close()
                continue
            if now - self._last_ping.get(sess.peer_id, 0.0) >= PING_INTERVAL:
                self._last_ping[sess.peer_id] = now
                sess.send(messages.envelope("ping", self.cfg.device_id, sess.peer_id, {}))

    # ------------------------------------------------------------- 辅助
    def _remember(self, key: str) -> None:
        self._seen_text[key] = True
        while len(self._seen_text) > _DEDUP_MAX:
            self._seen_text.popitem(last=False)

    def _is_duplicate(self, key: str) -> bool:
        if key in self._seen_text:
            return True
        self._remember(key)
        return False

    def _persist_message(self, msg: ChatMessage) -> None:
        if self.store is not None:
            try:
                # add_message 已顺带登记会话；这里再把对端名字补成标题（离线时也能显示）
                self.store.add_message(msg)
                name = self._peer_name(msg.conv_id)
                if name:
                    self.store.ensure_conversation(msg.conv_id, name)
            except Exception:
                pass
