"""NodeEngine 行为单测（不启动网络，只验证编排逻辑）。"""
from dasinong_core.config import Config
from dasinong_core.events import EventBus, EventType
from dasinong_core.models import FileEntry, Transfer, TransferState
from dasinong_core.node import NodeEngine
from dasinong_core.store import Store


def _engine(tmp_path, bus: EventBus) -> NodeEngine:
    cfg = Config(udp_port=0, tcp_port=0, data_dir=tmp_path,
                 device_name="T", device_id="dev-test-0000")
    engine = NodeEngine(cfg, bus=bus)
    engine.attach_store(Store(tmp_path / "t.db"))
    return engine


def test_outbox_flush_updates_status_without_mutating(tmp_path):
    """连不上时消息入队；连上后补发 → 状态写入 DB 并发布事件。

    关键回归点：**不能原地修改 UI 持有的 ChatMessage 实例**。
    否则 UI 侧 StateFlow 会因「新旧值相等」而不发通知，气泡永远卡在「待发送」。
    """
    bus = EventBus()
    engine = _engine(tmp_path, bus)

    status_events = []
    bus.subscribe(lambda e: status_events.append(e.data), EventType.MESSAGE_STATUS)

    # 1) 未连接 → pending + 入队；这个实例就是 UI 会持有的那个
    engine.transport.send_to = lambda pid, env: False          # type: ignore[assignment]
    msg = engine.send_text("peer-1", "你好")
    assert msg.status == "pending"
    assert len(engine._outbox) == 1

    # 2) 连上 → 补发
    engine.transport.send_to = lambda pid, env: True           # type: ignore[assignment]
    engine._flush_outbox()

    assert engine._outbox == []
    assert engine.history("peer-1")[0].status == "sent"
    assert status_events == [{"msg_id": msg.msg_id, "status": "sent"}]
    # 核心：原实例没有被原地改成 sent（改了就丢了 UI 刷新）
    assert msg.status == "pending"


def test_delivered_updates_db_without_mutating(tmp_path):
    bus = EventBus()
    engine = _engine(tmp_path, bus)

    status_events = []
    bus.subscribe(lambda e: status_events.append(e.data), EventType.MESSAGE_STATUS)

    engine.transport.send_to = lambda pid, env: True           # type: ignore[assignment]
    msg = engine.send_text("peer-1", "hi")
    assert msg.status == "sent"

    engine._on_delivered({"data": {"msg_id": msg.msg_id}})
    assert engine.history("peer-1")[0].status == "delivered"
    assert status_events[-1] == {"msg_id": msg.msg_id, "status": "delivered"}
    assert msg.status == "sent"        # 同上：不原地改


def _add_transfer(engine, tmp_path, state, name="a.bin"):
    f = tmp_path / name
    f.write_bytes(b"hello")
    entry = FileEntry(file_id="f1", name=name, rel_path=name, size=5)
    engine.store.upsert_transfer(Transfer(
        transfer_id="g1", peer_id="p1", file=entry, direction="recv",
        state=state, received=5, final_path=str(f),
    ))
    return f


def test_delete_transfer_removes_record_and_file(tmp_path):
    engine = _engine(tmp_path, EventBus())
    f = _add_transfer(engine, tmp_path, TransferState.COMPLETED)
    assert len(engine.store.transfers()) == 1

    ok, msg = engine.delete_transfer("f1")
    assert ok, msg
    assert not f.exists()                     # 磁盘文件被删掉
    assert engine.store.transfers() == []     # 记录也移除


def test_delete_transfer_refuses_while_active(tmp_path):
    engine = _engine(tmp_path, EventBus())
    f = _add_transfer(engine, tmp_path, TransferState.ACTIVE)

    ok, msg = engine.delete_transfer("f1")
    assert not ok and "进行中" in msg
    assert f.exists()                         # 进行中：文件与记录都不动
    assert len(engine.store.transfers()) == 1


def test_delete_transfer_without_file_keeps_going(tmp_path):
    """文件已经不在了（用户手动删过）也要能把记录清掉。"""
    engine = _engine(tmp_path, EventBus())
    f = _add_transfer(engine, tmp_path, TransferState.COMPLETED)
    f.unlink()
    ok, msg = engine.delete_transfer("f1")
    assert ok
    assert engine.store.transfers() == []


def _add_transfer2(engine, tmp_path, state, file_id, name):
    f = tmp_path / name
    f.write_bytes(b"hello")
    entry = FileEntry(file_id=file_id, name=name, rel_path=name, size=5)
    engine.store.upsert_transfer(Transfer(
        transfer_id="g-" + file_id, peer_id="p1", file=entry, direction="recv",
        state=state, received=5, final_path=str(f),
    ))
    return f


def test_persisted_transfers_roundtrip(tmp_path):
    """启动回填：数据库历史记录能还原成 Transfer（否则关掉重开列表就空了）。"""
    engine = _engine(tmp_path, EventBus())
    _add_transfer2(engine, tmp_path, TransferState.COMPLETED, "f9", "nine.bin")

    rows = engine.persisted_transfers()
    assert len(rows) == 1
    t = rows[0]
    assert t.file.file_id == "f9"
    assert t.file.name == "nine.bin"
    assert t.state == TransferState.COMPLETED
    assert t.direction == "recv"
    assert t.final_path is not None and t.final_path.endswith("nine.bin")


def test_clear_transfers_removes_finished_keeps_active(tmp_path):
    """一键清空：已结束的记录连同文件一起删；进行中的保留。"""
    engine = _engine(tmp_path, EventBus())
    f_done = _add_transfer2(engine, tmp_path, TransferState.COMPLETED, "f-done", "done.bin")
    f_fail = _add_transfer2(engine, tmp_path, TransferState.FAILED, "f-fail", "fail.bin")
    f_live = _add_transfer2(engine, tmp_path, TransferState.ACTIVE, "f-live", "live.bin")
    assert len(engine.store.transfers()) == 3

    ok, msg = engine.clear_transfers()
    assert ok, msg
    assert not f_done.exists()          # 已结束的文件被删
    assert not f_fail.exists()
    assert f_live.exists()              # 进行中的文件保留
    remaining = {r["file_id"] for r in engine.store.transfers()}
    assert remaining == {"f-live"}
    assert "保留" in msg


def test_clear_transfers_empty_is_ok(tmp_path):
    engine = _engine(tmp_path, EventBus())
    ok, msg = engine.clear_transfers()
    assert ok
    assert engine.store.transfers() == []
