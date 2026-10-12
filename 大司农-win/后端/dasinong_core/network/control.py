"""TCP 传输层：服务端 + 控制连接管理 + 数据连接分发。

- 监听 TCP 端口；新连接的**首帧**决定类型：
  * JSON `connect`  -> 控制连接（长连接，JSON 报文）
  * `DATA_HELLO`    -> 数据连接（交给 transfer 层处理）
- 每对设备只保留一条控制连接：保留**发起方 == 字典序较小 device_id** 的那条，避免重复建连。
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable, Optional

from ..config import Config
from ..events import EventBus, EventType
from ..protocol import messages
from ..protocol.constants import CONNECT_TIMEOUT, PROTO_VERSION
from ..protocol.frame import (
    FrameType,
    ProtocolError,
    read_frame,
    write_frame,
)

ControlHandler = Callable[["ControlSession", dict], None]
DataHandler = Callable[[socket.socket, int, bytes, tuple], None]
TunnelProvider = Callable[[], Optional[object]]
#: 直连时被叫方学到拨号方身份的钩子：(identity_dict, 来源IP) -> None
LearnPeerHandler = Callable[[dict, str], None]


class ControlSession:
    """一条控制连接。"""

    def __init__(
        self,
        cfg: Config,
        sock: socket.socket,
        peer_id: str,
        is_outgoing: bool,
        on_message: ControlHandler,
        on_close: Callable[["ControlSession"], None],
        peer_fp: str = "",
        peer_spki: bytes = b"",
        peer_nonce: bytes = b"",
    ) -> None:
        self.cfg = cfg
        self.sock = sock
        self.peer_id = peer_id
        self.is_outgoing = is_outgoing
        self.on_message = on_message
        self.on_close = on_close
        #: 隧道握手阶段**已认证**的对端身份（v4：配对判定的依据）
        self.peer_fp = peer_fp
        self.peer_spki = peer_spki
        self.peer_nonce = peer_nonce
        #: 本会话是否已完成配对（由 NodeEngine 标注）
        self.paired = False
        #: 配对协商中：本端用户是否已同意 / 对端是否已同意
        self.pair_own_ok = False
        self.pair_peer_ok = False

        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_seen = time.time()
        self.last_pong = time.time()
        self._closed = False

    @property
    def alive(self) -> bool:
        return not self._closed and not self._stop.is_set()

    def remote_ip(self) -> Optional[str]:
        """这条连接对端的**当前** IP。

        数据连接就该往这里发 —— 而不是查发现表/记忆地址：手机当热点时发不出广播，
        只有这条活着的连接能告诉我们对方现在在哪。
        """
        if self._closed:
            return None
        try:
            addr = self.sock.getpeername()
        except OSError:
            return None
        return addr[0] if addr else None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._read_loop, name=f"control-{self.peer_id[:8]}", daemon=True
        )
        self._thread.start()

    def send(self, env: dict) -> bool:
        payload = messages.encode(env)
        try:
            with self._send_lock:
                write_frame(self.sock, FrameType.JSON, payload)
            return True
        except (OSError, ProtocolError):
            self.close()
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        try:
            self.on_close(self)
        except Exception:
            pass

    def _read_loop(self) -> None:
        try:
            while not self._stop.is_set():
                frame = read_frame(self.sock)
                if frame is None:
                    break
                ftype, body = frame
                if ftype != FrameType.JSON:
                    continue  # 控制连接上的非 JSON 帧一律忽略
                env = messages.decode(body)
                if env is None:
                    continue
                self.last_seen = time.time()
                try:
                    self.on_message(self, env)
                except Exception:
                    pass
        except (OSError, ProtocolError):
            pass
        finally:
            self.close()


class TransportManager:
    def __init__(
        self,
        cfg: Config,
        bus: EventBus,
        on_control: ControlHandler,
        on_data: DataHandler,
        tunnel_provider: Optional[TunnelProvider] = None,
        on_learn_peer: Optional[LearnPeerHandler] = None,
    ) -> None:
        self.cfg = cfg
        self.bus = bus
        self.on_control = on_control
        self.on_data = on_data
        self._tunnel_provider = tunnel_provider
        self.on_learn_peer = on_learn_peer

        self._sessions: dict[str, ControlSession] = {}
        self._lock = threading.RLock()
        self._server: Optional[socket.socket] = None
        self._stop = threading.Event()
        # 控制连接**端口池**（v5）：绑上几个算几个，主口必须成功
        self._servers: list = []
        self._accept_threads: list = []
        #: 接口池分配表：peer_id -> (监听socket, port)
        self._peer_ports: dict = {}

    #: 控制连接**接口池**大小（v5）：池里共这么多口，设备接入就从池里分配一个。
    _PORT_POOL_SIZE = 8

    def _acquire_pool_port(self):
        """从接口池里**按需**取一个空闲口并开始监听；池空返回 None。

        ⚠ 关键：**不设 SO_REUSEADDR**。
        设了的话，别人占着这个口我们也能"绑成功" → 两条连接串台（踩过一次，挂 5 条用例）。
        不设则绑不上就换下一个，**永不串台**。

        跳过主口（`tcp_port`）——它由 `start()` 常驻，负责"第一次被找到"。
        """
        for i in range(1, self._PORT_POOL_SIZE):
            port = self.cfg.tcp_port + i
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                srv.bind(("", port))
            except OSError:
                srv.close()
                continue                      # 被占/不可用 → 换下一个
            srv.listen(16)
            srv.settimeout(1.0)
            self._servers.append(srv)
            t = threading.Thread(
                target=self._accept_loop, args=(srv,), name=f"tcp-accept-{port}", daemon=True
            )
            self._accept_threads.append(t)
            t.start()
            return srv, port
        return None, None

    def _assign_pool_port(self, peer_id: str) -> None:
        """接口池：给这**一台设备**分配一个专属口（已有则复用；池空则回落主口，不分配）。"""
        if peer_id in self._peer_ports:
            return
        srv, port = self._acquire_pool_port()
        if srv is None:
            return                       # 池满 → 回落主口
        self._peer_ports[peer_id] = (srv, port)

    def _release_pool_port_for(self, peer_id: str) -> None:
        """设备离线 → 归还它那个口（回到池里给别的设备用）。"""
        ent = self._peer_ports.pop(peer_id, None)
        if ent is not None:
            self._release_pool_port(ent[0])

    def _release_pool_port(self, srv) -> None:
        """归还池里的一个口：关掉监听并移除（accept 线程会因 OSError 自行退出）。"""
        try:
            srv.close()
        except OSError:
            pass
        if srv in self._servers:
            self._servers.remove(srv)

    # ------------------------------------------------------------- 服务端
    def start(self) -> None:
        # ⚠ 端口池**不能用"连续端口 +N"**：多实例/多测试的端口本来就挨着，
        # A 的备用口会正好是 B 的主口，加上 SO_REUSEADDR 会串台（踩过一次）。
        # 正确做法：**按需在临时端口（bind 端口 0）上再开监听**，把 OS 分配到的真实端口通告出去。
        # 见任务 #93；本轮先保持单口（行为不变）。
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", self.cfg.tcp_port))
        srv.listen(16)
        srv.settimeout(1.0)
        self._servers = [srv]
        self._server = srv
        t = threading.Thread(
            target=self._accept_loop, args=(srv,), name="tcp-accept", daemon=True
        )
        self._accept_threads = [t]
        t.start()

    @property
    def listen_ports(self) -> list:
        """当前真正在监听的控制口列表（供端口池通告 `tcp_ports`）。"""
        return [x.getsockname()[1] for x in self._servers]

    def stop(self) -> None:
        self._stop.set()
        for srv in self._servers:
            try:
                srv.close()
            except OSError:
                pass
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.close()

    def _accept_loop(self, srv) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            threading.Thread(
                target=self._handle_incoming, args=(conn, addr), daemon=True
            ).start()

    def current_tunnel(self):
        """当前的隧道上下文；`None` 表示未接线（无加密场景）。

        ⚠ **刻意不吞异常**：provider 抛错说明"本机加密状态未知"，调用方必须
        fail-closed（拒绝/关闭）。以前这里 `except: return None`，等于把异常
        悄悄变成"没加密"，会让入站连接**按明文放行**。
        """
        if self._tunnel_provider is None:
            return None
        return self._tunnel_provider()

    def _handle_incoming(self, conn: socket.socket, addr: tuple) -> None:
        # 隧道：本机启用加密时，先包 TLS 并跑握手，之后才是业务首帧
        try:
            tunnel = self.current_tunnel()
        except Exception as exc:
            # 加密状态未知 → 拒绝，绝不退化成明文
            self.bus.publish(EventType.LOG, f"隧道不可用，拒绝入站连接（{addr[0]}）：{exc}")
            try:
                conn.close()
            except OSError:
                pass
            return
        peer_fp = ""
        peer_spki = b""
        peer_nonce = b""
        if tunnel is not None and getattr(tunnel, "enabled", False):
            try:
                auth = tunnel.accept(conn)
                conn = auth.sock
                peer_fp, peer_spki, peer_nonce = auth.fp, auth.pub_der, auth.nonce
            except Exception as exc:
                self.bus.publish(EventType.LOG, f"隧道握手失败（{addr[0]}）：{exc}")
                try:
                    conn.close()
                except OSError:
                    pass
                return
        try:
            frame = read_frame(conn)
        except (OSError, ProtocolError):
            conn.close()
            return
        if frame is None:
            conn.close()
            return
        ftype, body = frame
        if ftype == FrameType.JSON:
            env = messages.decode(body)
            if not env or env.get("type") != "connect":
                conn.close()
                return
            peer_id = env.get("from")
            if not peer_id or peer_id == self.cfg.device_id:
                conn.close()
                return
            # 版本闸门：对端协议版本不兼容 → 回一条可读错误后断开（不建会话）
            data = env.get("data") or {}
            remote_proto = int(env.get("proto") or 0)
            if remote_proto and messages.negotiate(
                remote_proto,
                int(data.get("proto_min") or remote_proto),
                int(data.get("proto_max") or remote_proto),
            ) < 0:
                try:
                    write_frame(
                        conn, FrameType.JSON,
                        messages.encode(messages.envelope(
                            "error", self.cfg.device_id, peer_id,
                            {"code": "proto", "message": "协议版本不一致，请把两端都升级到最新版"},
                        )),
                    )
                except (OSError, ProtocolError):
                    pass
                conn.close()
                return
            sess = ControlSession(
                self.cfg, conn, peer_id, is_outgoing=False,
                on_message=self.on_control, on_close=self._on_session_close,
                peer_fp=peer_fp, peer_spki=peer_spki, peer_nonce=peer_nonce,
            )
            self._register(sess)
            sess.start()
            # **任何**入站连接都要把拨号方登记下来（不只是「只知 IP 直连」那条路）：
            # 拨号方在 connect 里带了自己的身份，这里连 IP 一起记进发现表/端点缓存，
            # 否则被叫方虽然能收，却不知道对方在哪 —— 数据连接就发不回去。
            # 热点场景下手机发不出广播，这是被叫方唯一的信息来源。
            if self.on_learn_peer is not None:
                info = dict(env.get("data") or {})
                info.setdefault("device_id", peer_id)
                try:
                    self.on_learn_peer(info, addr[0])
                except Exception:
                    pass
            # 「只知 IP 的直连」再多回一条身份应答（拨号方靠它知道我们是谁）。
            if (env.get("data") or {}).get("ip_only"):
                sess.send(messages.envelope(
                    "connect_ok", self.cfg.device_id, peer_id, self._identity(peer_id),
                ))
        elif ftype == FrameType.DATA_HELLO:
            try:
                self.on_data(conn, ftype, body, addr)
            except Exception:
                try:
                    conn.close()
                except OSError:
                    pass
        else:
            conn.close()

    # ------------------------------------------------------------- 客户端
    def connect_control(self, ip: str, peer_id: str, port: int | None = None) -> Optional[ControlSession]:
        port = port or self.cfg.tcp_port
        try:
            sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
        except OSError:
            return None
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        # 隧道：本机启用加密时先包 TLS + 握手，握手通过才发业务首帧
        try:
            tunnel = self.current_tunnel()
        except Exception as exc:
            try:
                sock.close()
            except OSError:
                pass
            self.bus.publish(EventType.LOG, f"本机加密状态异常，取消连接：{exc}")
            return None
        peer_fp = ""
        peer_spki = b""
        peer_nonce = b""
        if tunnel is not None and getattr(tunnel, "enabled", False):
            try:
                auth = tunnel.connect(sock, ip)
                sock = auth.sock
                peer_fp, peer_spki, peer_nonce = auth.fp, auth.pub_der, auth.nonce
            except Exception as exc:
                try:
                    sock.close()
                except OSError:
                    pass
                self.bus.publish(EventType.LOG, f"与 {peer_id[:8]} 建立加密隧道失败：{exc}")
                return None
        sess = ControlSession(
            self.cfg, sock, peer_id, is_outgoing=True,
            on_message=self.on_control, on_close=self._on_session_close,
            peer_fp=peer_fp, peer_spki=peer_spki, peer_nonce=peer_nonce,
        )
        # 握手：**总是**带上自己的身份（device_id/名字/端口）。
        # 老版本读不懂就忽略多余字段（协议里规定未知字段要忽略），不影响兼容；
        # 但这样任何一次入站连接，被叫方都能立刻知道拨号方"现在"的地址和端口 ——
        # 数据连接才有地方可发（热点下 UDP 发现是废的）。
        data = {"role": "control", **self._identity()}
        hello = messages.envelope("connect", self.cfg.device_id, peer_id, data)
        try:
            write_frame(sock, FrameType.JSON, messages.encode(hello))
        except (OSError, ProtocolError):
            sess.close()
            return None
        self._register(sess)
        sess.start()
        return sess

    def _identity(self, peer_id: Optional[str] = None) -> dict:
        """回给拨号方的自我介绍（直连用）。

        `tcp_ports`：接口池**给这台设备分配的口**（有就只给这一个，让它走专属口）；
        没分配（池满/首次）则给主口 `tcp_port`。
        """
        ent = self._peer_ports.get(peer_id) if peer_id else None
        _ports = [ent[1]] if ent else [self.cfg.tcp_port]
        return {
            "device_id": self.cfg.device_id,
            "name": self.cfg.device_name,
            "device_type": self.cfg.device_type,
            "tcp_port": self.cfg.tcp_port,
            "udp_port": self.cfg.udp_port,
            "proto": PROTO_VERSION,
            # 端口池（v5，**当前只通告不启用**）：对端没有这两个字段时退回单口 tcp_port
            "tcp_ports": _ports,
            "data_ports": _ports,
        }

    def connect_direct(
        self, ip: str, port: Optional[int] = None, timeout: float = 6.0
    ) -> tuple:
        """只知 IP 的直连：TCP（+隧道）→ 发 `connect{ip_only}` → 读 `connect_ok` → 建会话。

        返回 `(session, identity_dict, error_text)`。
        为什么要**先读应答再建会话**：拨号方此时还不知道对方 device_id，
        而会话是按 device_id 分键的；同步读到应答就省掉了「建完再改名」的竞态。
        """
        port = port or self.cfg.tcp_port
        try:
            sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
        except OSError as exc:
            return None, None, f"连不上 {ip}:{port}（{exc}）"
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            tunnel = self.current_tunnel()
        except Exception as exc:
            try:
                sock.close()
            except OSError:
                pass
            return None, None, f"本机加密状态异常，已取消直连：{exc}"
        peer_fp = ""
        peer_spki = b""
        peer_nonce = b""
        if tunnel is not None and getattr(tunnel, "enabled", False):
            try:
                # 直连要求**短超时**：TLS 与握手都由这个 timeout 兜住（握手完隧道会清掉它）
                sock.settimeout(timeout)
                auth = tunnel.connect(sock, ip)
                sock = auth.sock
                peer_fp, peer_spki, peer_nonce = auth.fp, auth.pub_der, auth.nonce
            except Exception as exc:
                try:
                    sock.close()
                except OSError:
                    pass
                # 隧道/身份认证失败的原因必须上抛给 UI
                return None, None, str(exc)

        hello = messages.envelope(
            "connect", self.cfg.device_id, None,
            {"role": "control", "ip_only": True, **self._identity()},
        )
        try:
            write_frame(sock, FrameType.JSON, messages.encode(hello))
            # 只在读应答这一小段用超时；拿到应答后恢复阻塞，交给读循环
            sock.settimeout(timeout)
            frame = read_frame(sock)
        except (OSError, ProtocolError):
            try:
                sock.close()
            except OSError:
                pass
            return None, None, "对方没有回应直连（可能是旧版本，请改用扫描发现）"
        if frame is None:
            try:
                sock.close()
            except OSError:
                pass
            return None, None, "对方关闭了连接"
        ftype, body = frame
        env = messages.decode(body) if ftype == FrameType.JSON else None
        if not env or env.get("type") != "connect_ok":
            try:
                sock.close()
            except OSError:
                pass
            return None, None, "对方不支持直连（版本较旧）"
        info = env.get("data") or {}
        peer_id = info.get("device_id")
        if not peer_id or peer_id == self.cfg.device_id:
            try:
                sock.close()
            except OSError:
                pass
            return None, None, "对方返回的身份无效"
        sock.settimeout(None)
        sess = ControlSession(
            self.cfg, sock, peer_id, is_outgoing=True,
            on_message=self.on_control, on_close=self._on_session_close,
            peer_fp=peer_fp, peer_spki=peer_spki, peer_nonce=peer_nonce,
        )
        self._register(sess)
        sess.start()
        return sess, info, None

    # ------------------------------------------------------------- 会话管理
    def _register(self, sess: ControlSession) -> None:
        """注册控制会话：每个 peer 只保留一条。

        两端同时互连时保留 canon 方向（发起方 == 较小 device_id）；但在对端从未连过来的
        情况下**允许非 canon 方向先建立**（应对单向防火墙/路由），canon 连接一旦到达再替换。
        """
        canon_outgoing = self.cfg.device_id < sess.peer_id
        with self._lock:
            existing = self._sessions.get(sess.peer_id)
            if existing is not None and existing.alive:
                if sess.is_outgoing == canon_outgoing and existing.is_outgoing != canon_outgoing:
                    existing.close()   # 新连接是 canon，替换旧的非 canon 连接
                else:
                    sess.close()       # 否则丢弃新连接
                    return
            self._sessions[sess.peer_id] = sess
        # 接口池：这台设备接入 → 从池里给它分一个专属口（池空则回落主口）
        self._assign_pool_port(sess.peer_id)

    def _on_session_close(self, sess: ControlSession) -> None:
        with self._lock:
            if self._sessions.get(sess.peer_id) is sess:
                del self._sessions[sess.peer_id]
        # 设备离线 → 归还专属口
        self._release_pool_port_for(sess.peer_id)

    def get_session(self, peer_id: str) -> Optional[ControlSession]:
        with self._lock:
            sess = self._sessions.get(peer_id)
            return sess if sess is not None and sess.alive else None

    @property
    def is_listening(self) -> bool:
        """TCP 服务端是否在监听（用于诊断）。"""
        return self._server is not None and not self._stop.is_set()

    def sessions(self) -> list[ControlSession]:
        with self._lock:
            return [s for s in self._sessions.values() if s.alive]

    def send_to(self, peer_id: str, env: dict) -> bool:
        sess = self.get_session(peer_id)
        if sess is None:
            return False
        return sess.send(env)
