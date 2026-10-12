"""SQLite 存储单测。"""
from dasinong_core.models import ChatMessage, FileEntry, MsgKind, Transfer, TransferState
from dasinong_core.store import Store


def test_message_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    try:
        msg = ChatMessage(msg_id="m1", conv_id="peer-1", kind=MsgKind.TEXT,
                          text="你好", outgoing=True, status="sent")
        store.add_message(msg)
        store.update_message_status("m1", "delivered")
        rows = store.messages_for("peer-1")
        assert len(rows) == 1
        assert rows[0].text == "你好"
        assert rows[0].status == "delivered"
        assert rows[0].outgoing is True
    finally:
        store.close()


def test_message_with_file(tmp_path):
    store = Store(tmp_path / "t.db")
    try:
        entry = FileEntry(file_id="f1", name="报告.pdf", rel_path="项目/报告.pdf",
                          size=100, kind="file", content_id="ab" * 32)
        msg = ChatMessage(msg_id="m2", conv_id="peer-2", kind=MsgKind.FILE,
                          file=entry, outgoing=False)
        store.add_message(msg)
        rows = store.messages_for("peer-2")
        assert rows[0].file is not None
        assert rows[0].file.name == "报告.pdf"
        assert rows[0].file.rel_path == "项目/报告.pdf"
    finally:
        store.close()


def test_conversations(tmp_path):
    store = Store(tmp_path / "t.db")
    try:
        store.add_message(ChatMessage(msg_id="a", conv_id="p1", kind=MsgKind.TEXT, text="1", ts=1.0))
        store.add_message(ChatMessage(msg_id="b", conv_id="p1", kind=MsgKind.TEXT, text="2", ts=5.0))
        store.add_message(ChatMessage(msg_id="c", conv_id="p2", kind=MsgKind.TEXT, text="3", ts=3.0))
        convs = store.conversations()
        assert convs[0]["conv_id"] == "p1"
        assert convs[0]["text"] == "2"
        assert len(convs) == 2
    finally:
        store.close()


def test_create_conversation_without_messages(tmp_path):
    """手动新建（无消息）的会话也要出现在列表里，且带标题。"""
    store = Store(tmp_path / "t.db")
    try:
        store.ensure_conversation("peer-9", "客厅手机")
        convs = store.conversations()
        assert len(convs) == 1
        assert convs[0]["conv_id"] == "peer-9"
        assert convs[0]["title"] == "客厅手机"
        assert convs[0]["text"] == ""
    finally:
        store.close()


def test_delete_conversation_clears_and_hides(tmp_path):
    store = Store(tmp_path / "t.db")
    try:
        store.add_message(ChatMessage(msg_id="a", conv_id="p1", kind=MsgKind.TEXT, text="hi", ts=1.0))
        assert len(store.messages_for("p1")) == 1
        store.delete_conversation("p1")
        # 消息被清空、会话从列表消失，但被记为 hidden（防止对端在线时又被自动列出）
        assert store.messages_for("p1") == []
        assert store.conversations() == []
        assert store.hidden_conversation_ids() == {"p1"}
        # 再次来消息 → 取消隐藏、重新出现
        store.add_message(ChatMessage(msg_id="b", conv_id="p1", kind=MsgKind.TEXT, text="again", ts=2.0))
        assert store.hidden_conversation_ids() == set()
        assert len(store.conversations()) == 1
    finally:
        store.close()


def test_rebind_manual_conversation(tmp_path):
    """手动 IP 会话（ip:…）在设备被发现后升级成真实 device_id，消息一并迁移。"""
    store = Store(tmp_path / "t.db")
    try:
        store.ensure_conversation("ip:192.168.1.5", "192.168.1.5")
        store.add_message(ChatMessage(msg_id="a", conv_id="ip:192.168.1.5",
                                      kind=MsgKind.TEXT, text="在吗", ts=1.0))
        assert store.has_conversation("ip:192.168.1.5")
        store.rebind_conversation("ip:192.168.1.5", "dev-9", "客厅手机")
        assert not store.has_conversation("ip:192.168.1.5")
        assert store.messages_for("ip:192.168.1.5") == []
        convs = store.conversations()
        assert len(convs) == 1
        assert convs[0]["conv_id"] == "dev-9"
        assert convs[0]["title"] == "客厅手机"
        assert convs[0]["text"] == "在吗"
        assert len(store.messages_for("dev-9")) == 1
    finally:
        store.close()


def test_transfer_upsert(tmp_path):
    store = Store(tmp_path / "t.db")
    try:
        entry = FileEntry(file_id="f1", name="a.bin", rel_path="a.bin", size=1000, content_id="x")
        t = Transfer(transfer_id="g1", peer_id="p1", file=entry, direction="recv",
                     state=TransferState.ACTIVE, received=400, final_path="/tmp/a.bin")
        store.upsert_transfer(t)
        t.state = TransferState.COMPLETED
        t.received = 1000
        store.upsert_transfer(t)
        rows = store.transfers()
        assert len(rows) == 1
        assert rows[0]["state"] == "completed"
        assert rows[0]["received"] == 1000
        assert rows[0]["group_id"] == "g1"
    finally:
        store.close()


def test_settings(tmp_path):
    store = Store(tmp_path / "t.db")
    try:
        assert store.setting_get("device_name", "default") == "default"
        store.setting_set("device_name", "我的电脑")
        assert store.setting_get("device_name") == "我的电脑"
    finally:
        store.close()
