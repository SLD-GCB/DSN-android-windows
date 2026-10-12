"""后端事件 -> Qt 信号桥。

网络线程调用 emit 是安全的（Qt 跨线程自动走 QueuedConnection），
UI 槽函数始终在 GUI 线程执行。
"""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from dasinong_core.events import EventBus, EventType


class Bridge(QObject):
    peerAdded = Signal(object)
    peerUpdated = Signal(object)
    peerRemoved = Signal(object)
    messageEvent = Signal(object)
    messageStatus = Signal(object)
    transferEvent = Signal(object)
    conversationRebound = Signal(str, str)   # (旧会话 id, 真实设备 id)
    log = Signal(str)

    def __init__(self, bus: EventBus) -> None:
        super().__init__()
        self._bus = bus
        bus.subscribe(self._on_event)

    def _on_event(self, event) -> None:
        t = event.type
        if t == EventType.PEER_ADDED:
            self.peerAdded.emit(event.data)
        elif t == EventType.PEER_UPDATED:
            self.peerUpdated.emit(event.data)
        elif t == EventType.PEER_REMOVED:
            self.peerRemoved.emit(event.data)
        elif t == EventType.MESSAGE:
            self.messageEvent.emit(event.data)
        elif t == EventType.MESSAGE_STATUS:
            self.messageStatus.emit(event.data)
        elif t in (EventType.TRANSFER_ADDED, EventType.TRANSFER_UPDATED):
            self.transferEvent.emit(event.data)
        elif t == EventType.CONVERSATION_REBOUND:
            old_id, new_id = event.data
            self.conversationRebound.emit(old_id, new_id)
        elif t == EventType.LOG:
            self.log.emit(str(event.data))
