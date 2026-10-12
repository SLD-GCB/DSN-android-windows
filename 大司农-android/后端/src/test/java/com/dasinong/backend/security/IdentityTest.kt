package com.dasinong.backend.security

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 每设备长期身份：**跨语言固定向量** + 签名/验签 + 加密落盘。
 *
 * ⚠ `fp` / `spkiLen` / `spkiPrefix` 必须与电脑端 `tests/test_identity.py` 里的
 * 同名常量一致 —— 它证明两端的 P-256 与 SPKI DER 编码完全一致。
 */
class IdentityTest {

    private val seed = ByteArray(32) { it.toByte() }        // 0x00..0x1f

    private val fp = "2c69f1bdbe45a8a6ab3b6abd66f8d4632abfb434998d4f3b6686972d5ea3c881"
    private val spkiLen = 91
    private val spkiPrefix = "3059301306072a8648ce3d02"

    @Test
    fun fromSeedVector() {
        val ident = Identity.fromSeed(seed)
        assertEquals(spkiLen, ident.spkiDer.size)
        assertEquals(spkiPrefix, Identity.hex(ident.spkiDer.copyOf(12)))
        assertEquals(fp, ident.fingerprint)
    }

    @Test
    fun identityIsStableAndDistinct() {
        assertEquals(fp, Identity.fromSeed(seed).fingerprint)         // 同种子 → 同身份
        assertNotEquals(fp, Identity.generate().fingerprint)         // 随机身份各不相同
    }

    @Test
    fun signAndVerify() {
        val a = Identity.fromSeed(seed)
        val b = Identity.generate()
        val msg = "hello".toByteArray()
        val sig = a.sign(msg)

        assertTrue(Identity.verify(a.spkiDer, msg, sig))
        assertFalse(Identity.verify(a.spkiDer, "hello!".toByteArray(), sig))   // 改消息
        assertFalse(Identity.verify(b.spkiDer, msg, sig))                     // 换公钥
        val tampered = sig.copyOf().also { it[it.size - 1] = (it[it.size - 1].toInt() xor 0x01).toByte() }
        assertFalse(Identity.verify(a.spkiDer, msg, tampered))                // 改签名
        assertFalse(Identity.verify("not-a-key".toByteArray(), msg, sig))     // 垃圾公钥不抛异常
    }

    @Test
    fun loadOrCreateRoundTrip() {
        val dir = tempDir()
        val store = JvmAesGcmSecretStore.random()
        val (a, reused1) = Identity.loadOrCreate(dir, store)
        assertFalse(reused1)
        val (b, reused2) = Identity.loadOrCreate(dir, store)
        assertTrue("第二次应沿用已有身份", reused2)
        assertEquals(a.fingerprint, b.fingerprint)

        // 落盘的是密文，不该出现裸种子
        assertFalse(dir.resolve(Identity.KEY_FILE).readBytes().contentEquals(a.seed))
    }

    @Test
    fun keyStoreResetMeansNewIdentity() {
        val dir = tempDir()
        val (a, _) = Identity.loadOrCreate(dir, JvmAesGcmSecretStore.random())
        // 换一个 store ≈ 系统密钥库被重置 → 解不开 → 生成新身份（对端需重新配对）
        val (c, reused) = Identity.loadOrCreate(dir, JvmAesGcmSecretStore.random())
        assertFalse(reused)
        assertNotEquals(a.fingerprint, c.fingerprint)
    }

    @Test
    fun brokenIdentityFileRegenerates() {
        val dir = tempDir()
        val store = JvmAesGcmSecretStore.random()
        val (a, _) = Identity.loadOrCreate(dir, store)
        dir.resolve(Identity.KEY_FILE).writeBytes("garbage".toByteArray())
        val (b, reused) = Identity.loadOrCreate(dir, store)
        assertFalse("解不开 → 应生成新身份", reused)
        assertNotEquals(a.fingerprint, b.fingerprint)
    }

    private fun tempDir(): java.io.File =
        java.nio.file.Files.createTempDirectory("dasinong-identity-test").toFile()
}
