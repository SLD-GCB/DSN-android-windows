package com.dasinong.backend.security

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 敏感数据加密存储的帧格式测试（走 [AesGcmCodec]，与 Android 侧同一份实现）。
 *
 * ⚠ `AndroidKeyStore` 在 JVM 里没有实现，所以这里用 [JvmAesGcmSecretStore] 验证
 * **格式与失败语义**；密钥库本身的可用性只能在真机上验。
 */
class SecretStoreTest {

    @Test
    fun roundTrip() {
        val store = JvmAesGcmSecretStore.random()
        val plain = "隧道口令或设备私钥".toByteArray()
        val blob = store.protect(plain)
        assertFalse("密文不该等于明文", blob.contentEquals(plain))
        assertArrayEquals(plain, store.unprotect(blob))
    }

    @Test
    fun emptyAndBinaryInputs() {
        val store = JvmAesGcmSecretStore.random()
        for (plain in listOf(ByteArray(0), ByteArray(1), ByteArray(256) { it.toByte() })) {
            assertArrayEquals(plain, store.unprotect(store.protect(plain)))
        }
    }

    @Test
    fun frameIsIvThenCiphertext() {
        val store = JvmAesGcmSecretStore.random()
        val plain = ByteArray(10)
        val blob = store.protect(plain)
        // iv(12) + 密文(10) + tag(16)
        assertEquals(AesGcmCodec.IV_LEN + plain.size + 16, blob.size)
    }

    @Test
    fun ciphertextDiffersPerCall() {
        val store = JvmAesGcmSecretStore.random()
        assertFalse("每次加密都要用新 IV", store.protect(ByteArray(4)).contentEquals(store.protect(ByteArray(4))))
    }

    @Test
    fun tamperedCiphertextFails() {
        val store = JvmAesGcmSecretStore.random()
        val blob = store.protect("secret".toByteArray())
        blob[blob.size - 1] = (blob[blob.size - 1].toInt() xor 0xFF).toByte()
        try {
            store.unprotect(blob)
            throw AssertionError("篡改后必须解密失败")
        } catch (expected: SecretStoreError) {
            // 期望
        }
    }

    @Test
    fun wrongKeyFails() {
        val a = JvmAesGcmSecretStore.random()
        val b = JvmAesGcmSecretStore.random()
        val blob = a.protect("secret".toByteArray())
        try {
            b.unprotect(blob)
            throw AssertionError("换密钥后必须解密失败")
        } catch (expected: SecretStoreError) {
            // 期望
        }
    }

    @Test
    fun truncatedBlobFails() {
        val store = JvmAesGcmSecretStore.random()
        try {
            store.unprotect(ByteArray(3))
            throw AssertionError("太短的输入必须直接失败")
        } catch (expected: SecretStoreError) {
            // 期望
        }
    }

    @Test
    fun identityLostCanStillBeCaughtAsSecretStoreError() {
        // 约定：调用方按 SecretStoreError 兜底，再按 SecretStoreIdentityLost 区分"身份丢了"
        val caught = try {
            throw SecretStoreIdentityLost("本机密钥已失效")
        } catch (e: SecretStoreError) {
            e
        }
        assertTrue(caught is SecretStoreIdentityLost)
    }
}
