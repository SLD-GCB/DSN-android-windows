package com.dasinong.backend.security

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.security.UnrecoverableKeyException
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * 用**系统密钥库**（`AndroidKeyStore`）里的一把 AES-256 密钥做加解密。
 *
 * - 密钥由系统生成并保管，**不出密钥库**；应用只拿到一个句柄，密文落库/落盘。
 * - 刻意**不设** `setUserAuthenticationRequired`：前台服务要在锁屏/后台也能解密。
 * - 清数据 / 重装 / 换机 / 系统重置密钥库后，密钥没了 → 抛 [SecretStoreIdentityLost]，
 *   调用方应据此提示"本机身份已更换"。
 *
 * 只用平台 API，不引入 `androidx.security:security-crypto`（本工程要求可离线构建）。
 */
class AndroidKeyStoreSecretStore(
    private val alias: String = DEFAULT_ALIAS,
) : SecretStore {

    override fun protect(plain: ByteArray): ByteArray = AesGcmCodec.seal(secretKey(), plain)

    override fun unprotect(blob: ByteArray): ByteArray = AesGcmCodec.open(secretKey(), blob)

    // ---------------------------------------------------------------- 密钥
    private fun secretKey(): SecretKey {
        val ks = loadKeyStore()
        existing(ks)?.let { return it }
        synchronized(LOCK) {
            existing(ks)?.let { return it }
            val generator = try {
                KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
            } catch (e: Exception) {
                throw SecretStoreIdentityLost("系统密钥库不可用：${e.message}", e)
            }
            generator.init(
                KeyGenParameterSpec.Builder(
                    alias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setKeySize(256)
                    .build(),
            )
            return generator.generateKey()
        }
    }

    private fun loadKeyStore(): KeyStore = try {
        KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
    } catch (e: Exception) {
        throw SecretStoreIdentityLost("系统密钥库不可用：${e.message}", e)
    }

    private fun existing(ks: KeyStore): SecretKey? = try {
        (ks.getEntry(alias, null) as? KeyStore.SecretKeyEntry)?.secretKey
    } catch (e: UnrecoverableKeyException) {
        // 密钥被系统作废（例如设备重置）→ 等同于"身份丢了"
        throw SecretStoreIdentityLost("本机密钥已失效，需重新配对", e)
    }

    companion object {
        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val DEFAULT_ALIAS = "dasinong-wrap"
        private val LOCK = Any()
    }
}
