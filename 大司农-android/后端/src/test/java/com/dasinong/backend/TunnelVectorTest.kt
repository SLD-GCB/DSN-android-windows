package com.dasinong.backend

import com.dasinong.backend.net.PeerAuth
import com.dasinong.backend.net.Tunnel
import com.dasinong.backend.net.TunnelContext
import com.dasinong.backend.security.Identity
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * 隧道（dsn-tunnel **v4**）与配对的**跨语言固定向量** + 真实 TLS 环回。
 *
 * ⚠ 这里的所有向量都必须与电脑端一致：
 *  · `helloFields` → `大司农-win/tests/test_tunnel.py`
 *  · `sasCode`     → `大司农-win/后端/dasinong_core/pairing.py::sas_code`
 *  · 身份指纹      → `大司农-win/tests/test_identity.py`
 */
class TunnelVectorTest {

    private val seedA = ByteArray(32) { it.toByte() }              // 0x00..0x1f
    private val seedB = ByteArray(32) { (31 - it).toByte() }

    @Test
    fun helloFieldsVector() {
        assertEquals(
            "dev-1|我的电脑|windows|37021|37020|1730000000123",
            Tunnel.helloFields("dev-1", "我的电脑", "windows", 37021, 37020, 1730000000123L),
        )
    }

    @Test
    fun magicConstant() {
        assertEquals("DSN1", Tunnel.MAGIC_TEXT)
    }

    /** 6 位短码：两端必须算出同一个值（与电脑端同值）。 */
    @Test
    fun sasVector() {
        val a = "00".repeat(32)
        val b = "ff".repeat(32)
        val nonce = ByteArray(16) { it.toByte() }
        assertEquals("959446", Pairing.sasCode(a, b, nonce))
        assertEquals("802553", Pairing.sasCode(a, b, ByteArray(16)))
        assertEquals("921012", Pairing.sasCode(a, b, ByteArray(16) { 0xAB.toByte() }))
        // 顺序无关：谁算都得同一个码
        assertEquals(Pairing.sasCode(a, b, nonce), Pairing.sasCode(b, a, nonce))
        // 换 nonce 就换码（防重放）
        assertNotEquals(Pairing.sasCode(a, b, nonce), Pairing.sasCode(a, b, ByteArray(16)))
    }

    @Test
    fun helloSignatureRoundTrip() {
        val tc = TunnelContext(Identity.fromSeed(seedA))
        val fields = Tunnel.helloFields("dev-1", "手机", "android", 37021, 37020, 1730000000123L)
        val sig = tc.signHello(fields)
        assertTrue(Tunnel.verifyHello(tc.spkiB64, fields, sig))
        // 改字段 / 换公钥 / 改签名 都必须失败
        assertFalse(Tunnel.verifyHello(tc.spkiB64, fields + "x", sig))
        assertFalse(Tunnel.verifyHello(TunnelContext(Identity.fromSeed(seedB)).spkiB64, fields, sig))
        assertFalse(Tunnel.verifyHello(tc.spkiB64, fields, sig.dropLast(4) + "AAAA"))
        assertFalse(Tunnel.verifyHello("", fields, sig))
    }

    /** 真 TLS 环回：两端互相认证出**对方的指纹**，且 nonce 一致（配对短码要用）。 */
    @Test
    fun tlsLoopbackMutualIdentity() {
        val a = TunnelContext(Identity.fromSeed(seedA))
        val b = TunnelContext(Identity.fromSeed(seedB))
        val srv = ServerSocket(0)
        val ready = CountDownLatch(1)
        var serverAuth: PeerAuth? = null
        var serverError: Exception? = null
        val th = Thread {
            try {
                ready.countDown()
                val raw = srv.accept()
                serverAuth = a.accept(raw)
            } catch (e: Exception) {
                serverError = e
            }
        }
        th.isDaemon = true
        th.start()
        ready.await(3, TimeUnit.SECONDS)
        val clientAuth = b.connect(Socket("127.0.0.1", srv.getLocalPort()), "127.0.0.1")
        th.join(10_000)
        srv.close()
        try {
            serverError?.let { throw it }
            assertEquals(b.fingerprint, serverAuth!!.fp)     // 服务端认证的是"客户端"
            assertEquals(a.fingerprint, clientAuth.fp)       // 客户端认证的是"服务端"
            assertEquals(16, serverAuth!!.nonce.size)
            assertTrue(serverAuth!!.nonce.contentEquals(clientAuth.nonce))
            assertNotEquals(serverAuth!!.fp, clientAuth.fp)  // 两个身份确实不同
        } finally {
            runCatching { serverAuth?.sock?.close() }
            runCatching { clientAuth.sock.close() }
        }
    }
}
