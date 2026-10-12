"""控制连接**接口池**：按需分配、互不串台、可归还。

设计口径：池 = tcp_port+1 .. +7（共 7 个可用）；一台设备接入就从池里拿一个口，
用完（设备离线）归还。**不设 SO_REUSEADDR** —— 被占就换下一个，绝不两条连接绑同一口。
"""
import socket

from dasinong_core.config import Config
from dasinong_core.events import EventBus
from dasinong_core.network.control import TransportManager


def _tm(tmp_path, tcp_port: int) -> TransportManager:
    cfg = Config(
        udp_port=tcp_port + 1, tcp_port=tcp_port, data_dir=tmp_path,
        device_name="T", device_id="t" * 8,
    )
    bus = EventBus()
    return TransportManager(cfg, bus, on_control=lambda *a: None, on_data=lambda *a: None)


def test_pool_acquire_returns_distinct_listening_ports(tmp_path):
    tm = _tm(tmp_path, 47300)
    tm.start()
    try:
        s1, p1 = tm._acquire_pool_port()
        s2, p2 = tm._acquire_pool_port()
        assert p1 and p2 and p1 != p2, "两次分配必须是不同的口"
        assert p1 != tm.cfg.tcp_port and p2 != tm.cfg.tcp_port, "不能占用主口"
        # 两个口确实在监听（能连上）
        for p in (p1, p2):
            c = socket.create_connection(("127.0.0.1", p), timeout=2)
            c.close()
        # 归还后该口不再监听
        tm._release_pool_port(s1)
        tm._release_pool_port(s2)
        for p in (p1, p2):
            c = socket.socket()
            c.settimeout(1.5)
            try:
                assert c.connect_ex(("127.0.0.1", p)) != 0, f"归还后 {p} 不该还通"
            finally:
                c.close()
    finally:
        tm.stop()


def test_pool_does_not_steal_an_occupied_port(tmp_path):
    """池里的某个口被别人占着 → 必须**跳过**，不能"绑成功"导致串台。"""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("", 47402))
    blocker.listen(1)
    tm = _tm(tmp_path, 47400)
    tm.start()
    try:
        _, p = tm._acquire_pool_port()
        assert p == 47401, f"47402 被占应跳过，实际拿到 {p}"
    finally:
        tm.stop()
        blocker.close()


def test_identity_advertises_assigned_port_and_releases(tmp_path):
    """接线后的契约：**这台设备分到专属口 → 身份通告它就是那个口**；离线归还 → 回到主口。"""
    tm = _tm(tmp_path, 47500)
    tm.start()
    try:
        assert tm._identity("dev-b")["tcp_ports"] == [tm.cfg.tcp_port], "没分配时应回落主口"
        tm._assign_pool_port("dev-b")
        ent = tm._peer_ports.get("dev-b")
        assert ent is not None, "应分配到一个专属口"
        port = ent[1]
        assert port != tm.cfg.tcp_port
        assert tm._identity("dev-b")["tcp_ports"] == [port], "通告必须是它那个专属口"
        # 另一台设备拿到的是**不同的**口
        tm._assign_pool_port("dev-c")
        assert tm._peer_ports["dev-c"][1] != port
        # 归还后：不再通告专属口（回落主口），且该口不再监听
        tm._release_pool_port_for("dev-b")
        assert tm._identity("dev-b")["tcp_ports"] == [tm.cfg.tcp_port]
        c = socket.socket(); c.settimeout(1.5)
        try:
            assert c.connect_ex(("127.0.0.1", port)) != 0, "归还后不该还通"
        finally:
            c.close()
    finally:
        tm.stop()
