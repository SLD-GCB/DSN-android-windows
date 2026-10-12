"""领域模型：Peer / 消息 / 传输文件 / 传输任务。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

# 字节区间（闭区间）
ByteRange = Tuple[int, int]


class PeerState(str, Enum):
    DISCOVERED = "discovered"
    CONNECTING = "connecting"
    ONLINE = "online"
    DEGRADED = "degraded"
    RECONNECT = "reconnect"
    REMOVED = "removed"


class MsgKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    SYSTEM = "system"


class TransferState(str, Enum):
    PENDING = "pending"        # 等待对方 accept
    REJECTED = "rejected"
    ACTIVE = "active"
    PAUSED = "paused"          # 断线，可续传
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Peer:
    device_id: str
    name: str
    ip: str
    tcp_port: int = 37021
    udp_port: int = 37020
    device_type: str = "windows"
    proto: int = 2
    encrypted: bool = False
    last_seen: float = field(default_factory=time.time)
    state: PeerState = PeerState.DISCOVERED

    @property
    def key(self) -> str:
        return self.device_id

    def touch(self) -> None:
        self.last_seen = time.time()


@dataclass
class FileEntry:
    """offer.files[] 单项。"""

    file_id: str
    name: str
    rel_path: str
    size: int
    kind: str = "file"            # file | image | text
    content_id: str = ""          # sha256
    chunk_size: int = 65536
    total_chunks: int = 0
    root_name: Optional[str] = None
    preview: Optional[str] = None  # image base64 缩略图
    mtime: float = 0.0
    #: **仅本地 UI 用**：本机上该文件的路径（发给对方时**不会**带上，避免泄露本地路径）
    local_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "rel_path": self.rel_path,
            "root_name": self.root_name,
            "size": self.size,
            "kind": self.kind,
            "content_id": self.content_id,
            "chunk_size": self.chunk_size,
            "total_chunks": self.total_chunks,
            "preview": self.preview,
            "mtime": self.mtime,
        }


@dataclass
class Transfer:
    transfer_id: str
    peer_id: str
    file: FileEntry
    direction: str = "send"                 # send | recv
    state: TransferState = TransferState.PENDING
    sent: int = 0
    received: int = 0
    # 接收端已落盘区间（闭区间，合并相邻）
    received_ranges: List[ByteRange] = field(default_factory=list)
    # 发送端待发块号队列游标
    local_path: Optional[str] = None
    final_path: Optional[str] = None
    part_path: Optional[str] = None
    error: Optional[str] = None

    @property
    def total(self) -> int:
        return self.file.size

    @property
    def progress(self) -> float:
        total = self.total
        if total <= 0:
            return 1.0
        done = self.received if self.direction == "recv" else self.sent
        return min(1.0, done / total)


@dataclass
class ChatMessage:
    msg_id: str
    conv_id: str            # == peer device_id
    kind: MsgKind
    text: str = ""
    ts: float = field(default_factory=time.time)
    outgoing: bool = False
    status: str = "sent"    # sent | delivered | received | failed
    transfer_id: Optional[str] = None
    file: Optional[FileEntry] = None
