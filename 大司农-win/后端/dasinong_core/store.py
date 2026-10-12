"""SQLite 持久化：会话 / 消息 / 传输状态。

- 消息与传输落盘，重启后可恢复会话历史与「可续传」列表；
- 通过 `Store.settings_get/set` 存少量设置项；
- 线程安全（内部加锁，check_same_thread=False，供网络线程与 UI 线程共用）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from .models import ChatMessage, FileEntry, MsgKind, Transfer, TransferState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS message (
    msg_id      TEXT PRIMARY KEY,
    conv_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT,
    ts          REAL NOT NULL,
    outgoing    INTEGER NOT NULL,
    status      TEXT,
    transfer_id TEXT,
    file_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_message_conv ON message(conv_id, ts);

CREATE TABLE IF NOT EXISTS transfer (
    file_id     TEXT PRIMARY KEY,
    group_id    TEXT,
    peer_id     TEXT,
    direction   TEXT,
    state       TEXT,
    name        TEXT,
    rel_path    TEXT,
    size        INTEGER,
    received    INTEGER,
    final_path  TEXT,
    error       TEXT,
    updated_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_transfer_group ON transfer(group_id);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 会话表：让「会话」成为可持久化、可新建/可删除的实体。
-- 有消息的会话会在写消息时自动登记；用户也能手动新建；删除 = 清空消息 + 打隐藏标记
-- （hidden=1 让它即使对端仍在线也不再被自动列出）。
CREATE TABLE IF NOT EXISTS conversation (
    conv_id     TEXT PRIMARY KEY,
    title       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    hidden      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conversation_updated ON conversation(updated_at);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------ 消息
    def add_message(self, msg: ChatMessage) -> None:
        file_json = None
        if msg.file is not None:
            # ⚠ local_path 只落**本地**库（用于重启后仍能显示图片缩略图），不进协议
            data = msg.file.to_dict()
            data["local_path"] = msg.file.local_path
            file_json = json.dumps(data, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO message"
                "(msg_id,conv_id,kind,text,ts,outgoing,status,transfer_id,file_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    msg.msg_id, msg.conv_id, msg.kind.value if isinstance(msg.kind, MsgKind) else str(msg.kind),
                    msg.text, msg.ts, 1 if msg.outgoing else 0, msg.status,
                    msg.transfer_id, file_json,
                ),
            )
            # 有消息 = 会话存在：顺带登记进 conversation 表（取消隐藏），标题稍后由上层补齐
            now = time.time()
            self._conn.execute(
                "INSERT OR IGNORE INTO conversation(conv_id,title,created_at,updated_at,hidden)"
                " VALUES(?,?,?,?,0)",
                (msg.conv_id, "", now, now),
            )
            self._conn.execute(
                "UPDATE conversation SET updated_at=?, hidden=0 WHERE conv_id=?",
                (now, msg.conv_id),
            )
            self._conn.commit()

    def update_message_status(self, msg_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE message SET status=? WHERE msg_id=?", (status, msg_id))
            self._conn.commit()

    def messages_for(self, conv_id: str, limit: int = 500) -> List[ChatMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM message WHERE conv_id=? ORDER BY ts ASC LIMIT ?",
                (conv_id, limit),
            ).fetchall()
        out: List[ChatMessage] = []
        for r in rows:
            file = None
            if r["file_json"]:
                try:
                    file = _file_from_dict(json.loads(r["file_json"]))
                except (ValueError, TypeError):
                    file = None
            out.append(ChatMessage(
                msg_id=r["msg_id"], conv_id=r["conv_id"], kind=MsgKind(r["kind"]),
                text=r["text"] or "", ts=r["ts"], outgoing=bool(r["outgoing"]),
                status=r["status"] or "sent", transfer_id=r["transfer_id"], file=file,
            ))
        return out

    def conversations(self) -> List[dict]:
        """返回**可见**会话（hidden=0），带最后一条消息预览，供会话列表使用。

        列表来源是 `conversation` 表而不是直接聚合 message：这样即使用户新建了一个
        「还没发过消息」的会话、或对端离线了，它依然留在列表里。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.conv_id AS conv_id, c.title AS title,"
                " COALESCE((SELECT m.text FROM message m"
                "           WHERE m.conv_id=c.conv_id AND m.text IS NOT NULL AND m.text<>''"
                "           ORDER BY m.ts DESC LIMIT 1), '') AS text,"
                " COALESCE((SELECT MAX(m.ts) FROM message m WHERE m.conv_id=c.conv_id),"
                "          c.updated_at) AS ts"
                " FROM conversation c WHERE c.hidden=0 ORDER BY ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def ensure_conversation(self, conv_id: str, title: Optional[str] = None) -> None:
        """登记（或取消隐藏）一个会话。写消息 / 打开会话 / 手动新建都会走这里。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO conversation(conv_id,title,created_at,updated_at,hidden)"
                " VALUES(?,?,?,?,0)",
                (conv_id, title or "", now, now),
            )
            if title:
                self._conn.execute(
                    "UPDATE conversation SET title=?, updated_at=?, hidden=0 WHERE conv_id=?",
                    (title, now, conv_id),
                )
            else:
                self._conn.execute(
                    "UPDATE conversation SET updated_at=?, hidden=0 WHERE conv_id=?",
                    (now, conv_id),
                )
            self._conn.commit()

    def clear_messages(self, conv_id: str) -> int:
        """清空某个会话的聊天记录（**不动会话本身**，会话仍留在列表里）。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM message WHERE conv_id=?", (conv_id,))
            self._conn.commit()
        return cur.rowcount or 0

    def delete_message(self, msg_id: str) -> int:
        """删掉一条消息（只删本机记录，不影响对方）。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM message WHERE msg_id=?", (msg_id,))
            self._conn.commit()
        return cur.rowcount or 0

    def delete_conversation(self, conv_id: str) -> None:
        """删除会话：清空该会话的全部消息，并把会话标记为 hidden（防止被自动重新列出）。"""
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM message WHERE conv_id=?", (conv_id,))
            self._conn.execute(
                "INSERT OR IGNORE INTO conversation(conv_id,title,created_at,updated_at,hidden)"
                " VALUES(?,?,?,?,1)",
                (conv_id, "", now, now),
            )
            self._conn.execute(
                "UPDATE conversation SET hidden=1, updated_at=? WHERE conv_id=?",
                (now, conv_id),
            )
            self._conn.commit()

    def hidden_conversation_ids(self) -> set:
        """被删除（隐藏）的会话 id 集合，供 UI 过滤掉仍在线但不该出现的对端。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT conv_id FROM conversation WHERE hidden=1"
            ).fetchall()
        return {r["conv_id"] for r in rows}

    def has_conversation(self, conv_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM conversation WHERE conv_id=?", (conv_id,)
            ).fetchone()
        return row is not None

    def rebind_conversation(self, old_id: str, new_id: str, title: Optional[str] = None) -> None:
        """把「手动 IP 合成会话」升级成真实设备会话：消息整批迁移，旧的合成行删掉。"""
        if old_id == new_id:
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE message SET conv_id=? WHERE conv_id=?", (new_id, old_id)
            )
            self._conn.execute("DELETE FROM conversation WHERE conv_id=?", (old_id,))
            self._conn.execute(
                "INSERT OR IGNORE INTO conversation(conv_id,title,created_at,updated_at,hidden)"
                " VALUES(?,?,?,?,0)",
                (new_id, title or "", now, now),
            )
            if title:
                self._conn.execute(
                    "UPDATE conversation SET title=?, updated_at=?, hidden=0 WHERE conv_id=?",
                    (title, now, new_id),
                )
            else:
                self._conn.execute(
                    "UPDATE conversation SET updated_at=?, hidden=0 WHERE conv_id=?",
                    (now, new_id),
                )
            self._conn.commit()

    # ------------------------------------------------------------------ 传输
    def upsert_transfer(self, t: Transfer) -> None:
        file = t.file
        fid = file.file_id if file else t.transfer_id
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO transfer"
                "(file_id,group_id,peer_id,direction,state,name,rel_path,size,received,"
                " final_path,error,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fid, t.transfer_id, t.peer_id, t.direction,
                    t.state.value if isinstance(t.state, TransferState) else str(t.state),
                    file.name if file else "", file.rel_path if file else "",
                    file.size if file else 0,
                    int(t.received if t.direction == "recv" else t.sent),
                    t.final_path, t.error, time.time(),
                ),
            )
            self._conn.commit()

    def transfers(self, limit: int = 300) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM transfer ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def transfer(self, file_id: str) -> Optional[dict]:
        """按 file_id 取一条传输记录（删除时用来找本地路径）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM transfer WHERE file_id=?", (file_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_transfer(self, file_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM transfer WHERE file_id=?", (file_id,))
            self._conn.commit()

    def delete_transfers(self, file_ids: List[str]) -> int:
        """批量删除传输记录，返回删掉的条数。"""
        if not file_ids:
            return 0
        with self._lock:
            placeholders = ",".join("?" * len(file_ids))
            cur = self._conn.execute(
                f"DELETE FROM transfer WHERE file_id IN ({placeholders})",
                list(file_ids),
            )
            self._conn.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------ 设置
    def setting_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def setting_set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO setting(key,value) VALUES (?,?)", (key, value))
            self._conn.commit()


def _file_from_dict(obj: dict) -> FileEntry:
    return FileEntry(
        file_id=obj.get("file_id", ""),
        name=obj.get("name", ""),
        rel_path=obj.get("rel_path", ""),
        root_name=obj.get("root_name"),
        size=int(obj.get("size", 0)),
        kind=obj.get("kind", "file"),
        content_id=obj.get("content_id", ""),
        chunk_size=int(obj.get("chunk_size", 65536)),
        total_chunks=int(obj.get("total_chunks", 0)),
        preview=obj.get("preview"),
        mtime=float(obj.get("mtime", 0.0)),
        local_path=obj.get("local_path"),
    )
