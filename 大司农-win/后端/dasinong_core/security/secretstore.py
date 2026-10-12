"""敏感数据的静态加密存储抽象。

用途：本机需要**落盘保存**的敏感值（当前是隧道口令；v4 起是设备私钥）。

- Windows：`DpapiStore` —— 密文绑定当前 Windows 用户，换用户/换机器解不开；
- 其它平台或 DPAPI 不可用：`PlaintextStore` —— **不提供任何保护**，只保证功能不崩，
  调用方应按 `store.name == "plaintext"` 给出明确告警。
"""
from __future__ import annotations

import functools
import logging
from typing import Protocol, runtime_checkable

from . import dpapi

_log = logging.getLogger(__name__)


class SecretStoreError(RuntimeError):
    """加解密失败（平台不支持、密文被篡改、换了用户等）。"""


@runtime_checkable
class SecretStore(Protocol):
    #: 实现名，用于判断是否真的加密（"dpapi" / "plaintext"）
    name: str

    def protect(self, plain: bytes) -> bytes:
        """把明文变成可落盘的密文。"""

    def unprotect(self, blob: bytes) -> bytes:
        """把 `protect` 的结果还原；`SecretStoreError` 表示解不开。"""


class DpapiStore:
    """Windows DPAPI 实现（用户作用域）。"""

    name = "dpapi"

    def protect(self, plain: bytes) -> bytes:
        return dpapi.protect(plain)

    def unprotect(self, blob: bytes) -> bytes:
        return dpapi.unprotect(blob)


class PlaintextStore:
    """降级实现：不做任何加密。仅用于"总比崩了好"。"""

    name = "plaintext"

    def protect(self, plain: bytes) -> bytes:
        return plain

    def unprotect(self, blob: bytes) -> bytes:
        return blob


@functools.lru_cache(maxsize=1)
def get_secret_store(*, allow_plaintext_fallback: bool = True) -> SecretStore:
    """取当前平台可用的加密存储（进程内缓存）。

    `allow_plaintext_fallback=False` 时，平台不支持就抛错（用于必须加密的场景）。
    """
    if dpapi.available():
        return DpapiStore()
    if allow_plaintext_fallback:
        _log.warning("当前平台没有 DPAPI，敏感数据将以明文保存")
        return PlaintextStore()
    raise SecretStoreError("当前平台没有可用的加密存储（仅 Windows 支持 DPAPI）")
