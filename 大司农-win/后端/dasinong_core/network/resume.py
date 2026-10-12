"""断点续传元数据（.partmeta）读写。

与 `.part` 同名，附加后缀 `.partmeta`，原子写入（tmp + os.replace）。
续传判定依据：content_id(sha256) + size，而非文件名。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

ByteRange = Tuple[int, int]


def meta_path(part_path: Path) -> Path:
    return part_path.with_suffix(part_path.suffix + ".partmeta")


@dataclass
class PartMeta:
    transfer_id: str
    file_id: str
    name: str
    size: int
    sha256: str
    chunk_size: int = 65536
    total_chunks: int = 0
    received: List[ByteRange] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "transfer_id": self.transfer_id,
            "file_id": self.file_id,
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "chunk_size": self.chunk_size,
            "total_chunks": self.total_chunks,
            "received": [[s, e] for s, e in self.received],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, obj: dict) -> Optional["PartMeta"]:
        try:
            received = [tuple(r) for r in obj.get("received", [])]
            return cls(
                transfer_id=obj["transfer_id"],
                file_id=obj["file_id"],
                name=obj.get("name", ""),
                size=int(obj["size"]),
                sha256=obj.get("sha256", ""),
                chunk_size=int(obj.get("chunk_size", 65536)),
                total_chunks=int(obj.get("total_chunks", 0)),
                received=[(int(s), int(e)) for s, e in received],
                updated_at=float(obj.get("updated_at", time.time())),
            )
        except (KeyError, TypeError, ValueError):
            return None


def load(part_path: Path) -> Optional[PartMeta]:
    p = meta_path(part_path)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return PartMeta.from_dict(obj) if isinstance(obj, dict) else None


def save(part_path: Path, meta: PartMeta) -> None:
    meta.updated_at = time.time()
    p = meta_path(part_path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(meta.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def clear(part_path: Path) -> None:
    for p in (meta_path(part_path),):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
