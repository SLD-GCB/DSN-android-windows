"""UDP 多通道发现。

通道：受限广播 255.255.255.255 + 逐网卡定向广播 + 多播(224.0.0.167) + 手动/历史 IP 单播探测。
- 接收用一个 SO_REUSEADDR socket 绑 ("", udp_port)，并尝试加入多播组；
- 发送为每个网卡各持一个 sender socket（绑定源 IP），强制从对应网卡出包；
- 去重键 = device_id；收到 hello 即注册/刷新，超时 PEER_TIMEOUT 或收到 bye 即移除；
- 对「非 direct 的 hello」回一条 direct hello（按源 IP 限频 2s），使手动 IP / 网关场景能反向被发现；
- gw_* 报文交给外部 gateway handler。
"""
from __future__ import annotations

import hmac
import socket
import threading
import time
from typing import Callable, Dict, List, Optional

from ..config import Config
from ..events import EventBus, EventType
from ..models import Peer, PeerState
from ..protocol import messages
from ..protocol.constants import (
    HELLO_INTERVAL,
    MULTICAST_GROUP,
    PEER_TIMEOUT,
    PROTO_MIN,
    PROTO_VERSION,
)
from . import interfaces as ifaces_mod
from .tunnel import hello_fields

_GW_TYPES = {"gw_hello", "gw_ack", "gw_register", "gw_peer_list", "gw_goodbye"}
_REPLY_MIN_INTERVAL = 2.0


