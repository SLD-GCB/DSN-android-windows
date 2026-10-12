"""配对：一次性人工确认 + 长期信任。

v4 的身份是**每设备独立**的，所以"这台设备我认不认识"需要一个明确的人工步骤：

- 首次连接时，两端各自算出由**双方公钥 + 本次握手 nonce** 派生的**同一个 6 位短码**；
- 人对得上再点「配对」；**两端都同意**才写入信任（不产生"半信任"）；
- 之后长期信任，直到用户在设置里解绑。

⚠ 刻意**不显示任何指纹**（用户硬要求）：短码是每次会话不同的临时值，不是身份指纹。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import threading
from typing import Dict

from .events import EventBus, EventType

#: 等用户确认的超时（秒）
PAIR_TIMEOUT = 60.0
#: 无头自测用：置 1 则自动接受。**真实运行绝不设置**。
AUTO_ENV = "DASINONG_PAIR_AUTO"


def sas_code(fp_a: str, fp_b: str, nonce: bytes) -> str:
    """两端必须算出**同一个** 6 位码。

    输入定长 + 固定前缀 + 大端无符号取模，保证 Python / Kotlin 逐字节一致：

    ```
    digest = SHA256("dasinong-sas-v1" || 较小指纹 || 较大指纹 || nonce)
    code   = 大端无符号(digest[0:4]) % 1_000_000     # 左补零到 6 位
    ```

    含**双方公钥** → 中间人各用自己密钥分别握手时，两端算出的码不同，人工核对必然发现；
    含 **nonce** → 每次会话不同，防重放别人确认过的码。
    """
    a, b = sorted([bytes.fromhex(fp_a), bytes.fromhex(fp_b)])
    digest = hashlib.sha256(b"dasinong-sas-v1" + a + b + nonce).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


class PairingManager:
    """把「要不要跟这台设备配对」抛给 UI，并等它的回答。

    网络线程本来就在后台线程里，所以这里**阻塞等待**是安全的（`Future`）。
    绝不能改用 `QEventLoop` —— 那会把 UI 线程卡住。
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._lock = threading.Lock()
        self._pending: Dict[str, concurrent.futures.Future] = {}
        #: 无头自测：自动接受
        self.auto = os.environ.get(AUTO_ENV) == "1"

    def ask(self, peer_id: str, peer_name: str, sas: str) -> bool:
        """发事件问 UI，并等回答（最多 `PAIR_TIMEOUT`）。超时/被顶掉 → 视为拒绝。"""
        if self.auto:
            return True
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            old = self._pending.get(peer_id)
            if old is not None and not old.done():
                old.set_result(False)          # 同一对端的旧询问作废
            self._pending[peer_id] = fut
        self._bus.publish(EventType.PAIR_REQUEST, {
            "peer_id": peer_id, "name": peer_name, "sas": sas,
        })
        try:
            return bool(fut.result(timeout=PAIR_TIMEOUT))
        except Exception:
            return False
        finally:
            with self._lock:
                if self._pending.get(peer_id) is fut:
                    self._pending.pop(peer_id, None)

    def answer(self, peer_id: str, accept: bool) -> None:
        """UI 的回答入口（点「配对」/「取消」时调用）。"""
        with self._lock:
            fut = self._pending.get(peer_id)
        if fut is not None and not fut.done():
            fut.set_result(bool(accept))

    def pending_peer(self) -> str:
        """当前正在等确认的对端（UI 用；无则空串）。"""
        with self._lock:
            for pid, fut in self._pending.items():
                if not fut.done():
                    return pid
        return ""
