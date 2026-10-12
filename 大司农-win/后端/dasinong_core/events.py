"""事件总线：后端（网络线程）向 UI 发布事件。

后端保持 Qt 无关；UI 侧订阅后自行切回 UI 线程（PySide6 信号自动排队）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List


class EventType(str, Enum):
    PEER_ADDED = "peer_added"
    PEER_UPDATED = "peer_updated"
    PEER_REMOVED = "peer_removed"
    MESSAGE = "message"
    MESSAGE_STATUS = "message_status"
    TRANSFER_ADDED = "transfer_added"
    TRANSFER_UPDATED = "transfer_updated"
    CONVERSATION_REBOUND = "conversation_rebound"
    LOG = "log"


@dataclass
class Event:
    type: str
    data: Any = None


class EventBus:
    """线程安全的事件总线。支持按类型订阅与全局订阅。"""

    def __init__(self) -> None:
        self._by_type: Dict[str, List[Callable[[Event], None]]] = {}
        self._all: List[Callable[[Event], None]] = []
        self._lock = threading.RLock()

    def subscribe(self, callback: Callable[[Event], None], event_type: str | None = None) -> None:
        with self._lock:
            if event_type is None:
                self._all.append(callback)
            else:
                self._by_type.setdefault(event_type, []).append(callback)

    def unsubscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback in self._all:
                self._all.remove(callback)
            for subs in self._by_type.values():
                if callback in subs:
                    subs.remove(callback)

    def publish(self, event_type: str, data: Any = None) -> None:
        event = Event(type=event_type, data=data)
        with self._lock:
            subs = list(self._all) + list(self._by_type.get(event_type, ()))
        for cb in subs:
            try:
                cb(event)
            except Exception:
                # 单个订阅者异常不影响其他订阅者
                pass
