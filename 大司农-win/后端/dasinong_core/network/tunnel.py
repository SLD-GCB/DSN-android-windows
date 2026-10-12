"""加密隧道（dsn-tunnel **v4**）—— TLS + **每设备长期身份** + 应用层双向签名认证。

v3 的身份是**口令派生**（同口令 = 同身份、可互相冒充）。v4 换成**每设备随机身份**，
因此握手方式必须变：

  · **加密**：标准 TLS（`ssl` / OpenSSL）。**密码学一行不自己造。**
  · **两侧都不预知对端指纹**（对端身份事先不知道）→ TLS 层**不校验**证书链；
    真正的身份证明放在**应用层**：双方各自用自己的私钥对
    `方向|nonce|对端指纹` 签名，对方用**它声明的公钥**验签。
    · 把 nonce 纳入签名 → 防重放；
    · 把对端指纹纳入签名 → 防中继/降级。
  · **是否信任**由**配对表**决定（控制层在知道 `device_id` 后判定）——握手只负责
    "证明你是那把你自称的钥匙"。
  · 发现报文的签名也从 HMAC 改为**设备私钥签名**（口令没了）。

帧格式沿用 `protocol.frame`：`[len:4 大端][type:1][body]`，AUTH 用 4 个专用帧类型。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from ..protocol.frame import FrameType, ProtocolError, read_frame, recv_exact, write_frame
from ..security.identity import Identity, fingerprint_of

__all__ = [
    "MAGIC",
    "TunnelError",
    "Tunnel",
    "PeerAuth",
    "hello_fields",
    "verify_hello",
]

MAGIC = b"DSN1"

_AUTH_TIMEOUT = 20.0


class TunnelError(Exception):
    """隧道建立/握手失败（对端不是本协议、签名不对、连接中断等）。"""


# ---------------------------------------------------------------- 规范化字段串

def hello_fields(
    from_id: str, name: str, device_type: str,
    tcp_port: int, udp_port: int, ts: int,
) -> str:
    """发现报文签名的规范化字段串（**两端必须一致**，改动即不兼容）。"""
    return f"{from_id}|{name}|{device_type}|{tcp_port}|{udp_port}|{ts}"


def verify_hello(pub_b64: str, fields: str, sig_b64: str) -> bool:
    """用 hello 里声明的公钥验发现报文的签名。任何异常都当失败。"""
    try:
        pub_der = base64.b64decode(pub_b64)
        sig = base64.b64decode(sig_b64)
    except (ValueError, TypeError):
        return False
    return Identity.verify(pub_der, fields.encode("utf-8"), sig)


# ---------------------------------------------------------------- 帧收发小工具

def _send_json(sock: socket.socket, ftype: int, obj: dict) -> None:
    write_frame(sock, ftype, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _read_json(sock: socket.socket) -> Tuple[int, dict]:
    frame = read_frame(sock)
    if frame is None:
        raise TunnelError("隧道握手中连接被关闭")
    ftype, body = frame
    try:
        obj = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        obj = {}
    return ftype, obj


@dataclass
class PeerAuth:
    """一条**已完成身份认证**的连接。"""

    sock: socket.socket
    #: 已认证的对端公钥指纹（SPKI DER 的 SHA-256）
    fp: str
    #: 对端公钥 SPKI DER
    pub_der: bytes
    #: 本次握手的 nonce（原始字节；配对短码要用它）
    nonce: bytes


# ---------------------------------------------------------------- 握手（模块级，便于单测）

def handshake_server(sock: socket.socket, identity: Identity) -> PeerAuth:
    """服务端侧：发 magic + AUTH_HELLO{nonce}，验客户端签名，回 AUTH_RES。"""
    try:
        sock.sendall(MAGIC)
        nonce = os.urandom(16)
        nonce_hex = nonce.hex()
        # `fp` 是服务端**自报**的指纹：客户端把它纳入签名，服务端再核对自己是否真是它
        _send_json(sock, FrameType.AUTH_HELLO, {
            "nonce": nonce_hex, "fp": identity.fingerprint,
        })

        ftype, req = _read_json(sock)
        if ftype != FrameType.AUTH_REQ:
            raise TunnelError("隧道握手帧不对（预期 AUTH_REQ）")
        fp_c = str(req.get("fp") or "")
        pub_c = base64.b64decode(str(req.get("pub") or "") or "")
        sig_c = base64.b64decode(str(req.get("sig") or "") or "")
        if not fp_c or fingerprint_of_der(pub_c) != fp_c:
            raise TunnelError("对端公钥与声明指纹不符")
        payload = f"client|{nonce_hex}|{identity.fingerprint}".encode("utf-8")
        if not Identity.verify(pub_c, payload, sig_c):
            _send_json(sock, FrameType.AUTH_ERR, {"code": "auth", "错": "对方身份签名校验未通过"})
            raise TunnelError("对方身份签名校验未通过")

        payload_s = f"server|{nonce_hex}|{fp_c}".encode("utf-8")
        _send_json(sock, FrameType.AUTH_RES, {
            "fp": identity.fingerprint,
            "pub": base64.b64encode(identity.spki_der).decode("ascii"),
            "sig": base64.b64encode(identity.sign(payload_s)).decode("ascii"),
        })
        return PeerAuth(sock, fp_c, pub_c, nonce)
    except TunnelError:
        raise
    except (OSError, ProtocolError) as exc:
        raise TunnelError(f"隧道握手失败：{exc}") from exc


def handshake_client(sock: socket.socket, identity: Identity) -> PeerAuth:
    """客户端侧：收 magic + AUTH_HELLO，发 AUTH_REQ（带签名），验服务端签名。"""
    try:
        if recv_exact(sock, 4) != MAGIC:
            raise TunnelError("对面不是大司农隧道（magic 不对）")
        ftype, hello = _read_json(sock)
        if ftype != FrameType.AUTH_HELLO:
            raise TunnelError("隧道握手帧不对（预期 AUTH_HELLO）")
        nonce_hex = str(hello.get("nonce") or "")
        if not nonce_hex:
            raise TunnelError("隧道握手缺少 nonce")
        nonce = bytes.fromhex(nonce_hex)

        # 客户端签名覆盖：方向 + nonce + **服务端自报的指纹**
        # （服务端会核对自己是不是那个指纹；不一致即拒 → 防中继/降级）
        sfp = str(hello.get("fp") or "")
        payload = f"client|{nonce_hex}|{sfp}".encode("utf-8")
        _send_json(sock, FrameType.AUTH_REQ, {
            "fp": identity.fingerprint,
            "pub": base64.b64encode(identity.spki_der).decode("ascii"),
            "sig": base64.b64encode(identity.sign(payload)).decode("ascii"),
        })

        ftype, res = _read_json(sock)
        if ftype == FrameType.AUTH_ERR:
            raise TunnelError(str(res.get("错") or "对方拒绝了本次认证"))
        if ftype != FrameType.AUTH_RES:
            raise TunnelError("隧道握手帧不对（预期 AUTH_RES）")
        fp_s = str(res.get("fp") or "")
        pub_s = base64.b64decode(str(res.get("pub") or "") or "")
        sig_s = base64.b64decode(str(res.get("sig") or "") or "")
        if not fp_s or fingerprint_of_der(pub_s) != fp_s:
            raise TunnelError("对端公钥与声明指纹不符")
        if sfp and sfp != fp_s:
            raise TunnelError("对端身份与它自己声明的不一致（疑似中间人）")
        payload_s = f"server|{nonce_hex}|{identity.fingerprint}".encode("utf-8")
        if not Identity.verify(pub_s, payload_s, sig_s):
            raise TunnelError("对方身份签名校验未通过")
        return PeerAuth(sock, fp_s, pub_s, nonce)
    except TunnelError:
        raise
    except (OSError, ProtocolError, ValueError) as exc:
        raise TunnelError(f"隧道握手失败：{exc}") from exc


def fingerprint_of_der(spki_der: bytes) -> str:
    """SPKI DER → 指纹（与 `Identity.fingerprint` 同一算法）。"""
    import hashlib

    return hashlib.sha256(spki_der).hexdigest()


# ---------------------------------------------------------------- 隧道上下文

class Tunnel:
    """持有本机身份与 TLS 上下文，可被多线程复用。

    v4 起 `enabled` 恒为 True（引擎在没身份时不启动网络，见 `NodeEngine.start`）。
    """

    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.enabled = True
        self._lock = threading.Lock()
        self._cert_dir: Optional[str] = None
        self._server_ctx: Optional[ssl.SSLContext] = None
        self._client_ctx: Optional[ssl.SSLContext] = None
        # load_cert_chain 需要文件路径 → 把自签证书/私钥写进临时目录
        self._cert_dir = tempfile.mkdtemp(prefix="dasinong-cert-")
        self._cert_path = os.path.join(self._cert_dir, "cert.pem")
        self._key_path = os.path.join(self._cert_dir, "key.pem")
        with open(self._cert_path, "wb") as f:
            f.write(identity.cert_pem())
        with open(self._key_path, "wb") as f:
            f.write(identity.private_pem())

    # ---- 对外信息 ----
    @property
    def fingerprint(self) -> str:
        """本机身份指纹。⚠ 不外显（用户硬要求：界面不显示本机指纹）。"""
        return self.identity.fingerprint

    @property
    def spki_b64(self) -> str:
        return base64.b64encode(self.identity.spki_der).decode("ascii")

    # ---- 发现报文签名 ----
    def sign_hello(self, fields: str) -> str:
        """用设备私钥签发现报文的规范化字段串（base64 的 DER 签名）。"""
        return base64.b64encode(self.identity.sign(fields.encode("utf-8"))).decode("ascii")

    # ---- TLS 上下文（懒建）----
    def server_context(self) -> ssl.SSLContext:
        with self._lock:
            if self._server_ctx is None:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                # 优先 TLS1.3；允许 1.2 回退（Android 26~28 最高只到 1.2）
                try:
                    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                except (AttributeError, ValueError):
                    pass
                ctx.load_cert_chain(self._cert_path, self._key_path)
                self._server_ctx = ctx
            return self._server_ctx

    def client_context(self) -> ssl.SSLContext:
        with self._lock:
            if self._client_ctx is None:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                try:
                    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                except (AttributeError, ValueError):
                    pass
                # 对端指纹事先不知道 → 不校验证书链（身份交给应用层签名证明，见握手）
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self._client_ctx = ctx
            return self._client_ctx

    # ---- 包装连接 ----
    def accept(self, raw: socket.socket) -> PeerAuth:
        """服务端侧：TLS + 双向签名握手，返回**已认证**的对端信息。"""
        try:
            raw.settimeout(_AUTH_TIMEOUT)
            tls = self.server_context().wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            try:
                raw.close()
            except OSError:
                pass
            raise TunnelError(f"TLS 握手失败：{exc}") from exc
        try:
            auth = handshake_server(tls, self.identity)
        except Exception:
            try:
                tls.close()
            except OSError:
                pass
            raise
        try:
            tls.settimeout(None)
        except OSError:
            pass
        return PeerAuth(tls, auth.fp, auth.pub_der, auth.nonce)

    def connect(self, raw: socket.socket, host: str = "") -> PeerAuth:
        """客户端侧：TLS + 双向签名握手，返回**已认证**的对端信息。

        不覆盖调用方设的 socket 超时（直连要靠它做短超时），握手完再清掉。
        """
        try:
            tls = self.client_context().wrap_socket(raw, server_hostname=None)
        except (ssl.SSLError, OSError) as exc:
            try:
                raw.close()
            except OSError:
                pass
            raise TunnelError(f"TLS 握手失败：{exc}") from exc
        try:
            auth = handshake_client(tls, self.identity)
        except Exception:
            try:
                tls.close()
            except OSError:
                pass
            raise
        try:
            tls.settimeout(None)
        except OSError:
            pass
        return PeerAuth(tls, auth.fp, auth.pub_der, auth.nonce)

    def close(self) -> None:
        import shutil

        if self._cert_dir:
            shutil.rmtree(self._cert_dir, ignore_errors=True)
            self._cert_dir = None
