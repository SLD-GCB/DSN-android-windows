"""直连（只知 IP，不依赖 UDP 发现）的回归测试。

场景对应「手机开热点」：Android 不暴露热点网卡，手机既不能广播也不能回 UDP，
所以两端**互相发现不到**。这里用「两台引擎监听不同的 udp_port」来忠实模拟这一点，
验证只靠 `connect_by_ip` 也能建立会话、传文件、并让 `ip:` 会话升级成真实设备。

注意：每个用例都用**独立的端口和设备 ID**（`tag`），否则上一个用例还没完全停掉的
引擎可能仍占着端口、且 device_id 相同，会被误连上，导致时好时坏。
"""
import socket
import threading
import time

from dasinong_core.config import Config
from dasinong_core.events import EventBus, EventType
from dasinong_core.node import NodeEngine
from dasinong_core.store import Store


def _engine(tmp_path, tag: str, name: str, tcp_port: int, udp_port: int) -> NodeEngine:
    data = tmp_path / f"{tag}{name}"
    data.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        udp_port=udp_port, tcp_port=tcp_port, data_dir=data,
        device_name=name, device_id=f"dev-{tag}-{name}-00000000",
    )
    cfg.ensure_dirs()
    engine = NodeEngine(cfg, bus=EventBus())
    engine.attach_store(Store(data / "t.db"))
    # 测试隔离：清掉「记住的设备地址」（pytest 整个会话共用一个临时 prefs 文件，
    # 不清的话上个用例记住的地址会被本用例当成已知设备去连）。
    engine._known_peers = {}          # type: ignore[attr-defined]
    logs = []
    engine.bus.subscribe(lambda e: logs.append(e.data), EventType.LOG)
    engine.test_logs = logs           # type: ignore[attr-defined]
    return engine


def _pair(tmp_path, tag: str, passphrase: str = ""):
    """两台互相发现不到的引擎（udp_port 不同 = 模拟热点下手机发不出广播）。

    v4 起没有口令了：身份是每设备随机的，首次连接由集成测试的 `DASINONG_PAIR_AUTO=1`
    自动完成配对（见 `conftest.py`）。
    """
    n = int(tag)
    a = _engine(tmp_path, tag, "A", 47000 + n * 10, 47200 + n * 10)
    b = _engine(tmp_path, tag, "B", 47001 + n * 10, 47201 + n * 10)
    a.start()
    b.start()
    return a, b


def _close(*engines) -> None:
    for e in engines:
        try:
            e.stop()
        except Exception:
            pass


def test_direct_connect_without_any_discovery(tmp_path):
    """UDP 互相发现不到（模拟热点），直连仍能建会话并把对端登记进发现表。"""
    a, b = _pair(tmp_path, "1")
    try:
        time.sleep(0.4)
        assert a.peers() == [] and b.peers() == [], "本用例前提：两端互相发现不到"

        ok, msg = a.connect_by_ip("127.0.0.1", b.cfg.tcp_port)
        assert ok, msg
        assert a.is_connected(b.cfg.device_id), "拨号方应持有到 B 的会话"
        assert b.is_connected(a.cfg.device_id), "被叫方也应持有到 A 的会话"
        assert any(p.device_id == b.cfg.device_id for p in a.peers()), \
            "直连成功后应把对端登记进发现表（附近列表/会话升级都靠它）"
        assert any(p.device_id == a.cfg.device_id for p in b.peers()), \
            "被叫方也要登记拨号方，否则它不知道对方地址、反向发不出来"
    finally:
        _close(a, b)


def test_direct_connect_upgrades_manual_conversation(tmp_path):
    """先建 `ip:<addr>` 占位会话 → 直连成功 → 自动升级成真实 device_id。"""
    a, b = _pair(tmp_path, "2")
    try:
        bound = []
        a.bus.subscribe(lambda e: bound.append(e.data), EventType.CONVERSATION_REBOUND)

        conv = a.create_conversation_by_ip("127.0.0.1")
        assert conv.startswith("ip:"), f"未发现时应先建占位会话，实际 {conv}"

        ok, msg = a.connect_by_ip("127.0.0.1", b.cfg.tcp_port)
        assert ok, msg
        deadline = time.time() + 3
        while time.time() < deadline and not bound:
            time.sleep(0.1)
        assert bound == [("ip:127.0.0.1", b.cfg.device_id)], f"应触发会话升级，实际 {bound}"
    finally:
        _close(a, b)


