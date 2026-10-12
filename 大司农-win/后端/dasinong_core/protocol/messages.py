"""JSON 信封构造/解析 + 版本协商。

信封：{proto, type, id, from, to, ts, data}
解析时对未知字段天然忽略（dict 保留即可，业务侧按需取值）。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

from .constants import PROTO_MIN, PROTO_VERSION


def new_id() -> str:
    return str(uuid.uuid4())


def envelope(
    msg_type: str,
    from_id: str,
    to_id: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
    *,
    proto: int = PROTO_VERSION,
    msg_id: Optional[str] = None,
    ts: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "proto": proto,
        "type": msg_type,
        "id": msg_id or new_id(),
        "from": from_id,
        "to": to_id,
        "ts": int(ts) if ts is not None else int(time.time() * 1000),
        "data": data if data is not None else {},
    }


def encode(env: Dict[str, Any]) -> bytes:
    return json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode(body: bytes) -> Optional[Dict[str, Any]]:
    """解析 JSON 帧体；非法返回 None（调用方应忽略该帧）。"""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("data"), dict):
        obj["data"] = {}
    return obj


def negotiate(
    remote_proto: int,
    remote_min: int,
    remote_max: int,
    local: int = PROTO_VERSION,
    local_min: int = PROTO_MIN,
) -> int:
    """协商生效协议版本；返回 -1 表示不兼容。"""
    eff = min(local, remote_max)
    if eff < remote_min or eff < local_min:
        return -1
    return eff
