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
    ) -> None:
        self.cfg = cfg
        self.sock = sock
        self.peer_id = peer_id
        self.is_outgoing = is_outgoing
        self.on_message = on_message
        self.on_close = on_close

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
        self._accept_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------- 服务端
    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("", self.cfg.tcp_port))
        srv.listen(16)
        srv.settimeout(1.0)
        self._server = srv
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="tcp-accept", daemon=True
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.close()

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._server.accept()
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
        """当前的隧道上下文（口令为空则 enabled=False，等价于明文）。"""
        if self._tunnel_provider is None:
            return None
        try:
            return self._tunnel_provider()
        except Exception:
            return None

    def _handle_incoming(self, conn: socket.socket, addr: tuple) -> None:
        # 隧道：口令非空时，先包 TLS 并跑握手，之后才是业务首帧
        tunnel = self.current_tunnel()
        if tunnel is not None and getattr(tunnel, "enabled", False):
            try:
                conn = tunnel.accept(conn)
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
            sess = ControlSession(
                self.cfg, conn, peer_id, is_outgoing=False,
                on_message=self.on_control, on_close=self._on_session_close,
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
                    "connect_ok", self.cfg.device_id, peer_id, self._identity(),
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
        # 隧道：口令非空时先包 TLS + 握手，握手通过才发业务首帧
        tunnel = self.current_tunnel()
        if tunnel is not None and getattr(tunnel, "enabled", False):
            try:
                sock = tunnel.connect(sock, ip)
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

    def _identity(self) -> dict:
        """回给拨号方的自我介绍（直连用）。"""
        return {
            "device_id": self.cfg.device_id,
            "name": self.cfg.device_name,
            "device_type": self.cfg.device_type,
            "tcp_port": self.cfg.tcp_port,
            "udp_port": self.cfg.udp_port,
            "proto": PROTO_VERSION,
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
        tunnel = self.current_tunnel()
        if tunnel is not None and getattr(tunnel, "enabled", False):
            try:
                sock = tunnel.connect(sock, ip)
            except Exception as exc:
                try:
                    sock.close()
                except OSError:
                    pass
                # 口令不一致时这里就是那句「对方身份对不上」；必须上抛给 UI
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

    def _on_session_close(self, sess: ControlSession) -> None:
        with self._lock:
            if self._sessions.get(sess.peer_id) is sess:
                del self._sessions[sess.peer_id]

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
