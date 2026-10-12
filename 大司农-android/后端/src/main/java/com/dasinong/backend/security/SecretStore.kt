package com.dasinong.backend.security

/**
 * 敏感数据的静态加密存储抽象。
 *
 * 用途：本机需要**落盘保存**的敏感值（第 1 步是隧道口令；v4 起是**设备私钥**）。
 *
 * 实现只允许用**平台 API**（`AndroidKeyStore` + `AES/GCM`）—— 不能引入
 * `androidx.security:security-crypto`：本工程要能**离线构建**（依赖只有 Gradle 缓存里那批）。
 */
interface SecretStore {

    /** 把明文变成可落盘的密文。 */
    fun protect(plain: ByteArray): ByteArray

    /** 把 [protect] 的结果还原；[SecretStoreError] 表示解不开。 */
    fun unprotect(blob: ByteArray): ByteArray
}

/** 加解密失败（平台不支持、密文被篡改等）。 */
open class SecretStoreError(message: String, cause: Throwable? = null) :
    RuntimeException(message, cause)

/**
 * 解密失败且**原因是密钥没了**（清数据/重装/换机/系统重置 KeyStore）。
 * 调用方应据此提示"本机身份已更换，需要重新配对"，而不是当成普通错误。
 */
class SecretStoreIdentityLost(message: String, cause: Throwable? = null) :
    SecretStoreError(message, cause)