def _wait_paired(*engines, timeout: float = 20.0) -> None:
    """v4：首次连接要先**配对**（测试里由 `DASINONG_PAIR_AUTO=1` 自动接受）。

    业务帧（文本/文件）只有在配对完成后才被接受 —— 真实 UI 里表现为"未配对时发送按钮禁用"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(
            any(e.is_paired(o.cfg.device_id) for o in engines if o is not e)
            for e in engines
        ):
            return
        time.sleep(0.1)


def test_direct_connect_transfers_both_ways(tmp_path):
    """直连建立后，双向传文件都要成功（热点下「电脑往手机传」也是这个路径）。"""
    a, b = _pair(tmp_path, "3")
    try:
        ok, msg = a.connect_by_ip("127.0.0.1", b.cfg.tcp_port)
        assert ok, msg
        _wait_paired(a, b)

        payload_a = b"hello-from-a" * 40000
        payload_b = b"hello-from-b" * 40000
        (tmp_path / "a.bin").write_bytes(payload_a)
        (tmp_path / "b.bin").write_bytes(payload_b)

        assert a.send_files(b.cfg.device_id, [str(tmp_path / "a.bin")])
        assert b.send_files(a.cfg.device_id, [str(tmp_path / "b.bin")])

        got_b = b.cfg.receive_dir / "a.bin"
        got_a = a.cfg.receive_dir / "b.bin"
        deadline = time.time() + 30
        while time.time() < deadline and not (got_b.exists() and got_a.exists()):
            time.sleep(0.2)
        assert got_b.exists() and got_b.read_bytes() == payload_a, "B 未收到 A 的文件"
        assert got_a.exists() and got_a.read_bytes() == payload_b, "A 未收到 B 的文件"
    finally:
        _close(a, b)


def test_peer_stays_stable_without_udp(tmp_path):
    """热点场景（UDP 通不了）：有控制连接时附近列表不能反复「删了又加」。

    回归点：曾经发现表每 PEER_TIMEOUT(10s) 把对端剪掉，接着又被补回来，
    而且补回来的那份 last_seen 是旧的 → 下一秒又被剪 —— 附近列表每秒闪一次。
    """
    a, b = _pair(tmp_path, "6")
    try:
        ok, msg = a.connect_by_ip("127.0.0.1", b.cfg.tcp_port)
        assert ok, msg

        added, removed = [], []
        a.bus.subscribe(lambda e: added.append(e.data), EventType.PEER_ADDED)
        a.bus.subscribe(lambda e: removed.append(e.data), EventType.PEER_REMOVED)

        time.sleep(12)          # 必须超过 PEER_TIMEOUT 才暴露这个抖动
        assert removed == [], f"对端不该被剪掉（附近列表会闪）: {removed}"
        assert len(added) <= 1, f"不该反复重新加入: {added}"
        assert a.is_connected(b.cfg.device_id), "控制连接应保持"
        assert any(p.device_id == b.cfg.device_id for p in a.peers()), "对端应一直在列表里"
    finally:
        _close(a, b)


def test_direct_connect_old_peer_times_out_without_hanging(tmp_path):
    """模拟旧版本对端：接受 TCP 但**不回** connect_ok → 必须短超时返回错误。"""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = srv.accept()
            conn.recv(65536)          # 读掉 connect 帧，然后什么都不回
            time.sleep(3)
            conn.close()
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()

    a = _engine(tmp_path, "5", "A", 47050, 47250)
    a.start()
    try:
        t0 = time.time()
        ok, msg = a.connect_by_ip("127.0.0.1", port, timeout=1.0)
        elapsed = time.time() - t0
        assert not ok
        assert elapsed < 3, f"应短超时返回，实际耗时 {elapsed:.1f}s"
        assert msg, "应给出失败原因"
    finally:
        _close(a)
        srv.close()


def test_incoming_peer_can_be_sent_files_without_udp(tmp_path):
    """对端主动连进来后，我们也能把文件发给它（热点场景下的真实毛病）。

    复现：电脑记忆里存的是手机**上一次 WLAN 的旧地址**，手机当热点时又发不出广播，
    于是电脑"能收、发不出去"。修好后：入站连接会把对方**当前**地址登记下来，
    数据连接跟着这条活着的控制连接走。
    """
    a, b = _pair(tmp_path, "8")
    try:
        # A（电脑）记忆里是 B（手机）的旧地址，根本不可达
        a._known_peers[b.cfg.device_id] = {
            "ip": "10.255.255.1", "tcp_port": 59999, "name": "B", "device_type": "windows",
        }
        # B 已经知道 A 的身份（= 手机从电脑的广播里学到电脑），所以是**普通连接**，
        # 而不是"只知 IP 直连"。这条路径才是你遇到的那条。
        b._known_peers[a.cfg.device_id] = {
            "ip": "127.0.0.1", "tcp_port": a.cfg.tcp_port,
            "name": "A", "device_type": "windows",
        }
        # B 主动连进来（= 手机连进电脑）
        ok, msg = b.connect_by_ip("127.0.0.1", a.cfg.tcp_port)
        assert ok, msg

        payload = b"from-pc-to-phone" * 30000
        (tmp_path / "back.bin").write_bytes(payload)
        assert a.send_files(b.cfg.device_id, [str(tmp_path / "back.bin")])

        got = b.cfg.receive_dir / "back.bin"
        deadline = time.time() + 20
        while time.time() < deadline and not got.exists():
            time.sleep(0.2)
        assert got.exists() and got.read_bytes() == payload, \
            "电脑应能把文件发给主动连进来的对端（不该受旧地址影响）"
    finally:
        _close(a, b)


def test_live_session_address_overrides_stale_remembered(tmp_path):
    """在线会话的远端地址必须覆盖记忆里的旧地址（否则会往旧 IP 发）。"""
    a, b = _pair(tmp_path, "9")
    try:
        a._known_peers[b.cfg.device_id] = {
            "ip": "10.255.255.1", "tcp_port": 59999, "name": "B", "device_type": "windows",
        }
        # 同上：B 知道 A 的身份 → 普通连接（不是 ip_only）
        b._known_peers[a.cfg.device_id] = {
            "ip": "127.0.0.1", "tcp_port": a.cfg.tcp_port,
            "name": "A", "device_type": "windows",
        }
        ok, msg = b.connect_by_ip("127.0.0.1", a.cfg.tcp_port)
        assert ok, msg

        deadline = time.time() + 5
        while time.time() < deadline:
            peer = a.discovery.get(b.cfg.device_id)
            if peer is not None and peer.ip == "127.0.0.1":
                break
            time.sleep(0.2)

        peer = a.discovery.get(b.cfg.device_id)
        assert peer is not None, "对端应被登记进发现表"
        assert peer.ip == "127.0.0.1", f"应修正为在线地址，实际 {peer.ip}"
        assert a._known_peers[b.cfg.device_id]["ip"] == "127.0.0.1", "记忆里的旧地址也该被更正"
    finally:
        _close(a, b)
