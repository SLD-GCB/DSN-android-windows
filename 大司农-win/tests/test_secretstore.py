"""敏感数据的加密存储（DPAPI）单测。

非 Windows 上 DPAPI 相关用例会跳过；`PlaintextStore` 与工厂函数的用例任何平台都跑。
"""
import pytest

from dasinong_core.security import dpapi
from dasinong_core.security.secretstore import (
    DpapiStore,
    PlaintextStore,
    get_secret_store,
)

win_only = pytest.mark.skipif(not dpapi.available(), reason="DPAPI 仅 Windows 可用")


@win_only
def test_dpapi_roundtrip():
    store = DpapiStore()
    blob = store.protect(b"hello dasinong")
    assert blob != b"hello dasinong"          # 确实被加密了
    assert store.unprotect(blob) == b"hello dasinong"


@win_only
def test_dpapi_roundtrip_edge_inputs():
    store = DpapiStore()
    for raw in (b"", "中文口令测试".encode("utf-8"), b"\x00\x01\x02", bytes(range(256))):
        assert store.unprotect(store.protect(raw)) == raw


@win_only
def test_dpapi_ciphertext_differs_per_call():
    """密文必须带随机（否则等于明文可比对）。"""
    store = DpapiStore()
    assert store.protect(b"same") != store.protect(b"same")


@win_only
def test_dpapi_rejects_tampered_blob():
    store = DpapiStore()
    blob = bytearray(store.protect(b"secret"))
    blob[len(blob) // 2] ^= 0xFF
    with pytest.raises(dpapi.SecretStoreError):
        store.unprotect(bytes(blob))


@win_only
def test_dpapi_rejects_garbage():
    with pytest.raises(dpapi.SecretStoreError):
        DpapiStore().unprotect(b"this is not a dpapi blob")


def test_plaintext_store_passthrough():
    s = PlaintextStore()
    assert s.name == "plaintext"
    assert s.protect(b"x") == b"x"
    assert s.unprotect(b"x") == b"x"


def test_get_secret_store_matches_platform():
    """工厂返回的实现要跟平台能力一致（Windows→DPAPI，其它→明文降级）。"""
    store = get_secret_store()
    if dpapi.available():
        assert isinstance(store, DpapiStore) and store.name == "dpapi"
    else:
        assert isinstance(store, PlaintextStore)
