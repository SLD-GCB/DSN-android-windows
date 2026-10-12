"""敏感数据的静态加密存储。

- `secretstore.get_secret_store()` 返回一个 `protect`/`unprotect` 的对象；
- Windows 走 **DPAPI**（密文绑定当前 Windows 用户）；
- 其它平台/DPAPI 不可用时降级为明文并告警（调用方可用 `store.name` 判断并提示用户）。
"""
from .secretstore import (
    DpapiStore,
    PlaintextStore,
    SecretStore,
    SecretStoreError,
    get_secret_store,
)

__all__ = [
    "DpapiStore",
    "PlaintextStore",
    "SecretStore",
    "SecretStoreError",
    "get_secret_store",
]
