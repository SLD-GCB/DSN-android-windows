package com.dasinong.backend.security

import org.bouncycastle.asn1.x500.X500Name
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder
import org.bouncycastle.jce.ECNamedCurveTable
import org.bouncycastle.jce.provider.BouncyCastleProvider
import org.bouncycastle.jce.spec.ECNamedCurveParameterSpec
import org.bouncycastle.jce.spec.ECPrivateKeySpec
import org.bouncycastle.jce.spec.ECPublicKeySpec
import org.bouncycastle.math.ec.ECPoint
import org.bouncycastle.math.ec.FixedPointCombMultiplier
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder
import java.io.File
import java.math.BigInteger
import java.security.KeyFactory
import java.security.MessageDigest
import java.security.PrivateKey
import java.security.PublicKey
import java.security.SecureRandom
import java.security.Signature
import java.security.cert.X509Certificate
import java.security.spec.X509EncodedKeySpec
import java.util.Date

/**
 * 本机**长期身份**：随机 32 字节种子 → P-256 私钥 + 自签证书。
 *
 * 与 v3 的关键差别：v3 的身份由**口令派生**（同口令 = 同身份、可互相冒充）；
 * v4 起是**每设备随机**、彼此不同 —— 这样「配对」（识别 / 授权 / 吊销某台设备）才有意义。
 *
 * 种子经 [SecretStore]（系统密钥库）加密后落盘；指纹 = 公钥 **SPKI DER 的 SHA-256**。
 * 曲线 / 点乘 / SPKI 编码刻意沿用 `Tunnel.kt` 里已验证跨语言一致的那套。
 */
class Identity internal constructor(
    /** 32 字节随机种子（等价于私钥材料；只在内存与加密文件里出现） */
    val seed: ByteArray,
    val privateKey: PrivateKey,
    val publicKey: PublicKey,
    val cert: X509Certificate,
) {

    /** 公钥 SPKI DER —— 与电脑端 `Identity.spki_der` 逐字节一致。 */
    val spkiDer: ByteArray get() = publicKey.encoded

    /** 公钥指纹（SPKI DER 的 SHA-256，小写 hex）。⚠ 不是整张证书的哈希。 */
    val fingerprint: String get() = hex(MessageDigest.getInstance("SHA-256").digest(spkiDer))

    /** 用本机私钥签名（ECDSA-SHA256，DER 签名）。 */
    fun sign(message: ByteArray): ByteArray =
        Signature.getInstance("SHA256withECDSA", provider).run {
            initSign(privateKey)
            update(message)
            sign()
        }

    companion object {
        private val provider = BouncyCastleProvider()
        private val random = SecureRandom()

        /** 私钥文件名（内容经 SecretStore 加密）。 */
        const val KEY_FILE = "identity.key"
        private const val CERT_CN = "dasinong-identity"
        private const val CERT_DAYS = 3650L

        private val p256Order: BigInteger = BigInteger(
            "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16,
        )

        private fun curveSpec(): ECNamedCurveParameterSpec =
            ECNamedCurveTable.getParameterSpec("secp256r1") as ECNamedCurveParameterSpec

        fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }

        /** 种子 → 标量 `d = (int(seed) mod (n-1)) + 1`（与电脑端同一算法）。 */
        private fun scalarOf(seed: ByteArray): BigInteger =
            BigInteger(1, seed).mod(p256Order.subtract(BigInteger.ONE)).add(BigInteger.ONE)

        /** 固定种子构造（跨语言测试向量的锚点）。 */
        fun fromSeed(seed: ByteArray): Identity {
            val spec = curveSpec()
            val d = scalarOf(seed)
            val point: ECPoint = FixedPointCombMultiplier().multiply(spec.g, d).normalize()
            val kf = KeyFactory.getInstance("EC", provider)
            val pub = kf.generatePublic(ECPublicKeySpec(point, spec))
            val priv = kf.generatePrivate(ECPrivateKeySpec(d, spec))
            return Identity(seed, priv, pub, buildCert(priv, pub))
        }

        fun generate(): Identity {
            val seed = ByteArray(32)
            random.nextBytes(seed)
            return fromSeed(seed)
        }

        /** 用对方**声明的公钥**验签；任何异常都当"验证失败"，不往外抛。 */
        fun verify(spkiDer: ByteArray, message: ByteArray, signature: ByteArray): Boolean = try {
            val pub = KeyFactory.getInstance("EC", provider)
                .generatePublic(X509EncodedKeySpec(spkiDer))
            Signature.getInstance("SHA256withECDSA", provider).run {
                initVerify(pub)
                update(message)
                verify(signature)
            }
        } catch (_: Exception) {
            false
        }

        /**
         * 读取已有身份；没有 / 解不开就新建并落盘。
         *
         * 返回 `(身份, 是否沿用已有)`。`false` = **新生成**：可能是首次运行，也可能是
         * 老身份丢了（重装 / 清数据 / 换机）—— 后者需要提示用户重新配对。
         */
        fun loadOrCreate(dataDir: File, store: SecretStore): Pair<Identity, Boolean> {
            val f = File(dataDir, KEY_FILE)
            if (f.isFile) {
                try {
                    val seed = store.unprotect(f.readBytes())
                    if (seed.size == 32) return fromSeed(seed) to true
                } catch (_: Exception) {
                    // 解不开/文件损坏 → 当作没有身份
                }
            }
            val ident = generate()
            try {
                f.parentFile?.mkdirs()
                f.writeBytes(store.protect(ident.seed))
            } catch (_: Exception) {
                // 存不下来也先能用（本次会话有效）
            }
            return ident to false
        }

        private fun buildCert(priv: PrivateKey, pub: PublicKey): X509Certificate {
            val name = X500Name("CN=$CERT_CN")
            val now = System.currentTimeMillis()
            val builder = JcaX509v3CertificateBuilder(
                name,
                BigInteger(64, random),
                Date(now - 86_400_000L),
                Date(now + CERT_DAYS * 86_400_000L),
                name,
                pub,
            )
            val signer = JcaContentSignerBuilder("SHA256withECDSA").setProvider(provider).build(priv)
            return JcaX509CertificateConverter().setProvider(provider).getCertificate(builder.build(signer))
        }
    }
}
