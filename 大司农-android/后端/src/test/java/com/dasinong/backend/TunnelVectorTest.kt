package com.dasinong.backend

import com.dasinong.backend.net.Tunnel
import com.dasinong.backend.net.TunnelContext
import com.dasinong.backend.protocol.FrameCodec
import com.dasinong.backend.protocol.FrameType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Test
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import kotlin.concurrent.thread

/**
 * 加密隧道（dsn-tunnel v3）单测。
 *
 * ⚠ 固定向量与电脑端 `tests/test_tunnel.py` **必须一致** —— 这是跨语言一致性的锚点：
 *   任何一端动了盐/迭代/曲线/DER 编码，这里或那边就会红。
 */
class TunnelVectorTest {

    private val pw = "dasinong-test-2026"
    private val pwCn = "中文口令测试"

    // ── 固定向量（与 test_tunnel.py 一一对应，改动即跨端不兼容）──
    private val pwKdf8 = "2bb8681617c797d9"
    private val pwFp = "a836bcb66ecfef2096fcea5cb1c1760a698fb18bdbf95eae7eef5bf1b810ea80"
    private val pwCnFp = "fb0b9bab22de0e8d7c46d3567609c6ee98129cdf81541e53fc68e3d15fb6138f"

    @Test
    fun kdfVector() {
        val key = Tunnel.deriveKey(pw)
        assertEquals(32, key.size)
        assertEquals(pwKdf8, Tunnel.hex(key.copyOf(8)))
    }

    @Test
    fun fingerprintVector() {
        assertEquals(pwFp, Tunnel.fingerprintOf(pw))
    }

    @Test
    fun fingerprintVectorChinese() {
        assertEquals(pwCnFp, Tunnel.fingerprintOf(pwCn))
    }

    @Test
    fun fingerprintStableAndDistinct() {
        assertEquals(Tunnel.fingerprintOf(pw), Tunnel.fingerprintOf(pw))
        assertNotEquals(Tunnel.fingerprintOf(pw), Tunnel.fingerprintOf("another-passphrase"))
    }

    @Test
    fun emptyPassphraseMeansNoFingerprint() {
        assertEquals("", Tunnel.fingerprintOf(""))
        val ctx = TunnelContext("")
        assertEquals(false, ctx.enabled)
    }

    @Test
    fun helloFieldsVector() {
        assertEquals(
            "dev-1|我的电脑|windows|37021|37020|1730000000123",
            Tunnel.helloFields("dev-1", "我的电脑", "windows", 37021, 37020, 1730000000123L),
        )
    }

    @Test
    fun constantsAndDomainSeparation() {
        assertEquals("DSN1", Tunnel.MAGIC_TEXT)
        assertEquals("dasinong-tunnel-v1", Tunnel.SALT_TEXT)
        assertEquals(200_000, Tunnel.ITERATIONS)
        // 域分离：发现签名与 AUTH 握手必须用不同的密钥
        assertNotEquals(
            Tunnel.hex(Tunnel.keyFor(pw, "hello")),
            Tunnel.hex(Tunnel.keyFor(pw, "auth")),
        )
    }

    @Test
    fun contextBuildsCertificate() {
        val ctx = TunnelContext(pw)
        assertEquals(true, ctx.enabled)
        assertEquals(pwFp, ctx.fingerprint)
    }

    // ── 真实 TLS 环回（验证自签证书 / pinning / 握手）──

    /** 返回 (服务端是否收到业务帧, 出错信息)。 */
    private fun loopback(pwServer: String, pwClient: String): Pair<Boolean, Exception?> {
        val srv = ServerSocket(0, 1, InetAddress.getByName("127.0.0.1"))
        val port = srv.localPort
        val ctxServer = TunnelContext(pwServer)
        val ctxClient = TunnelContext(pwClient)

        var gotBusinessFrame = false
        var serverErr: Exception? = null
        val worker = thread(isDaemon = true) {
            try {
                val raw = srv.accept()
                val s = ctxServer.accept(raw)
                val frame = FrameCodec.readFrame(s.inputStream)
                gotBusinessFrame = frame != null && frame.type == FrameType.JSON
                s.close()
            } catch (e: Exception) {
                serverErr = e
            }
        }

        var clientErr: Exception? = null
        try {
            val raw = Socket("127.0.0.1", port)
            val s = ctxClient.connect(raw, "127.0.0.1")
            FrameCodec.writeFrame(s.outputStream, FrameType.JSON, "{\"type\":\"connect\"}".toByteArray())
            s.close()
        } catch (e: Exception) {
            clientErr = e
        }
        worker.join(20_000)
        srv.close()
        return gotBusinessFrame to (clientErr ?: serverErr)
    }

    @Test
    fun tlsLoopbackSamePassphrase() {
        val (ok, err) = loopback(pw, pw)
        assertEquals("不应报错：$err", null, err)
        assertEquals(true, ok)
    }

    @Test
    fun tlsLoopbackPlaintextBothEmpty() {
        val (ok, err) = loopback("", "")
        assertEquals(null, err)
        assertEquals(true, ok)
    }

    @Test
    fun tlsLoopbackWrongPassphraseRejected() {
        val (ok, err) = loopback(pw, "wrong-passphrase")
        assertNotNull("口令不一致必须被拒", err)
        assertEquals(false, ok)
    }
}
