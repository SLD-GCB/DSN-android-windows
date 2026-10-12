"""隧道（dsn-tunnel **v4**）：每设备身份的双向签名握手 + 发现报文签名。

⚠ 身份的**固定向量**在 `test_identity.py`（那是跨语言锚点）。
这里测的是握手本身：双方能互相**证明身份**、篡改的报文会被拒。
"""
import socket
import threading

from dasinong_core.network.tunnel import (
    MAGIC,
    Tunnel,
    hello_fields,
    verify_hello,
)
from dasinong_core.security.identity import Identity

SEED_A = bytes(range(32))
SEED_B = bytes(reversed(range(32)))


def _tunnel(seed: bytes) -> Tunnel:
    return Tunnel(Identity.from_seed(seed))


def _loopback(tun_server: Tunnel, tun_client: Tunnel):
    """真 TLS 环回：前者作服务端、后者作客户端；返回 (server_auth, client_auth)。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    box = {}

    def serve() -> None:
        raw, _ = srv.accept()
        try:
            box["server"] = tun_server.accept(raw)
        except Exception as exc:                    # pragma: no cover - 便于定位
            box["server_error"] = exc

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    conn = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        box["client"] = tun_client.connect(conn, "127.0.0.1")
        th.join(timeout=10)
    finally:
        srv.close()
    if "server_error" in box:
        raise box["server_error"]
    return box["server"], box["client"]


def test_mutual_identity_handshake():
    """两端各自拿到**对方已认证的指纹**，且 nonce 一致（配对短码要用它）。"""
    a, b = _tunnel(SEED_A), _tunnel(SEED_B)
    server, client = _loopback(a, b)
    try:
        assert server.fp == b.identity.fingerprint      # 服务端认证的是"客户端"
        assert client.fp == a.identity.fingerprint      # 客户端认证的是"服务端"
        assert server.nonce == client.nonce and len(server.nonce) == 16
        # 连接可用
        client.sock.sendall(b"ping")
        assert server.sock.recv(4) == b"ping"
    finally:
        server.sock.close()
        client.sock.close()
        a.close()
        b.close()


def test_handshake_is_authentication_not_trust():
    """任意两台设备都能完成握手（证明持有自称的私钥）—— 是否信任由配对决定。"""
    a, b = _tunnel(SEED_A), _tunnel(SEED_B)
    server, client = _loopback(a, b)
    try:
        assert server.fp != client.fp                     # 两个身份确实不同
        assert server.fp == Identity.from_seed(SEED_B).fingerprint
    finally:
        server.sock.close()
        client.sock.close()
        a.close()
        b.close()


def test_hello_fields_vector():
    # ⚠ 跨语言锚点：Android `TunnelVectorTest.kt` 断言同一串
    assert hello_fields("dev-1", "我的电脑", "windows", 37021, 37020, 1730000000123) == (
        "dev-1|我的电脑|windows|37021|37020|1730000000123"
    )


def test_magic_and_constants():
    assert MAGIC == b"DSN1"


def test_hello_signature_roundtrip():
    a = _tunnel(SEED_A)
    try:
        fields = hello_fields("dev-1", "手机", "android", 37021, 37020, 1730000000123)
        sig = a.sign_hello(fields)
        assert verify_hello(a.spki_b64, fields, sig) is True
        # 改字段 / 换公钥 / 改签名 都必须失败
        assert verify_hello(a.spki_b64, fields + "x", sig) is False
        assert verify_hello(_tunnel(SEED_B).spki_b64, fields, sig) is False
        assert verify_hello(a.spki_b64, fields, sig[:-4] + "AAAA") is False
        assert verify_hello("", fields, sig) is False
    finally:
        a.close()


def test_signature_only_valid_for_matching_key():
    """A 的签名拿 B 的公钥来验 → 必须失败（"你是谁"只能由对应私钥证明）。"""
    a, b = _tunnel(SEED_A), _tunnel(SEED_B)
    try:
        import base64

        fields = hello_fields("dev-1", "手机", "android", 37021, 37020, 1730000000123)
        sig_by_a = a.sign_hello(fields)
        pub_b = base64.b64encode(b.identity.spki_der).decode()
        assert verify_hello(pub_b, fields, sig_by_a) is False
        assert verify_hello(a.spki_b64, fields, sig_by_a) is True
    finally:
        a.close()
        b.close()
