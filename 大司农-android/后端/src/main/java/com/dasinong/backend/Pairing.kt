package com.dasinong.backend

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

/**
 * 配对：一次性人工确认 + 长期信任。
 *
 * v4 的身份是**每设备独立**的，所以"这台设备我认不认识"需要一个明确的人工步骤：
 *  - 首次连接时，两端各自算出由**双方公钥 + 本次握手 nonce** 派生的**同一个 6 位短码**；
 *  - 人对得上再点「配对」；**两端都同意**才写入信任（不产生"半信任"）；
 *  - 之后长期信任，直到用户在设置里解绑。
 *
 * ⚠ 刻意**不显示任何指纹**：短码是每次会话不同的临时值，不是身份指纹。
 */
object Pairing {

    /** 等用户确认的超时（毫秒）。 */
    const val TIMEOUT_MS = 60_000L

    /** 无头自测用：环境变量置 1 则自动接受。**真实运行绝不设置**。 */
    const val AUTO_ENV = "DASINONG_PAIR_AUTO"

    /**
     * 两端必须算出**同一个** 6 位码。
     *
     * ```
     * digest = SHA-256("dasinong-sas-v1" || 较小指纹 || 较大指纹 || nonce)
     * code   = 大端无符号(digest[0:4]) % 1_000_000     # 左补零到 6 位
     * ```
     *
     * 含**双方公钥** → 中间人各用自己密钥分别握手时，两端算出的码不同，人工核对必然发现；
     * 含 **nonce** → 每次会话不同，防重放别人确认过的码。
     */
    fun sasCode(fpA: String, fpB: String, nonce: ByteArray): String {
        val a = hexToBytes(fpA)
        val b = hexToBytes(fpB)
        val (lo, hi) = if (compareUnsigned(a, b) <= 0) a to b else b to a
        val md = MessageDigest.getInstance("SHA-256")
        md.update("dasinong-sas-v1".toByteArray(Charsets.UTF_8))
        md.update(lo)
        md.update(hi)
        md.update(nonce)
        val d = md.digest()
        // 大端无符号 32 位（Kotlin 的 Int 有符号 → 用 Long 再取模）
        val u32 = ((d[0].toLong() and 0xFF) shl 24) or ((d[1].toLong() and 0xFF) shl 16) or
            ((d[2].toLong() and 0xFF) shl 8) or (d[3].toLong() and 0xFF)
        return "%06d".format(u32 % 1_000_000L)
    }

    private fun compareUnsigned(a: ByteArray, b: ByteArray): Int {
        val n = minOf(a.size, b.size)
        for (i in 0 until n) {
            val x = a[i].toInt() and 0xFF
            val y = b[i].toInt() and 0xFF
            if (x != y) return x - y
        }
        return a.size - b.size
    }

    private fun hexToBytes(s: String): ByteArray =
        ByteArray(s.length / 2) { ((s[it * 2].digitToInt(16) shl 4) or s[it * 2 + 1].digitToInt(16)).toByte() }
}

/**
 * 把「要不要跟这台设备配对」抛给 UI，并等它的回答。
 *
 * Android 侧用 `CompletableDeferred`：等待是**挂起**而不是占线程（网络逻辑跑在协程里）。
 */
class PairingManager(private val scope: CoroutineScope) {

    private val pending = ConcurrentHashMap<String, CompletableDeferred<Boolean>>()

    /** 无头自测：自动接受。 */
    val auto: Boolean = System.getenv(Pairing.AUTO_ENV) == "1"

    /** 发事件问 UI 并等回答（最多 [Pairing.TIMEOUT_MS]）；超时/被顶掉 → 视为拒绝。 */
    suspend fun ask(peerId: String, peerName: String, sas: String, onAsk: (PairRequest) -> Unit): Boolean {
        if (auto) return true
        val deferred = CompletableDeferred<Boolean>()
        pending.put(peerId, deferred)?.complete(false)      // 同一对端的旧询问作废
        onAsk(PairRequest(peerId, peerName, sas))
        val answer = withTimeoutOrNull(Pairing.TIMEOUT_MS) { deferred.await() }
        pending.remove(peerId, deferred)
        return answer ?: false
    }

    /** UI 的回答入口（点「配对」/「取消」时调用）。 */
    fun answer(peerId: String, accept: Boolean) {
        pending[peerId]?.complete(accept)
    }

    /** 供非挂起上下文触发一次询问（结果通过回调返回）。 */
    fun askAsync(
        peerId: String,
        peerName: String,
        sas: String,
        onAsk: (PairRequest) -> Unit,
        onAnswer: (Boolean) -> Unit,
    ) {
        scope.launch { onAnswer(ask(peerId, peerName, sas, onAsk)) }
    }
}

/** 一次「需要用户确认配对」的请求（UI 侧转成 Compose 状态）。 */
data class PairRequest(
    val peerId: String,
    val name: String,
    val sas: String,
)
