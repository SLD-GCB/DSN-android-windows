"""网关中继（Gateway Rendezvous）。

用于无互联网热点 / AP 隔离场景：客户端↔客户端广播可能被拦，改由「客户端 → 网关(热点主机)」单播汇聚。

- **客户端角色**：周期性向候选网关 + 真实默认网关发 `gw_hello`；收到 `gw_ack` 后发 `gw_register`；
  收到 `gw_peer_list` 后把其中设备注入 DiscoveryService 的 peer 表。
- **网关角色**：收到 `gw_hello` 回 `gw_ack`；收到 `gw_register` 记录客户端并回推 `gw_peer_list`；
  成员变化时主动向所有客户端推送。

本类不自己收包：`gw_*` 报文由 DiscoveryService 的接收 socket 收到后转发进来（handle）。
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from typing import Dict, List, Optional

from ..config import Config
from ..events import EventBus
from ..models import Peer, PeerState
from ..protocol import messages
from ..protocol.constants import (
    CANDIDATE_GATEWAYS,
    GATEWAY_PROBE_INTERVAL,
    PEER_TIMEOUT,
    PROTO_VERSION,
)
from . import interfaces as ifaces_mod
from .discovery import DiscoveryService

_REGISTER_MIN_INTERVAL = 5.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class GatewayRendezvous:
    def __init__(
        self,
        cfg: Config,
        bus: EventBus,
        discovery: DiscoveryService,
        enabled: bool = True,
        tunnel_provider=None,
    ) -> None:
        self.cfg = cfg
        self.bus = bus
        self.discovery = discovery
        self.enabled = enabled
        self._tunnel_provider = tunnel_provider

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._gateways: List[str] = list(CANDIDATE_GATEWAYS)
        self._gw_clients: Dict[str, dict] = {}   # ip -> device_info
        self._last_register: Dict[str, float] = {}
        self._lock = threading.RLock()

    def _local_encrypted(self) -> bool:
        """本机是否启用加密隧道（写进 device_info，让别的设备也能显示「已加密」）。"""
        try:
            tunnel = self._tunnel_provider() if self._tunnel_provider else None
            return bool(getattr(tunnel, "enabled", False))
        except Exception:
            return False

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if not self.enabled:
            return
        self.discovery.set_gateway_handler(self.handle)
        gw = ifaces_mod.default_gateway()
        if gw and gw not in self._gateways:
            self._gateways.append(gw)
        self._thread = threading.Thread(target=self._probe_loop, name="gw-probe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        for ip in list(self._gw_clients):
            self._send("gw_goodbye", {"device_id": self.cfg.device_id}, ip)

    def add_candidate(self, ip: str) -> None:
        ip = ip.strip()
        if ip and ip not in self._gateways:
            self._gateways.append(ip)

    # ------------------------------------------------------------- 发送工具
    def _source_ip_for(self, dst_ip: str) -> Optional[str]:
        try:
            dst = ipaddress.ip_address(dst_ip)
        except ValueError:
            return None
        for i in ifaces_mod.list_ipv4_interfaces():
            try:
                if dst in ipaddress.IPv4Network(f"{i.ip}/{i.netmask}", strict=False):
                    return i.ip
            except ValueError:
                continue
        primary = ifaces_mod.pick_primary()
        return primary.ip if primary else None

    def _send(self, msg_type: str, data: dict, dst_ip: str) -> None:
        raw = messages.encode(messages.envelope(msg_type, self.cfg.device_id, None, data))
        src = self._source_ip_for(dst_ip)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if src:
                s.bind((src, 0))
            s.sendto(raw, (dst_ip, self.cfg.udp_port))
        except OSError:
            pass
        finally:
            s.close()

    # ------------------------------------------------------------- 设备信息
    def _my_ip(self) -> str:
        # 网关场景优先热点网段，否则主用网卡
        primary = ifaces_mod.pick_primary()
        return primary.ip if primary else "0.0.0.0"

    def _device_info(self) -> dict:
        return {
            "device_id": self.cfg.device_id,
            "name": self.cfg.device_name,
            "device_type": self.cfg.device_type,
            "ip": self._my_ip(),
            "tcp_port": self.cfg.tcp_port,
            "udp_port": self.cfg.udp_port,
            "proto": PROTO_VERSION,
            "enc": 1 if self._local_encrypted() else 0,
            "ts": _now_ms(),
        }

    @staticmethod
    def _peer_to_info(peer: Peer) -> dict:
        return {
            "device_id": peer.device_id,
            "name": peer.name,
            "device_type": peer.device_type,
            "ip": peer.ip,
            "tcp_port": peer.tcp_port,
            "udp_port": peer.udp_port,
            "proto": peer.proto,
            "enc": 1 if peer.encrypted else 0,
            "ts": _now_ms(),
        }

    def _peer_list(self) -> List[dict]:
        seen = {self.cfg.device_id}
        out: List[dict] = []
        for peer in self.discovery.snapshot():
            if peer.device_id in seen:
                continue
            seen.add(peer.device_id)
            out.append(self._peer_to_info(peer))
        with self._lock:
            for info in self._gw_clients.values():
                did = info.get("device_id")
                if did and did not in seen:
                    seen.add(did)
                    out.append(info)
        return out

    # ------------------------------------------------------------- 客户端角色
    def _probe_loop(self) -> None:
        while not self._stop.is_set():
            try:
                for ip in list(self._gateways):
                    self._send("gw_hello", {}, ip)
            except Exception:
                pass
            self._stop.wait(GATEWAY_PROBE_INTERVAL)

    # ------------------------------------------------------------- 处理入口
    def handle(self, env: dict, addr: tuple) -> None:
        if not self.enabled:
            return
        mtype = env.get("type")
        data = env.get("data") or {}
        src_ip = addr[0]
        if mtype == "gw_hello":
            self._on_gw_hello(src_ip)
        elif mtype == "gw_ack":
            self._on_gw_ack(data, src_ip)
        elif mtype == "gw_register":
            self._on_gw_register(data, src_ip)
        elif mtype == "gw_peer_list":
            self._on_gw_peer_list(data)
        elif mtype == "gw_goodbye":
            self._on_gw_goodbye(src_ip)

    def _on_gw_hello(self, src_ip: str) -> None:
        # 网关角色：告知对方这里有大司农主机
        self._send("gw_ack", {"host": self._device_info()}, src_ip)

    def _on_gw_ack(self, data: dict, src_ip: str) -> None:
        # 客户端角色：向网关注册自己
        now = time.time()
        if now - self._last_register.get(src_ip, 0.0) < _REGISTER_MIN_INTERVAL:
            return
        self._last_register[src_ip] = now
        self._send("gw_register", {"device": self._device_info()}, src_ip)

    def _on_gw_register(self, data: dict, src_ip: str) -> None:
        info = data.get("device")
        if not isinstance(info, dict):
            return
        did = info.get("device_id")
        if not did or did == self.cfg.device_id:
            return
        info = dict(info)
        info.setdefault("ip", src_ip)
        with self._lock:
            self._gw_clients[src_ip] = info
        # 也注入本地发现表
        self._inject(info)
        # 回推设备列表给新客户端，并主动推送给其他客户端
        self._send("gw_peer_list", {"peers": self._peer_list()}, src_ip)
        self._push_peer_list(exclude=src_ip)

    def _on_gw_peer_list(self, data: dict) -> None:
        peers = data.get("peers")
        if not isinstance(peers, list):
            return
        for info in peers:
            if isinstance(info, dict):
                self._inject(info)

    def _on_gw_goodbye(self, src_ip: str) -> None:
        with self._lock:
            self._gw_clients.pop(src_ip, None)
        self._push_peer_list()

    def _push_peer_list(self, exclude: Optional[str] = None) -> None:
        with self._lock:
            targets = [ip for ip in self._gw_clients if ip != exclude]
        if not targets:
            return
        payload = {"peers": self._peer_list()}
        for ip in targets:
            self._send("gw_peer_list", payload, ip)

    def _inject(self, info: dict) -> None:
        did = info.get("device_id")
        ip = info.get("ip")
        if not did or not ip or did == self.cfg.device_id:
            return
        peer = Peer(
            device_id=did,
            name=info.get("name") or did[:8],
            ip=ip,
            tcp_port=int(info.get("tcp_port", 37021)),
            udp_port=int(info.get("udp_port", self.cfg.udp_port)),
            device_type=info.get("device_type", "windows"),
            proto=int(info.get("proto", 1)),
            encrypted=bool(info.get("enc")),
            state=PeerState.ONLINE,
        )
        self.discovery.register_peer(peer)

    def prune(self) -> None:
        """清理超时未刷新的网关客户端（由 NodeEngine 周期调用）。"""
        now = time.time()
        removed = False
        with self._lock:
            for ip, info in list(self._gw_clients.items()):
                ts = info.get("ts", 0) / 1000.0
                if ts and now - ts > PEER_TIMEOUT:
                    del self._gw_clients[ip]
                    removed = True
        if removed:
            self._push_peer_list()
