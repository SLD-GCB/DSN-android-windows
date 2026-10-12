"""加密隧道（dsn-tunnel v3）单测。

⚠ 里面的**固定向量**是跨语言一致性的锚点：Kotlin 端 `TunnelVectorTest.kt` 断言
同样的值。任何一端动了盐/迭代/曲线/编码，这里都会红。
"""
import socket
import threading

from dasinong_core.network import tunnel
from dasinong_core.protocol.frame import FrameType, read_frame, write_frame

PW = "dasinong-test-2026"
PW_CN = "中文口令测试"

# ── 固定向量（改动即跨端不兼容）─────────────────────────────────────
PW_KDF8 = "2bb8681617c797d9"
PW_FP = "a836bcb66ecfef2096fcea5cb1c1760a698fb18bdbf95eae7eef5bf1b810ea80"
PW_CN_FP = "fb0b9bab22de0e8d7c46d3567609c6ee98129cdf81541e53fc68e3d15fb6138f"


def test_kdf_vector():
    key = tunnel.derive_key(PW)
    assert len(key) == 32
    assert key[:8].hex() == PW_KDF8


def test_fingerprint_vector():
    """同口令的两台设备必须算出**同一把**身份密钥（v3 的核心约定）。"""
    t = tunnel.Tunnel(PW)
    try:
        assert t.enabled
        assert t.fingerprint == PW_FP
        assert tunnel.Tunnel(PW).fingerprint == PW_FP       # 可复现
    finally:
        t.close()


def test_fingerprint_vector_chinese():
    """中文口令也要一致（确认 UTF-8 编码路径两端相同）。"""
    t = tunnel.Tunnel(PW_CN)
    try:
        assert t.fingerprint == PW_CN_FP
    finally:
        t.close()


def test_fingerprint_is_distinct_for_other_passphrase():
    a = tunnel.Tunnel(PW)
    c = tunnel.Tunnel("another-passphrase")
    try:
        assert a.fingerprint != c.fingerprint
    finally:
        a.close()
        c.close()


def test_empty_passphrase_means_plaintext():
    t = tunnel.Tunnel("")
    try:
        assert t.enabled is False
        assert t.fingerprint == ""
        s1, s2 = socket.socketpair()
        try:
            assert t.accept(s1) is s1      # 空口令：原样返回裸 socket，不加密
            assert t.connect(s2) is s2
        finally:
            s1.close()
            s2.close()
    finally:
        t.close()


def test_hello_fields_and_signature():
    fields = tunnel.hello_fields("dev-1", "我的电脑", "windows", 37021, 37020, 1730000000123)
    assert fields == "dev-1|我的电脑|windows|37021|37020|1730000000123"
    t = tunnel.Tunnel(PW)
    plain = tunnel.Tunnel("")
    try:
        assert t.sign_hello(fields) != ""
        assert t.sign_hello(fields) == t.sign_hello(fields)   # 确定性
        assert plain.sign_hello(fields) == ""                 # 未加密 → 不签名
        # 域分离：发现签名与 AUTH 握手必须用不同的密钥
        assert tunnel.key_for(PW, b"hello") != tunnel.key_for(PW, b"auth")
    finally:
        t.close()
        plain.close()


def test_magic_and_constants():
    assert tunnel.MAGIC == b"DSN1"
    assert tunnel.SALT == b"dasinong-tunnel-v1"
    assert tunnel.ITERATIONS == 200_000


# ── 真实 TLS 环回 ──────────────────────────────────────────────────

def _loopback(pw_server: str, pw_client: str):
    """起一个本地 TLS 服务端，客户端连上去握手并塞一帧业务数据。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    t_srv = tunnel.Tunnel(pw_server)
    t_cli = tunnel.Tunnel(pw_client)
    seen = {}

    def server():
        try:
            raw, _ = srv.accept()
            sock = t_srv.accept(raw)
            frame = read_frame(sock)
            seen["frame"] = frame
            sock.close()
        except Exception as exc:      # noqa: BLE001 - 测试里要看具体异常
            seen["server_err"] = exc

    th = threading.Thread(target=server, daemon=True)
    th.start()

    client_err = None
    try:
        raw = socket.create_connection(("127.0.0.1", port), timeout=8)
        sock = t_cli.connect(raw, "127.0.0.1")
        write_frame(sock, FrameType.JSON, b'{"type":"connect"}')
        sock.close()
    except Exception as exc:          # noqa: BLE001
        client_err = exc
    th.join(timeout=8)
    srv.close()
    t_srv.close()
    t_cli.close()
    return seen, client_err


def test_tls_loopback_same_passphrase():
    seen, client_err = _loopback(PW, PW)
    assert client_err is None, f"客户端不应报错：{client_err}"
    assert "server_err" not in seen, f"服务端不应报错：{seen.get('server_err')}"
    assert seen["frame"] is not None
    assert seen["frame"][0] == FrameType.JSON
    assert b"connect" in seen["frame"][1]


def test_tls_loopback_wrong_passphrase_rejected():
    """口令不一致必须连不上（公钥指纹对不上）。"""
    seen, client_err = _loopback(PW, "wrong-passphrase")
    assert client_err is not None
    assert isinstance(client_err, tunnel.TunnelError)
    assert "身份" in str(client_err) or "口令" in str(client_err)
    assert seen.get("frame") is None


def test_plaintext_loopback_both_empty():
    seen, client_err = _loopback("", "")
    assert client_err is None
    assert seen.get("frame") is not None
    assert seen["frame"][0] == FrameType.JSON
