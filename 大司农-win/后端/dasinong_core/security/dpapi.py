"""Windows DPAPI 加解密封装（只依赖 stdlib `ctypes`，不引 pywin32）。

DPAPI 把密文绑定到**当前 Windows 用户**：换用户、换机器、或密文被篡改后都解不开。
这是刻意行为 —— 身份/密钥不该跟着文件走。失败一律抛 `SecretStoreError`，由调用方
决定降级还是提示用户。

- 用**用户作用域**（不传 `CRYPTPROTECT_LOCAL_MACHINE`）；
- 带 `CRYPTPROTECT_UI_FORBIDDEN`，保证后台线程里调用也**绝不弹窗**；
- 固定 entropy 只做域分隔（它不是密钥，公开也不影响安全性）；
- 出参 blob 用 `kernel32.LocalFree` 释放（DPAPI 用 LocalAlloc 分配）。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

#: 固定 entropy：域分隔用，非密钥
ENTROPY = b"dasinong-secret-v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretStoreError(RuntimeError):
    """DPAPI 不可用、密文被篡改、或换了 Windows 用户导致解不开。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def available() -> bool:
    """当前平台是否支持 DPAPI。"""
    return sys.platform == "win32"


def protect(data: bytes, entropy: bytes = ENTROPY) -> bytes:
    """加密（用户作用域）。"""
    if not available():
        raise SecretStoreError("DPAPI 仅在 Windows 上可用")
    return _crypt(data, entropy, encrypt=True)


def unprotect(blob: bytes, entropy: bytes = ENTROPY) -> bytes:
    """解密。密文被篡改、换了用户或换机器都会抛 `SecretStoreError`。"""
    if not available():
        raise SecretStoreError("DPAPI 仅在 Windows 上可用")
    return _crypt(blob, entropy, encrypt=False)


def _blob_for(data: bytes) -> tuple:
    """把 bytes 包成 DATA_BLOB；返回 (blob, buffer) —— buffer 必须持有引用直到调用结束。"""
    buf = ctypes.create_string_buffer(data if data else b"\x00", max(1, len(data)))
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _crypt(data: bytes, entropy: bytes, encrypt: bool) -> bytes:
    # WinDLL 只在 Windows 上存在，所以必须在这里取而不是模块顶层
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # CryptProtectData / CryptUnprotectData 的第二个参数类型不同
    # （LPCWSTR vs LPWSTR*），这里统一按 void* 传 NULL，故签名可共用。
    argtypes = [
        ctypes.POINTER(_DataBlob),   # pDataIn
        ctypes.c_void_p,             # szDataDescr / ppszDataDescr
        ctypes.POINTER(_DataBlob),   # pOptionalEntropy
        ctypes.c_void_p,             # pvReserved
        ctypes.c_void_p,             # pPromptStruct
        wintypes.DWORD,              # dwFlags
        ctypes.POINTER(_DataBlob),   # pDataOut
    ]
    crypt32.CryptProtectData.argtypes = argtypes
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = argtypes
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    blob_in, _keep_in = _blob_for(data)
    blob_ent, _keep_ent = _blob_for(entropy)
    blob_out = _DataBlob()

    func = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = func(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_ent),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        err = ctypes.get_last_error()
        what = "加密" if encrypt else "解密"
        raise SecretStoreError(f"DPAPI {what}失败（错误码 {err}）")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
