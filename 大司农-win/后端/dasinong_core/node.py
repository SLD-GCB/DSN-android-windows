"""NodeEngine：编排发现 / 网关中继 / 控制连接 / 传输，对外暴露高层 API。

UI 只与本类交互；事件通过 EventBus 发布。
"""
from __future__ import annotations

import json
import random
import threading
import os
import time
import uuid
from dataclasses import replace
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config, load_prefs, save_prefs

#: 「保持长连接」模式：心跳更密、容忍更久的无响应（空闲也不剪会话）
KEEPALIVE_PING = 2.0
KEEPALIVE_TIMEOUT = 60.0
from .events import EventBus, EventType
from .models import ChatMessage, FileEntry, MsgKind, Peer, PeerState, Transfer, TransferState
from .network.tunnel import Tunnel
from .network.control import ControlSession, TransportManager
from .network.discovery import DiscoveryService
from .network.gateway_rendezvous import GatewayRendezvous
from .network.transfer import TransferManager
from .pairing import PairingManager, sas_code
from .protocol import messages
from .security import SecretStore, get_secret_store
from .security.identity import KEY_FILE, load_or_create
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

        # 敏感数据的静态加密存储（Windows=DPAPI；其它平台降级为明文）
        self._secret: SecretStore = get_secret_store()

        # 本机**长期身份**（v4：每设备随机，不再是口令派生）。
        # 原身份解不开（换机/清数据/换 Windows 用户）时会生成新的 —— 对端需重新配对。
        identity_path = Path(cfg.data_dir) / KEY_FILE
        had_identity = identity_path.exists()
        self.identity, identity_reused = load_or_create(cfg.data_dir, self._secret)
        self.tunnel = Tunnel(self.identity)
        if had_identity and not identity_reused:
            self.bus.publish(
                EventType.LOG, "本机身份已重新生成（原身份丢失），对端需要重新配对"
            )

        # 配对：首次连接要人工确认（两端显示同一个 6 位短码），之后长期信任
        self.pairing = PairingManager(self.bus)

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
        #: 「TCP 并发」（1~8，落盘）
        self._concurrency = max(1, min(8, int(load_prefs().get("tcp_concurrency") or 3)))
        #: 连接闲置多久自动关闭（分钟，落盘）
        self._conn_timeout_min = int(load_prefs().get("conn_timeout_min") or 10)
        #: 「保持长连接」开关（落盘，重启后保持）
        self._keep_alive = load_prefs().get("keep_alive") == "1"
        #: 探网关是否正在进行（防重入叠加）
        self._gw_probing = False
        #: 上次「直连网关做发现」的时间（节流）。
        #: ⚠ 初值取「现在」：启动后先等一个间隔再探，避免刚起来就与控制连接抢 transport。
        self._gw_discover_at = time.time()
        self._maintenance_thread: Optional[threading.Thread] = None

        self._seen_text: "OrderedDict[str, bool]" = OrderedDict()
        self._backoff_until: Dict[str, float] = {}
        self._endpoints: Dict[str, tuple] = {}   # device_id -> (Peer, 最近见到的时间)
        # 记住「连上过」的对端地址（落盘）：手机开热点时 UDP 发现不到，
        # 下次启动可以直接拨回去，不必每次手输 IP。
        self._known_peers: Dict[str, dict] = self._load_known_peers()
        self._attempts: Dict[str, int] = {}
        #: peer_id -> 上次"配对没成"的时间。防止「拒绝→断线→重连→再问」形成重连风暴。
        #: 配对失败过的对端：不再**自动**发起配对，等用户手动点「配对」。
        #: （只做时间冷却不够 —— 冷却一过又会自动重连→重问，表现为"疯狂发连接请求"。）
        self._pair_blocked: set = set()
        self._no_session_since: Dict[str, float] = {}
        self._last_ping: Dict[str, float] = {}
        self._outbox: List[tuple] = []
        self._outbox_lock = threading.Lock()
        self._chat_notified: set = set()   # 已往会话里补过消息的接收文件
        self._state_lock = threading.RLock()
        self.start_errors: List[str] = []

    # ------------------------------------------------------------- 生命周期
    def start(self) -> None:
        """启动各子系统。

        **强制加密**：没设口令就**什么都不启**（不监听、不广播）——否则等于把明文
        服务默认暴露在同网段里。用户在「设置」里设好口令后重启引擎即可。
        """
        self.start_errors = []
        if not self.is_encrypted():
            msg = (
                "未设置隧道口令 —— 为安全起见已停用网络功能。"
                "请在「设置 → 加密隧道口令」里设置口令后重试。"
            )
            self.start_errors.append(msg)
            self.bus.publish(EventType.LOG, msg)
            return
        # 逐个启动，任何一个失败都不影响其余子系统（否则会表现为
        # 「能发现设备但永远建不起 TCP 连接」）。
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
        # 启动即把设置里的「TCP 并发」写进传输引擎
        self.transfer.parallel = self._concurrency
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
        """手动添加/连接一个 IP：**优先 socket 直连（TCP）**，UDP 只作补充。

        为什么先走 TCP：手机当热点主机时它的 UDP 广播发不出来，等 UDP 回包会永远等不到；
        TCP 直连不依赖广播，热点下才连得上。连上即把对端登记进发现表（附近页随之出现）。
        """
        clean = (ip or "").strip()
        if not clean:
            return
        self.discovery.add_manual_target(clean)     # 同网段普通 WiFi：UDP 探测仍有用
        self.gateway.add_candidate(clean)
        # socket 直连放后台线程，别卡住界面
        threading.Thread(
            target=self._direct_connect_to, args=(clean,), name="manual-direct", daemon=True
        ).start()


    #: 「直连网关做发现」的重试间隔（秒）。
    _GW_DISCOVER_INTERVAL = 8.0

    def _auto_discover_gateway(self, now: float) -> None:
        """**自动发现**兜底：TCP 直连默认网关（热点主机）把对方登记进发现表。

        为什么需要：热点主机（手机）的 UDP 广播发不出来（安卓 `candidateLans()` 只认
        WIFI/ETHERNET，热点网卡被过滤）→ 客户端（电脑）永远收不到 → 附近列表空。
        这里让客户端**主动 TCP 直连网关**问一句"你是谁"，连上即登记 → 附近列表出现。

        ⚠ **只做发现**：不自动配对、不自动触发配对请求；连不连、配不配仍由用户手动点。
        """
        if self.discovery.snapshot():
            return                              # 已经发现有设备了，不必再探
        if self.transport.sessions():
            return                              # 已有会话，不必再探
        if self._gw_probing:
            return                              # 上一次还在探 → 别叠加（防重入/刷屏）
        if now - self._gw_discover_at < self._GW_DISCOVER_INTERVAL:
            return
        self._gw_discover_at = now
        self._gw_probing = True
        from .network import interfaces as ifaces_mod
        gw = ifaces_mod.default_gateway()
        if not gw:
            return
        def _probe() -> None:
            try:
                self._direct_connect_to(gw)
            finally:
                self._gw_probing = False

        threading.Thread(target=_probe, name="gw-discover", daemon=True).start()

    def _direct_connect_to(self, ip: str) -> bool:
        """**socket 直连**一个 IP（不依赖 UDP 广播）：连上就把对端登记进发现表。

        手动"添加 IP"与自动"发现网关"都走这一条。
        """
        try:
            sess, info, err = self.transport.connect_direct(ip, self.cfg.tcp_port, timeout=4.0)
        except Exception as exc:
            self.bus.publish(EventType.LOG, f"直连 {ip} 失败：{exc}")
            return False
        if sess is None:
            self.bus.publish(EventType.LOG, f"直连 {ip} 失败：{err or '无响应'}")
            return False
        info = info or {}
        did = str(info.get("device_id") or sess.peer_id or "")
        if not did or did == self.cfg.device_id:
            return False
        peer = Peer(
            device_id=did,
            name=info.get("name") or did[:8],
            ip=ip,
            tcp_port=int(info.get("tcp_port") or self.cfg.tcp_port),
            udp_port=int(info.get("udp_port") or self.cfg.udp_port),
            device_type=info.get("device_type") or "unknown",
            proto=int(info.get("proto") or 1),
            encrypted=True,
            state=PeerState.ONLINE,
        )
        self._remember_endpoint(peer)
        self._remember_peer(peer)
        try:
            self.discovery.register_peer(peer)
        except Exception:
            pass
        self.bus.publish(EventType.LOG, f"已直连 {peer.name}（{ip}）")
        return True

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

    # ------------------------------------------------------------- 身份 / 配对
    def fingerprint(self) -> str:
        """本机身份公钥指纹。⚠ **不要显示到界面上**（用户硬要求）。"""
        return self.identity.fingerprint

    def is_encrypted(self) -> bool:
        """v4 起恒为 True：身份是本机必需的，不存在"没加密"的运行模式。"""
        return True

    # ---- 配对表 ----
    def paired_devices(self) -> List[dict]:
        return self.store.pair_all() if self.store is not None else []

    def is_paired(self, peer_id: str) -> bool:
        if self.store is None:
            return False
        row = self.store.pair_get(peer_id)
        return bool(row) and bool(row.get("trusted"))

    def unpair(self, peer_id: str) -> None:
        """解绑：删掉信任记录并断开当前会话（下次连接需重新配对）。"""
        if self.store is not None:
            self.store.pair_remove(peer_id)
        for sess in list(self.transport.sessions()):
            if sess.peer_id == peer_id:
                try:
                    sess.close()
                except Exception:
                    pass
        self.bus.publish(EventType.PAIRED_CHANGED, {"peer_id": peer_id, "paired": False})
        self.bus.publish(EventType.LOG, f"已解绑 {self._peer_name(peer_id) or peer_id[:8]}")

    def answer_pairing(self, peer_id: str, accept: bool) -> None:
        """UI 回答「是否与这台设备配对」（配对弹窗的按钮回调）。"""
        self.pairing.answer(peer_id, accept)

    # ---- 配对流程：未配对时首次连接走一遍 ----
    def _pair_state(self, sess: ControlSession) -> bool:
        """懒判定该会话是否已配对；顺带发现"对方换了身份"。"""
        if self.store is None or not sess.peer_fp:
            return bool(sess.paired)
        row = self.store.pair_get(sess.peer_id)
        if row is None:
            return bool(sess.paired)
        if str(row.get("fp") or "") != sess.peer_fp:
            if not getattr(sess, "_identity_warned", False):
                sess._identity_warned = True
                self.bus.publish(
                    EventType.LOG, "对方身份已变更（可能重装/换机），需要重新配对"
                )
            sess.paired = False
            return False
        sess.paired = True
        return True

    def _begin_pairing(self, sess: ControlSession) -> None:
        """由**拨号方**发起配对：先发 `pair_request`，同时在后台问本端用户。

        只有**首次**（没失败过）才自动发起；一旦失败，该对端进入 `_pair_blocked`，
        不再自动询问，等用户在界面上手动点「配对」（`request_pairing`）。
        """
        if self._pair_state(sess):
            return
        if sess.peer_id in self._pair_blocked:
            return  # 之前配对失败过：不再自动发起，等用户手动
        try:
            sess.send(messages.envelope(
                "pair_request", self.cfg.device_id, sess.peer_id,
                {"name": self.cfg.device_name},
            ))
        except Exception:
            return
        sas = sas_code(sess.peer_fp, self.identity.fingerprint, sess.peer_nonce)
        name = self._peer_name(sess.peer_id) or sess.peer_id[:8]

        def worker() -> None:
            ok = self.pairing.ask(sess.peer_id, name, sas)
            try:
                sess.send(messages.envelope(
                    "pair_accept" if ok else "pair_reject", self.cfg.device_id, sess.peer_id, {}))
            except Exception:
                return
            sess.pair_own_ok = ok
            if ok:
                self._finish_pairing(sess, name)
            else:
                self._block_pairing(sess.peer_id)
                self.bus.publish(EventType.LOG, f"与 {name} 的配对未完成（可在「附近设备」里手动点「配对」）")

        threading.Thread(target=worker, name="pair-ask", daemon=True).start()

    def _on_pair_request(self, sess: ControlSession, env: dict) -> None:
        """被叫方收到配对请求：算短码 → 问本端用户 → 回 accept/reject。"""
        data = env.get("data") or {}
        name = str(data.get("name") or "") or (self._peer_name(sess.peer_id) or sess.peer_id[:8])
        sas = sas_code(sess.peer_fp, self.identity.fingerprint, sess.peer_nonce)
        ok = self.pairing.ask(sess.peer_id, name, sas)
        try:
            sess.send(messages.envelope(
                "pair_accept" if ok else "pair_reject", self.cfg.device_id, sess.peer_id, {}))
        except Exception:
            return
        sess.pair_own_ok = ok
        if ok:
            self._finish_pairing(sess, name)
        else:
            self.bus.publish(EventType.LOG, "已取消配对")

    def _block_pairing(self, peer_id: str) -> None:
        """配对没成：不再自动发起，并让维护循环慢下来（避免「拒绝→重连→再问」风暴）。"""
        self._pair_blocked.add(peer_id)
        self._backoff_until[peer_id] = time.time() + 30.0

    def request_pairing(self, peer_id: str) -> bool:
        """**手动**发起/重试配对（界面「配对」按钮调用）。

        清掉失败标记和退避，尽量在当前会话上立刻问；没有连接就先连一次。
        返回 False 表示暂时连不上该设备。
        """
        self._pair_blocked.discard(peer_id)
        self._backoff_until.pop(peer_id, None)
        sess = self.transport.get_session(peer_id)
        if sess is None:
            peer = self.discovery.get(peer_id)
            if peer is not None:
                try:
                    self.transport.connect_control(peer.ip, peer_id, peer.tcp_port)
                except Exception:
                    pass
                sess = self.transport.get_session(peer_id)
        if sess is None:
            return False
        self._begin_pairing(sess)
        return True

    def _finish_pairing(self, sess: ControlSession, name: str = "") -> None:
        """**两端都同意**后才落库（不产生"半信任"）。"""
        if sess.paired or not (sess.pair_own_ok and sess.pair_peer_ok) or self.store is None:
            return
        label = name or self._peer_name(sess.peer_id) or sess.peer_id[:8]
        try:
            self.store.pair_upsert(sess.peer_id, sess.peer_fp, label)
        except Exception as exc:
            self.bus.publish(EventType.LOG, f"保存配对失败：{exc}")
            return
        sess.paired = True
        try:
            sess.send(messages.envelope(
                "pair_ok", self.cfg.device_id, sess.peer_id,
                {"fp": self.identity.fingerprint},
            ))
        except Exception:
            pass
        self.bus.publish(EventType.PAIRED_CHANGED, {"peer_id": sess.peer_id, "paired": True})
        self.bus.publish(EventType.LOG, f"已与 {label} 配对")

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
        # 注：v4 起没有"口令"要恢复了 —— 身份是每设备随机的，配对表在库里持久化。

    def _persist_transfer_event(self, event) -> None:
        if self.store is None or not isinstance(event.data, Transfer):
            return
        try:
            self.store.upsert_transfer(event.data)
        except Exception:
            pass

    def _on_transfer_for_chat(self, event) -> None:
        """传输完成 → 在会话里生成一条 file/image 消息。

        **收发两端都要生成**（和文本消息一样）：
        - 接收：用**落盘路径**（final_path），UI 能直接出缩略图；
        - 发送：用**本地原文件路径**（local_path），自己那一侧也要能看到自己发的。
        """
        t = event.data
        if not isinstance(t, Transfer) or t.state != TransferState.COMPLETED:
            return
        if t.direction not in ("send", "recv"):
            return
        f = t.file
        if f is None or f.file_id in self._chat_notified:
            return
        self._chat_notified.add(f.file_id)
        sending = t.direction == "send"
        entry = f if sending else replace(f, local_path=t.final_path or f.local_path)
        msg = ChatMessage(
            msg_id=str(uuid.uuid4()),
            conv_id=t.peer_id,
            kind=MsgKind.IMAGE if entry.kind == "image" else MsgKind.FILE,
            outgoing=sending,
            status="sent" if sending else "received",
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
                # 配对**手动**：连上 ≠ 配对，用户要配对时自己在界面上点「配对」。
                # 唯一例外：无头自测（DASINONG_PAIR_AUTO=1，真实运行绝不设置）没人点按钮，
                # 自动发起一次，否则集成用例永远配不上、业务帧全被拒。
                if os.environ.get("DASINONG_PAIR_AUTO") == "1":
                    s0 = self.transport.get_session(peer.device_id)
                    if s0 is not None:
                        try:
                            self._begin_pairing(s0)
                        except Exception:
                            pass
                return True, f"已连接 {peer.name}"
            return False, f"连不上 {peer.name}（{clean}）"
        sess, info, err = self.transport.connect_direct(clean, port, timeout)
        if sess is None:
            return False, err or "直连失败"
        info = info or {}
        encrypted = bool(self.tunnel is not None and getattr(self.tunnel, "enabled", False))
        # 接口池：对端在 connect_ok 里通告了**给本机的专属口**（tcp_ports）→ 记下来，
        # 之后本机发起的连接（数据连接/重连）都走这个口；没有则回落主口 tcp_port。
        _ports = info.get("tcp_ports") or []
        _dial_port = int(_ports[0]) if _ports else int(info.get("tcp_port") or self.cfg.tcp_port)
        peer = Peer(
            device_id=info.get("device_id") or sess.peer_id,
            name=info.get("name") or (info.get("device_id") or sess.peer_id)[:8],
            ip=clean,
            tcp_port=_dial_port,
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
        # 配对**手动**：直连只建立连接；要不要配对由用户自己点「配对」。
        # 例外同上：仅无头自测（DASINONG_PAIR_AUTO=1）才自动发起。
        if os.environ.get("DASINONG_PAIR_AUTO") == "1":
            try:
                self._begin_pairing(sess)
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
        # **按需补连**：发送方（发消息/发文件）要用连接但没有 → 立刻拨一次。
        # 这是"用户主动发送"触发的，不是后台循环，所以不会撞接口。
        if self.transport.get_session(peer_id) is None:
            peer = self.discovery.get(peer_id) or self.get_peer(peer_id)
            if peer is not None:
                try:
                    self.transport.connect_control(peer.ip, peer_id, peer.tcp_port)
                except Exception:
                    pass

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
        # ---- 配对协商（任何一侧都可能发）----
        if mtype == "pair_request":
            self._on_pair_request(sess, env)
            return
        if mtype == "pair_accept":
            sess.pair_peer_ok = True
            self._finish_pairing(sess)
            return
        if mtype == "pair_reject":
            self._block_pairing(peer_id)
            self.bus.publish(EventType.LOG, "对方拒绝了配对（可在「附近设备」里手动点「配对」重试）")
            return
        if mtype == "pair_ok":
            sess.pair_peer_ok = True
            self._finish_pairing(sess)
            return
        # ---- 保活（配对前也允许）----
        if mtype == "ping":
            sess.send(messages.envelope("pong", self.cfg.device_id, peer_id, {}))
            return
        if mtype == "pong":
            sess.last_pong = time.time()
            return
        # ---- 未配对闸门：除保活/配对外，业务帧一律拒绝 ----
        if not self._pair_state(sess):
            try:
                sess.send(messages.envelope(
                    "error", self.cfg.device_id, peer_id,
                    {"code": "unpaired", "message": "尚未配对：请先在对端确认配对"},
                ))
            except Exception:
                pass
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
            self._auto_discover_gateway(now)   # 自动发现（只登记，不配对）
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
            if getattr(peer, "incompatible", False):
                # 版本协商不通过：能看见但不拨号，避免白发连接与一堆失败日志
                continue
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
            # ⚠ **不做任何自动重连**：闲置关闭时长由设置页的「连接闲置多久后关闭」决定，
            # 「保持长连接」= 无限期不关。掉线后要连请手动点。
            if remember:
                self._remember_peer(peer)

    #: 「TCP 并发」：一次传输并发开几条数据连接（1~8，填 1 = 单连接）。落盘。
    def concurrency(self) -> int:
        return self._concurrency

    def set_concurrency(self, n: int) -> None:
        self._concurrency = max(1, min(8, int(n)))
        try:
            self.transfer.parallel = self._concurrency      # 立刻生效（并发发送数）
        except Exception:
            pass
        try:
            save_prefs(tcp_concurrency=str(self._concurrency))
        except Exception:
            pass
        self.bus.publish(EventType.LOG, f"TCP 并发已设为 {self._concurrency} 条")

    #: 连接闲置多久自动关闭（分钟）。「保持长连接」开着时无限期。
    def conn_timeout_minutes(self) -> int:
        return self._conn_timeout_min

    def set_conn_timeout_minutes(self, minutes: int) -> None:
        self._conn_timeout_min = max(1, int(minutes))
        try:
            save_prefs(conn_timeout_min=str(self._conn_timeout_min))
        except Exception:
            pass
        self.bus.publish(EventType.LOG, f"连接闲置 {self._conn_timeout_min} 分钟后自动关闭")

    def keep_alive(self) -> bool:
        """「保持长连接」是否开启。"""
        return self._keep_alive

    def set_keep_alive(self, on: bool) -> None:
        """开关「保持长连接」：更密心跳 + 空闲不断开（落盘，重启后保持）。"""
        self._keep_alive = bool(on)
        try:
            save_prefs(keep_alive="1" if on else "0")
        except Exception:
            pass
        self.bus.publish(EventType.LOG, "已开启「保持长连接」" if on else "已关闭「保持长连接」")
        if on:
            # 一次性：把已发现的设备各接一次（之后不再自动重连、也不会自动关）
            try:
                for peer in self.discovery.snapshot():
                    if self.transport.get_session(peer.device_id) is None:
                        try:
                            self.transport.connect_control(peer.ip, peer.device_id, peer.tcp_port)
                        except Exception:
                            pass
            except Exception:
                pass

    def _ping_sessions(self, now: float) -> None:
        # 心跳间隔：「保持长连接」开着时更密
        ping_interval = KEEPALIVE_PING if self._keep_alive else PING_INTERVAL
        # 闲置关闭：保持长连接 = **无限期**；否则用设置里的分钟数
        timeout = None if self._keep_alive else max(1, self._conn_timeout_min) * 60.0
        for sess in self.transport.sessions():
            if timeout is not None and now - sess.last_pong > timeout:
                sess.close()
                continue
            if now - self._last_ping.get(sess.peer_id, 0.0) >= ping_interval:
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
