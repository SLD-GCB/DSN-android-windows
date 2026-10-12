"""加密隧道（dsn-tunnel v3）—— 身份由口令派生（两端相同）。

参照 honglu-tunnel v1（`ai酒馆/酒馆隧道.py` + `鸿胪寺/后端/隧道.kt`）的设计：

  · **加密**：标准 TLS（`ssl` / OpenSSL）。**密码学一行不自己造。**
  · **身份**：口令 --PBKDF2--> 32B 材料 --> P-256 私钥 --> 自签证书。
    两端用同一口令推出**同一把公钥**，本端**只钉这个指纹** —— 不知道口令的人
    推不出这把公钥，也就连不上、冒充不了。**不扫码、不比对、不手输指纹。**
  · **TLS 之上再加一道 HMAC 双向认证**：用于给出明确的「口令不一致」报错
    （TLS 层的报错信息含糊）。
  · **口令为空 = 不加密**：直接走明文 TCP，不套 TLS、不跑握手。

帧格式沿用 `protocol.frame`：`[len:4 大端][type:1][body]`，AUTH 用 4 个专用帧类型。

## 口令密钥（域分离）
`key(用途) = PBKDF2-HMAC-SHA256(口令, 盐 SALT + "|" + 用途, 200000, 32)`
  · 用途 `hello` → 发现报文签名。⚠ 广播是**明文**，必须走 PBKDF2——否则
    等于把"口令原文做 HMAC"暴露成可离线爆破的口令预言机。
  · 用途 `auth`  → AUTH 握手。
身份派生用的是不带用途后缀的 `PBKDF2(口令, 盐)`（保持 v3 的指纹向量不变）。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import shutil
import socket
import ssl
import tempfile
import threading
from typing import Optional, Tuple

from ..protocol.frame import FrameType, ProtocolError, read_frame, recv_exact, write_frame

__all__ = ["SALT", "ITERATIONS", "MAGIC", "TunnelError", "Tunnel", "derive_key", "key_for", "hello_fields"]

SALT = b"dasinong-tunnel-v1"
ITERATIONS = 200_000
MAGIC = b"DSN1"

_AUTH_TIMEOUT = 20.0

# P-256 曲线阶 n（派生标量时取模用）
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class TunnelError(Exception):
    """隧道层错误（口令不一致 / 对端不是本协议 / TLS 失败）。"""


# ---------------------------------------------------------------- 派生

def derive_key(passphrase: str) -> bytes:
    """口令 → 32 字节密钥材料（PBKDF2-HMAC-SHA256，**不带用途后缀**）。

    仅用于**身份派生**：两端同口令 → 同一把 P-256 密钥 → 同一指纹。
    """
    return hashlib.pbkdf2_hmac(
        "sha256", (passphrase or "").encode("utf-8"), SALT, ITERATIONS, 32
    )


def key_for(passphrase: str, purpose: bytes) -> bytes:
    """口令 → 某用途的 HMAC 密钥（域分离）。**两端必须一致。**"""
    return hashlib.pbkdf2_hmac(
        "sha256",
        (passphrase or "").encode("utf-8"),
        SALT + b"|" + purpose,
        ITERATIONS,
        32,
    )


def fingerprint_of(public_key) -> str:
    """公钥（SubjectPublicKeyInfo DER）的 SHA-256，小写十六进制。

    ⚠ 这是给两端"钉"的那串。别用**整张证书**的哈希——证书里有随机序列号和有效期，
    每次生成都不一样；只有**公钥**才是"由口令唯一决定、稳定"的。
    """
    from cryptography.hazmat.primitives import serialization

    spki = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(spki).hexdigest()


def _build_identity(passphrase: str):
    """口令 → (私钥, 证书PEM, 私钥PEM, 指纹)。P-256 自签。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    material = derive_key(passphrase)
    d = (int.from_bytes(material, "big") % (_P256_ORDER - 1)) + 1
    key = ec.derive_private_key(d, ec.SECP256R1())

    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dasinong-tunnel")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key, cert_pem, key_pem, fingerprint_of(key.public_key())


def hello_fields(from_id: str, name: str, device_type: str, tcp_port: int, udp_port: int, ts: int) -> str:
    """发现报文签名的规范化字段串（**两端必须一致**）。"""
    return "|".join([from_id or "", name or "", device_type or "", str(tcp_port), str(udp_port), str(ts)])


# ---------------------------------------------------------------- 帧小工具

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


# ---------------------------------------------------------------- 握手（模块级，便于单测）