class DiscoveryService:
    def __init__(
        self,
        cfg: Config,
        bus: EventBus,
        gateway_enabled: bool = True,
        tunnel_provider: Optional[Callable[[], object]] = None,
    ) -> None:
        self.cfg = cfg
        self.bus = bus
        self.gateway_enabled = gateway_enabled
        self._tunnel_provider = tunnel_provider

        self._peers: Dict[str, Peer] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._recv: Optional[socket.socket] = None
        self._senders: Dict[str, socket.socket] = {}
        self._ifaces: List[ifaces_mod.Iface] = []
        self._threads: List[threading.Thread] = []
        self._gw_handler: Optional[Callable[[dict, tuple], None]] = None
        self._manual_targets: List[str] = []
        self._last_reply: Dict[str, float] = {}
        self._interval = HELLO_INTERVAL

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        self._refresh_interfaces()
        self._recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._recv.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._recv.bind(("", self.cfg.udp_port))
        self._recv.settimeout(1.0)
        self._join_multicast()

        self._threads = [
            threading.Thread(target=self._recv_loop, name="discovery-recv", daemon=True),
            threading.Thread(target=self._announce_loop, name="discovery-announce", daemon=True),
            threading.Thread(target=self._prune_loop, name="discovery-prune", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            self.send_hello(direct=False)  # 尽量发一次再见前不留残影
            self._send_bye()
        except Exception:
            pass
        if self._recv is not None:
            try:
                self._recv.close()
            except OSError:
                pass
        for s in self._senders.values():
            try:
                s.close()
            except OSError:
                pass
        self._senders.clear()

    def set_gateway_handler(self, handler: Callable[[dict, tuple], None]) -> None:
        self._gw_handler = handler

    def set_background(self, background: bool) -> None:
        from ..protocol.constants import HELLO_INTERVAL_BG

        self._interval = HELLO_INTERVAL_BG if background else HELLO_INTERVAL

    # ------------------------------------------------------------- 对外查询
    def snapshot(self) -> List[Peer]:
        with self._lock:
            return list(self._peers.values())

    def get(self, device_id: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(device_id)

    def add_manual_target(self, ip: str) -> None:
        ip = ip.strip()
        if not ip or ip in self._manual_targets:
            return
        self._manual_targets.append(ip)
        self.send_hello(direct=True, dst=ip)

    # ------------------------------------------------------------- 网卡/发送
    def _refresh_interfaces(self) -> None:
        self._ifaces = ifaces_mod.list_ipv4_interfaces()
        wanted = {i.ip for i in self._ifaces}
        for ip in list(self._senders):
            if ip not in wanted:
                try:
                    self._senders[ip].close()
                except OSError:
                    pass
                del self._senders[ip]
        for i in self._ifaces:
            if i.ip not in self._senders:
                self._senders[i.ip] = self._make_sender(i.ip)

    @staticmethod
    def _make_sender(ip: str) -> socket.socket:
        s = ifaces_mod.open_sender(ip, broadcast=True)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        except OSError:
            pass
        return s

    def _join_multicast(self) -> None:
        for i in self._ifaces:
            try:
                mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(i.ip)
                self._recv.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass

    def _current_tunnel(self):
        if self._tunnel_provider is None:
            return None
        try:
            return self._tunnel_provider()
        except Exception:
            return None

    def _build_hello(self, direct: bool) -> bytes:
        ts = int(time.time() * 1000)
        data = {
            "name": self.cfg.device_name,
            "device_type": self.cfg.device_type,
            "tcp_port": self.cfg.tcp_port,
            "udp_port": self.cfg.udp_port,
            "proto_min": PROTO_MIN,
            "proto_max": PROTO_VERSION,
            "ts": ts,
        }
        if direct:
            data["direct"] = True
        tunnel = self._current_tunnel()
        if tunnel is not None and getattr(tunnel, "enabled", False):
            data["enc"] = 1
            data["mac"] = tunnel.sign_hello(hello_fields(
                self.cfg.device_id, self.cfg.device_name, self.cfg.device_type,
                self.cfg.tcp_port, self.cfg.udp_port, ts,
            ))
        return messages.encode(messages.envelope("hello", self.cfg.device_id, None, data))

    def send_hello(self, direct: bool = False, dst: Optional[str] = None) -> None:
        raw = self._build_hello(direct)
        if dst is not None:
            sock = next(iter(self._senders.values()), None)
            if sock is None:
                return
            try:
                sock.sendto(raw, (dst, self.cfg.udp_port))
            except OSError:
                pass
            return
        targets = ifaces_mod.broadcast_targets(self._ifaces, include_hotspot_fallback=True)
        for sock in list(self._senders.values()):
            for tgt in targets:
                try:
                    sock.sendto(raw, (tgt, self.cfg.udp_port))
                except OSError:
                    pass
            try:
                sock.sendto(raw, (MULTICAST_GROUP, self.cfg.udp_port))
            except OSError:
                pass

    def _send_bye(self) -> None:
        raw = messages.encode(messages.envelope("bye", self.cfg.device_id, None, {}))
        targets = ifaces_mod.broadcast_targets(self._ifaces, include_hotspot_fallback=True)
        for sock in list(self._senders.values()):
            for tgt in targets:
                try:
                    sock.sendto(raw, (tgt, self.cfg.udp_port))
                except OSError:
                    pass

    # ------------------------------------------------------------- 循环
    def _announce_loop(self) -> None:
        # 首次尽快广播
        self._refresh_interfaces()
        while not self._stop.is_set():
            try:
                self.send_hello(direct=False)
                for ip in list(self._manual_targets):
                    self.send_hello(direct=True, dst=ip)
                self._refresh_interfaces()
            except Exception:
                pass
            self._stop.wait(self._interval)

    def _prune_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            removed: List[str] = []
            with self._lock:
                for did, peer in list(self._peers.items()):
                    if now - peer.last_seen > PEER_TIMEOUT:
                        del self._peers[did]
                        removed.append(did)
            for did in removed:
                self.bus.publish(EventType.PEER_REMOVED, did)
            self._stop.wait(1.0)

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._recv.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle_datagram(data, addr)
            except Exception:
                pass

    # ------------------------------------------------------------- 处理
    def _handle_datagram(self, data: bytes, addr: tuple) -> None:
        env = messages.decode(data)
        if env is None:
            return
        if env.get("from") == self.cfg.device_id:
            return  # 自己发的（广播回环）
        mtype = env.get("type")
        src_ip = addr[0]

        if mtype in _GW_TYPES:
            if self.gateway_enabled and self._gw_handler is not None:
                try:
                    self._gw_handler(env, addr)
                except Exception:
                    pass
            return

        if mtype == "hello":
            self._on_hello(env, src_ip)
            if not (env.get("data") or {}).get("direct"):
                self._maybe_reply(src_ip)
        elif mtype == "bye":
            self._remove_peer(env.get("from"))

    def _on_hello(self, env: dict, src_ip: str) -> None:
        did = env.get("from")
        if not did:
            return
        data = env.get("data") or {}
        encrypted = bool(data.get("enc"))

        # 本机已启用加密 → 只认签名正确的 hello（防同网段伪造设备冒进列表）
        tunnel = self._current_tunnel()
        if tunnel is not None and getattr(tunnel, "enabled", False):
            mac = str(data.get("mac") or "")
            expect = tunnel.sign_hello(hello_fields(
                did,
                data.get("name") or "",
                data.get("device_type", "windows"),
                int(data.get("tcp_port", 37021)),
                int(data.get("udp_port", self.cfg.udp_port)),
                int(data.get("ts") or 0),
            ))
            if not mac or not hmac.compare_digest(mac, expect):
                return

        now = time.time()
        is_new = False
        changed = False
        with self._lock:
            peer = self._peers.get(did)
            name = data.get("name") or did[:8]
            tcp_port = int(data.get("tcp_port", 37021))
            udp_port = int(data.get("udp_port", self.cfg.udp_port))
            dtype = data.get("device_type", "windows")
            proto = int(env.get("proto", 1))
            if peer is None:
                peer = Peer(
                    device_id=did,
                    name=name,
                    ip=src_ip,
                    tcp_port=tcp_port,
                    udp_port=udp_port,
                    device_type=dtype,
                    proto=proto,
                    encrypted=encrypted,
                    last_seen=now,
                    state=PeerState.ONLINE,
                )
                self._peers[did] = peer
                is_new = True
            else:
                changed = (
                    peer.ip != src_ip
                    or peer.name != name
                    or peer.tcp_port != tcp_port
                    or peer.encrypted != encrypted
                    or peer.state != PeerState.ONLINE
                )
                peer.ip = src_ip
                peer.name = name
                peer.tcp_port = tcp_port
                peer.udp_port = udp_port
                peer.device_type = dtype
                peer.proto = proto
                peer.encrypted = encrypted
                peer.state = PeerState.ONLINE
                peer.touch()
        if is_new:
            self.bus.publish(EventType.PEER_ADDED, peer)
        elif changed:
            self.bus.publish(EventType.PEER_UPDATED, peer)

    def _remove_peer(self, device_id: Optional[str]) -> None:
        if not device_id:
            return
        with self._lock:
            existed = self._peers.pop(device_id, None)
        if existed is not None:
            self.bus.publish(EventType.PEER_REMOVED, device_id)

    def register_peer(self, peer: Peer) -> None:
        """供网关中继 / 直连把对端注入发现表。"""
        with self._lock:
            # 注入的 peer 可能来自「记忆里的地址」这种陈旧对象，last_seen 很老；
            # 不刷新的话下一轮 prune 立刻又把它剪掉 → 附近列表每秒闪一次。
            peer.touch()
            existing = self._peers.get(peer.device_id)
            if existing is None:
                self._peers[peer.device_id] = peer
                self.bus.publish(EventType.PEER_ADDED, peer)
            else:
                changed = (
                    existing.ip != peer.ip
                    or existing.name != peer.name
                    or existing.tcp_port != peer.tcp_port
                    or existing.encrypted != peer.encrypted
                )
                existing.ip = peer.ip
                existing.name = peer.name
                existing.tcp_port = peer.tcp_port
                existing.encrypted = peer.encrypted
                existing.touch()
                if changed:
                    self.bus.publish(EventType.PEER_UPDATED, existing)

    def touch(self, device_id: str) -> None:
        """刷新 last_seen（不碰其它字段、不发事件）。

        有活着的控制连接就说明对方还在，必须持续刷新，否则 UDP 通不了的对端
        （典型：手机开热点时发不出广播）会每 10 秒被剪一次、再被补回来。
        """
        with self._lock:
            peer = self._peers.get(device_id)
            if peer is not None:
                peer.touch()

    def _maybe_reply(self, src_ip: str) -> None:
        now = time.time()
        if now - self._last_reply.get(src_ip, 0.0) < _REPLY_MIN_INTERVAL:
            return
        self._last_reply[src_ip] = now
        self.send_hello(direct=True, dst=src_ip)
