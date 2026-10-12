"""帧编解码与 JSON 信封的单测。"""
import socket
import struct

import pytest

from dasinong_core.protocol import frame, messages
from dasinong_core.protocol.constants import MAX_FRAME, PROTO_VERSION


def _pair():
    return socket.socketpair()


def test_json_frame_roundtrip():
    a, b = _pair()
    try:
        frame.write_json_frame(a, b'{"a":1}')
        ftype, body = frame.read_frame(b)
        assert ftype == frame.FrameType.JSON
        assert body == b'{"a":1}'
    finally:
        a.close()
        b.close()


def test_unknown_type_is_readable_and_skippable():
    """未知 type 仍能读满长度，调用方据 type 忽略。"""
    a, b = _pair()
    try:
        frame.write_frame(a, 0x55, b"unknown-payload")
        frame.write_json_frame(a, b'{"type":"hello"}')

        t1, body1 = frame.read_frame(b)
        assert t1 == 0x55
        assert body1 == b"unknown-payload"

        t2, body2 = frame.read_frame(b)
        assert t2 == frame.FrameType.JSON
        assert messages.decode(body2)["type"] == "hello"
    finally:
        a.close()
        b.close()


def test_bad_length_rejected():
    a, b = _pair()
    try:
        a.sendall(struct.pack(">IB", MAX_FRAME + 10, 0x01))
        with pytest.raises(frame.ProtocolError):
            frame.read_frame(b)
    finally:
        a.close()
        b.close()


def test_zero_length_rejected():
    a, b = _pair()
    try:
        a.sendall(struct.pack(">IB", 0, 0x01))
        with pytest.raises(frame.ProtocolError):
            frame.read_frame(b)
    finally:
        a.close()
        b.close()


def test_data_hello_roundtrip():
    payload = frame.pack_data_hello("dev-1", "tr-1", "f-1")
    assert frame.parse_data_hello(payload) == ("dev-1", "tr-1", "f-1")
    # 中文/非 ASCII id 也能正确编解码
    payload = frame.pack_data_hello("设备", "传输", "文件")
    assert frame.parse_data_hello(payload) == ("设备", "传输", "文件")


def test_chunk_roundtrip():
    payload = frame.pack_chunk(1048576, b"abcdef")
    offset, data = frame.parse_chunk(payload)
    assert offset == 1048576
    assert data == b"abcdef"


def test_chunk_ack_roundtrip():
    assert frame.parse_chunk_ack(frame.pack_chunk_ack(4096)) == 4096


def test_data_done_roundtrip():
    digest = bytes(range(32))
    total, got = frame.parse_data_done(frame.pack_data_done(12345, digest))
    assert total == 12345
    assert got == digest


def test_data_error_roundtrip():
    code, msg = frame.parse_data_error(frame.pack_data_error(7, "校验失败"))
    assert code == 7
    assert msg == "校验失败"


def test_data_done_requires_32_bytes():
    with pytest.raises(frame.ProtocolError):
        frame.pack_data_done(1, b"short")


def test_envelope_and_decode():
    env = messages.envelope("hello", "from-1", None, {"name": "小明"})
    assert env["proto"] == PROTO_VERSION
    assert env["type"] == "hello"
    assert env["from"] == "from-1"
    assert env["to"] is None
    assert isinstance(env["ts"], int)
    decoded = messages.decode(messages.encode(env))
    assert decoded["data"]["name"] == "小明"


def test_decode_invalid_returns_none():
    assert messages.decode(b"not json") is None
    assert messages.decode(b"[1,2,3]") is None


def test_negotiate():
    # 双方 v2 -> 2
    assert messages.negotiate(2, 1, 2) == 2
    # 远端最高 1（老版本）-> 1
    assert messages.negotiate(1, 1, 1) == 1
    # 远端最低版本高于本机最高版本 -> 不兼容
    assert messages.negotiate(PROTO_VERSION + 1, PROTO_VERSION + 1, PROTO_VERSION + 2) == -1