def handshake_server(sock: socket.socket, passphrase: str) -> None:
    """服务端侧：发 magic + AUTH_HELLO，校验客户端 MAC，回 AUTH_RES。"""
    key = key_for(passphrase, b"auth")
    try:
        sock.sendall(MAGIC)
        nonce = os.urandom(16).hex()
        _send_json(sock, FrameType.AUTH_HELLO, {
            "nonce": nonce, "mac": hmac.new(key, f"hello|{nonce}".encode(), hashlib.sha256).hexdigest()
            if passphrase else "",
        })
        ftype, req = _read_json(sock)
        if ftype != FrameType.AUTH_REQ:
            raise TunnelError("隧道握手帧不对（预期 AUTH_REQ）")
        mac = str(req.get("mac") or "")
        if passphrase:
            expect = hmac.new(key, f"client|{nonce}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(mac, expect):
                _send_json(sock, FrameType.AUTH_ERR, {"错": "口令不一致"})
                raise TunnelError("口令不一致")
        mac2 = hmac.new(key, f"server|{nonce}".encode(), hashlib.sha256).hexdigest() if passphrase else ""
        _send_json(sock, FrameType.AUTH_RES, {"mac2": mac2})
    except TunnelError:
        raise
    except (OSError, ProtocolError) as exc:
        raise TunnelError(f"隧道握手失败：{exc}") from exc


def handshake_client(sock: socket.socket, passphrase: str) -> None:
    """客户端侧：收 magic + AUTH_HELLO，回 AUTH_REQ，校验服务端 MAC2。"""
    key = key_for(passphrase, b"auth")
    try:
        if recv_exact(sock, 4) != MAGIC:
            raise TunnelError("对面不是大司农隧道（magic 不对）")
        ftype, hello = _read_json(sock)
        if ftype != FrameType.AUTH_HELLO:
            raise TunnelError("隧道握手帧不对（预期 AUTH_HELLO）")
        nonce = str(hello.get("nonce") or "")
        if not nonce:
            raise TunnelError("隧道握手缺少 nonce")
        mac = hmac.new(key, f"client|{nonce}".encode(), hashlib.sha256).hexdigest() if passphrase else ""
        _send_json(sock, FrameType.AUTH_REQ, {"mac": mac})
        ftype, res = _read_json(sock)
        if ftype == FrameType.AUTH_ERR:
            raise TunnelError("口令与对方不一致（对方拒绝了本次认证）")
        if ftype != FrameType.AUTH_RES:
            raise TunnelError("隧道握手帧不对（预期 AUTH_RES）")
        if passphrase:
            expect = hmac.new(key, f"server|{nonce}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(str(res.get("mac2") or ""), expect):
                raise TunnelError("对方未通过口令校验（口令不一致？）")
    except TunnelError:
        raise
    except (OSError, ProtocolError) as exc:
        raise TunnelError(f"隧道握手失败：{exc}") from exc


# ---------------------------------------------------------------- 隧道上下文

class Tunnel:
    """按口令构建的隧道上下文：持有身份与 TLS 上下文，可被多线程复用。

    口令为空时 `enabled=False`，`accept/connect` 原样返回裸 socket（不加密）。
    """

    def __init__(self, passphrase: str = "") -> None:
        self.passphrase = passphrase or ""
        self.enabled = bool(self.passphrase)
        self._lock = threading.Lock()
        self._cert_dir: Optional[str] = None
        self._server_ctx: Optional[ssl.SSLContext] = None
        self._client_ctx: Optional[ssl.SSLContext] = None
        self._fingerprint = ""
        if self.enabled:
            _key, cert_pem, key_pem, self._fingerprint = _build_identity(self.passphrase)
            # load_cert_chain 需要文件路径 → 放临时目录（整个身份只写一次）
            self._cert_dir = tempfile.mkdtemp(prefix="dasinong-cert-")
            self._cert_path = os.path.join(self._cert_dir, "cert.pem")
            self._key_path = os.path.join(self._cert_dir, "key.pem")
            with open(self._cert_path, "wb") as f:
                f.write(cert_pem)
            with open(self._key_path, "wb") as f:
                f.write(key_pem)

    # ---- 对外信息 ----
    @property
    def fingerprint(self) -> str:
        """本机隧道公钥指纹（口令为空则空串）。供人工核对。"""
        return self._fingerprint

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
                # 不用 PKI 链校验：身份靠"公钥指纹钉死"（见 _verify_peer）。
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self._client_ctx = ctx
            return self._client_ctx

    def _verify_peer(self, tls: ssl.SSLSocket) -> None:
        """钉公钥指纹：与由口令推出的指纹不符 → 口令不一致，或有人在冒充。"""
        from cryptography import x509

        der = tls.getpeercert(binary_form=True)
        if not der:
            raise TunnelError("对端没给证书")
        got = fingerprint_of(x509.load_der_x509_certificate(der).public_key())
        if got != self._fingerprint:
            raise TunnelError("对方身份对不上 —— 口令不一致，或有人在冒充")

    # ---- 连接包装 ----
    def accept(self, raw: socket.socket) -> socket.socket:
        """服务端侧：包 TLS + 跑握手；失败抛 TunnelError。"""
        if not self.enabled:
            return raw
        try:
            tls = self.server_context().wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            raise TunnelError(f"TLS 握手失败（对方可能未开加密）：{exc}") from exc
        try:
            tls.settimeout(_AUTH_TIMEOUT)
            handshake_server(tls, self.passphrase)
            tls.settimeout(None)
            return tls
        except Exception:
            try:
                tls.close()
            except OSError:
                pass
            raise

    def connect(self, raw: socket.socket, host: str = "") -> socket.socket:
        """客户端侧：包 TLS + 钉指纹 + 跑握手；失败抛 TunnelError。"""
        if not self.enabled:
            return raw
        try:
            tls = self.client_context().wrap_socket(raw, server_hostname=None)
        except (ssl.SSLError, OSError) as exc:
            raise TunnelError(f"TLS 握手失败（对方可能未开加密）：{exc}") from exc
        try:
            tls.settimeout(_AUTH_TIMEOUT)
            self._verify_peer(tls)
            handshake_client(tls, self.passphrase)
            tls.settimeout(None)
            return tls
        except Exception:
            try:
                tls.close()
            except OSError:
                pass
            raise

    # ---- 发现报文的签名（明文广播 → 用 PBKDF2 派生的专用密钥）----
    def sign_hello(self, fields: str) -> str:
        if not self.enabled:
            return ""
        return hmac.new(key_for(self.passphrase, b"hello"), fields.encode("utf-8"), hashlib.sha256).hexdigest()

    def close(self) -> None:
        if self._cert_dir:
            shutil.rmtree(self._cert_dir, ignore_errors=True)
            self._cert_dir = None
