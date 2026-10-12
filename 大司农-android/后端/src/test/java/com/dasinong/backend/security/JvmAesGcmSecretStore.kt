package com.dasinong.backend.security

import javax.crypto.KeyGenerator
import javax.crypto.SecretKey

/**
 * **测试专用**的 [SecretStore]：用 JVM 生成的 AES 密钥走与
 * [AndroidKeyStoreSecretStore] 完全相同的 [AesGcmCodec] 帧格式。
 *
 * 为什么要它：`AndroidKeyStore` 在纯 JVM 单测里没有实现，而帧格式（`iv||ct`、
 * 篡改必失败）恰恰是必须验证的部分。把加解密细节抽到 [AesGcmCodec] 后，
 * 这里就能真正测到那份共享代码。
 */
class JvmAesGcmSecretStore(private val key: SecretKey) : SecretStore {

    override fun protect(plain: ByteArray): ByteArray = AesGcmCodec.seal(key, plain)

    override fun unprotect(blob: ByteArray): ByteArray = AesGcmCodec.open(key, blob)

    companion object {
        fun random(): JvmAesGcmSecretStore {
            val generator = KeyGenerator.getInstance("AES")
            generator.init(256)
            return JvmAesGcmSecretStore(generator.generateKey())
        }
    }
}
