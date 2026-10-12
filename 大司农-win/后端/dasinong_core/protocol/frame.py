"""帧编解码：u32 len(BE) + u8 type + body。

控制报文 body 为 UTF-8 JSON（type=0x01），
数据通道 body 为各二进制结构（type=0x02/0x10/0x11/0x12/0x7F）。
"""
from __future__ import annotations

import socket
import struct
from enum import IntEnum
from typing import Optional, Tuple

from .constants import MAX_FRAME


class ProtocolError(Exception):
    """帧层协议错误（长度非法 / 连接中途关闭等）。"""


class FrameType(IntEnum):
    JSON = 0x01
    # ---- 隧道认证（TLS 之上、业务首帧之前；见 net/tunnel.py）----
    AUTH_HELLO = 0x03
    AUTH_REQ = 0x04
    AUTH_RES = 0x05
    AUTH_ERR = 0x06
    DATA_HELLO = 0x02
    CHUNK = 0x10
    CHUNK_ACK = 0x11
    DATA_DONE = 0x12
    DATA_ERROR = 0x7F


# ---------------------------------------------------------------- 基础收发

def recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """读取恰好 n 字节；连接在起始处关闭返回 None，中途关闭抛 ProtocolError。"""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if not buf:
                return None
            raise ProtocolError("connection closed mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def pack_frame(frame_type: int, body: bytes) -> bytes:
    if len(body) + 1 > MAX_FRAME:
        raise ProtocolError(f"frame too large: {len(body) + 1} > {MAX_FRAME}")
    return struct.pack(">IB", len(body) + 1, frame_type) + body


def write_frame(sock: socket.socket, frame_type: int, body: bytes) -> None:
    sock.sendall(pack_frame(frame_type, body))


def read_frame(sock: socket.socket) -> Optional[Tuple[int, bytes]]:
    """读取一帧，返回 (type, body)；连接正常结束返回 None。"""
    head = recv_exact(sock, 5)
    if head is None:
        return None
    length, frame_type = struct.unpack(">IB", head)
    if length < 1 or length > MAX_FRAME:
        raise ProtocolError(f"bad frame length: {length}")
    body_len = length - 1
    if body_len == 0:
        return frame_type, b""
    body = recv_exact(sock, body_len)
    if body is None:
        raise ProtocolError("eof mid-frame body")
    return frame_type, body


def write_json_frame(sock: socket.socket, payload: bytes) -> None:
    write_frame(sock, FrameType.JSON, payload)


# ---------------------------------------------------------------- 数据通道结构

def pack_len_prefixed(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def pack_data_hello(device_id: str, transfer_id: str, file_id: str) -> bytes:
    return (
        pack_len_prefixed(device_id)
        + pack_len_prefixed(transfer_id)
        + pack_len_prefixed(file_id)
    )


def parse_data_hello(body: bytes) -> Tuple[str, str, str]:
    pos = 0

    def read_str() -> str:
        nonlocal pos
        if pos + 2 > len(body):
            raise ProtocolError("truncated DATA_HELLO")
        (n,) = struct.unpack_from(">H", body, pos)
        pos += 2
        if pos + n > len(body):
            raise ProtocolError("truncated DATA_HELLO str")
        s = body[pos : pos + n].decode("utf-8", "replace")
        pos += n
        return s

    device_id = read_str()
    transfer_id = read_str()
    file_id = read_str()
    return device_id, transfer_id, file_id


def pack_chunk(offset: int, payload: bytes) -> bytes:
    return struct.pack(">Q", offset) + payload


def parse_chunk(body: bytes) -> Tuple[int, bytes]:
    if len(body) < 8:
        raise ProtocolError("truncated CHUNK")
    (offset,) = struct.unpack_from(">Q", body, 0)
    return offset, body[8:]


def pack_chunk_ack(cumulative_offset: int) -> bytes:
    return struct.pack(">Q", cumulative_offset)


def parse_chunk_ack(body: bytes) -> int:
    if len(body) < 8:
        raise ProtocolError("truncated CHUNK_ACK")
    (offset,) = struct.unpack_from(">Q", body, 0)
    return offset


def pack_data_done(total_size: int, sha256_digest: bytes) -> bytes:
    if len(sha256_digest) != 32:
        raise ProtocolError("sha256 must be 32 bytes")
    return struct.pack(">Q", total_size) + sha256_digest


def parse_data_done(body: bytes) -> Tuple[int, bytes]:
    if len(body) < 40:
        raise ProtocolError("truncated DATA_DONE")
    (total,) = struct.unpack_from(">Q", body, 0)
    return total, body[8:40]


def pack_data_error(code: int, message: str) -> bytes:
    raw = message.encode("utf-8")
    return struct.pack(">H", code) + raw


def parse_data_error(body: bytes) -> Tuple[int, str]:
    if len(body) < 2:
        raise ProtocolError("truncated DATA_ERROR")
    (code,) = struct.unpack_from(">H", body, 0)
    return code, body[2:].decode("utf-8", "replace")
