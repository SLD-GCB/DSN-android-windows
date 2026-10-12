package com.dasinong.backend.security

import javax.crypto.AEADBadTagException
import javax.crypto.Cipher
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * AES/GCM 的**帧格式**与加解密细节（供 [AndroidKeyStoreSecretStore] 与测试实现共用）。
 *
 * 格式：`iv(12 字节) || 密文+tag`。GCM 自带完整性校验，密文被篡改会在解密时抛
 * `AEADBadTagException` —— 统一包成 [SecretStoreError]。
 */
object AesGcmCodec {

    const val IV_LEN = 12
    const val TAG_BITS = 128
    const val TRANSFORM = "AES/GCM/NoPadding"

    fun seal(key: SecretKey, plain: ByteArray): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORM)
        cipher.init(Cipher.ENCRYPT_MODE, key)
        val body = cipher.doFinal(plain)
        return cipher.iv + body
    }

    fun open(key: SecretKey, blob: ByteArray): ByteArray {
        if (blob.size <= IV_LEN) throw SecretStoreError("密文太短，不可能是本机加密的数据")
        val iv = blob.copyOfRange(0, IV_LEN)
        val body = blob.copyOfRange(IV_LEN, blob.size)
        val cipher = Cipher.getInstance(TRANSFORM)
        return try {
            cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(TAG_BITS, iv))
            cipher.doFinal(body)
        } catch (e: AEADBadTagException) {
            throw SecretStoreError("密文校验失败（可能被篡改）", e)
        } catch (e: SecretStoreError) {
            throw e
        } catch (e: Exception) {
            throw SecretStoreError("解密失败：${e.message}", e)
        }
    }
}
