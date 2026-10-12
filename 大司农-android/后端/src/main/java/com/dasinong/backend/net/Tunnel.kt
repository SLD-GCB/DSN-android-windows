package com.dasinong.backend.net

import com.dasinong.backend.protocol.FrameCodec
import com.dasinong.backend.protocol.FrameType
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
import java.math.BigInteger
import java.net.Socket
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyStore
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.Date
import javax.crypto.Mac
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec
import javax.crypto.spec.SecretKeySpec
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager

/** 隧道层错误（口令不一致 / 对端不是本协议 / TLS 失败）。 */
class TunnelError(message: String) : Exception(message)

/**
 * 加密隧道（dsn-tunnel v3）—— 与电脑端 `net/tunnel.py` 对接。
 *
 * 参照 honglu-tunnel v1（`ai酒馆/酒馆隧道.py` + `鸿胪寺/后端/隧道.kt`）：
 *  · **加密**：标准 TLS（Android Conscrypt）。**密码学一行不自己造。**
 *  · **身份**：口令 --PBKDF2--> 32B 材料 --> P-256 私钥 --> 自签证书。
 *    两端用同一口令推出**同一把公钥**，本端**只钉这个指纹** —— 不知道口令的人
 *    推不出这把公钥，也就连不上、冒充不了。
 *  · **TLS 之上再加一道 HMAC 双向认证**，给出明确的「口令不一致」报错。
 *  · **口令为空 = 不加密**：原样返回裸 socket。
 *
 * 口令密钥**域分离**：`keyFor(口令, 用途) = PBKDF2(口令, 盐+"|"+用途, 200000)`，
 * 用途 `hello`（发现签名，明文广播必须走 PBKDF2）/ `auth`（AUTH 握手）。
 * 身份派生用不带用途后缀的 `deriveKey(口令)`（保持与电脑端一致的指纹）。
 *
 * ⚠ 全是**阻塞**接口：给协程里 `withContext(Dispatchers.IO)` 用。
 */
object Tunnel {
    const val SALT_TEXT = "dasinong-tunnel-v1"
    const val ITERATIONS = 200_000
    const val MAGIC_TEXT = "DSN1"
    val SALT: ByteArray = SALT_TEXT.toByteArray(Charsets.UTF_8)
    val MAGIC: ByteArray = MAGIC_TEXT.toByteArray(Charsets.UTF_8)

    /** P-256 曲线阶 n。 */
    private val P256_ORDER =
        BigInteger("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)

    private val provider = BouncyCastleProvider()
    private val random = SecureRandom()

    /** 口令 → 32 字节密钥材料（PBKDF2-HMAC-SHA256，**不带用途后缀**）。用于身份派生。 */
    fun deriveKey(passphrase: String): ByteArray =
        SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
            .generateSecret(PBEKeySpec(passphrase.toCharArray(), SALT, ITERATIONS, 256)).encoded

    /** 口令 → 某用途的 HMAC 密钥（域分离）。**必须跟电脑端一致。** */
    fun keyFor(passphrase: String, purpose: String): ByteArray =
        SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(
            PBEKeySpec(
                passphrase.toCharArray(),
                SALT + "|".toByteArray(Charsets.UTF_8) + purpose.toByteArray(Charsets.UTF_8),
                ITERATIONS,
                256,
            ),
        ).encoded

    private fun curveSpec(): ECNamedCurveParameterSpec =
        ECNamedCurveTable.getParameterSpec("secp256r1") as ECNamedCurveParameterSpec

    /** 口令 → 期望公钥点（BouncyCastle 算 d·G）。 */
    fun publicKeyPoint(passphrase: String): ECPoint {
        val spec = curveSpec()
        val d = BigInteger(1, deriveKey(passphrase)).mod(spec.n.subtract(BigInteger.ONE)).add(BigInteger.ONE)
        return FixedPointCombMultiplier().multiply(spec.g, d).normalize()
    }

    /** 口令 → P-256 公钥对象（JCA）。 */
    fun publicKeyOf(passphrase: String): java.security.PublicKey {
        val spec = curveSpec()
        return KeyFactory.getInstance("EC", provider)
            .generatePublic(ECPublicKeySpec(publicKeyPoint(passphrase), spec))
    }

    private fun privateKeyOf(passphrase: String): java.security.PrivateKey {
        val spec = curveSpec()
        val d = BigInteger(1, deriveKey(passphrase)).mod(spec.n.subtract(BigInteger.ONE)).add(BigInteger.ONE)
        return KeyFactory.getInstance("EC", provider).generatePrivate(ECPrivateKeySpec(d, spec))
    }

    fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }

    fun hmacHex(key: ByteArray, text: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key, "HmacSHA256"))
        return hex(mac.doFinal(text.toByteArray(Charsets.UTF_8)))
    }

    /**
     * 口令 → **公钥指纹**（SPKI DER 的 SHA-256）。**两端必须一致。**
     * 这是给两端"钉"的那串；别用整张证书的哈希（序列号/有效期每次都变）。
     */
    fun fingerprintOf(passphrase: String): String {
        if (passphrase.isEmpty()) return ""
        return hex(MessageDigest.getInstance("SHA-256").digest(publicKeyOf(passphrase).encoded))
    }

    /** 发现报文签名的规范化字段串（**两端必须一致**）。 */
    fun helloFields(
        fromId: String,
        name: String,
        deviceType: String,
        tcpPort: Int,
        udpPort: Int,
        ts: Long,
    ): String = listOf(fromId, name, deviceType, tcpPort.toString(), udpPort.toString(), ts.toString())
        .joinToString("|")

    internal fun authMac(passphrase: String, side: String, nonce: String): String =
        if (passphrase.isEmpty()) "" else hmacHex(keyFor(passphrase, "auth"), "$side|$nonce")

    // ---- AUTH 帧的载荷：自己拼/解析，避免依赖 org.json（JVM 单测里是桩会抛 not mocked）----
    internal fun authJson(key: String, value: String): ByteArray =
        "{\"$key\":\"$value\"}".toByteArray(Charsets.UTF_8)

    internal fun authField(body: ByteArray, key: String): String {
        val text = String(body, Charsets.UTF_8)
        val m = Regex("\"" + Regex.escape(key) + "\"\\s*:\\s*\"([^\"]*)\"").find(text) ?: return ""
        return m.groupValues[1]
    }

    internal fun nonce(): String = hex(ByteArray(16).also { random.nextBytes(it) })

    // ---- 握手 ----

    fun handshakeServer(sock: Socket, passphrase: String) {
        val out = sock.getOutputStream()
        val ins = sock.getInputStream()
        try {
            out.write(MAGIC); out.flush()
            val nonce = nonce()
            FrameCodec.writeFrame(out, FrameType.AUTH_HELLO, authJson("nonce", nonce))
            val frame = FrameCodec.readFrame(ins) ?: throw TunnelError("隧道握手中连接被关闭")
            if (frame.type != FrameType.AUTH_REQ) throw TunnelError("隧道握手帧不对（预期 AUTH_REQ）")
            if (passphrase.isNotEmpty() && authField(frame.body, "mac") != authMac(passphrase, "client", nonce)) {
                FrameCodec.writeFrame(out, FrameType.AUTH_ERR, authJson("错", "口令不一致"))
                throw TunnelError("口令不一致")
            }
            FrameCodec.writeFrame(out, FrameType.AUTH_RES, authJson("mac2", authMac(passphrase, "server", nonce)))
        } catch (e: TunnelError) {
            throw e
        } catch (e: Exception) {
            throw TunnelError("隧道握手失败：${e.message ?: e.javaClass.simpleName}")
        }
    }

    fun handshakeClient(sock: Socket, passphrase: String) {
        val out = sock.getOutputStream()
        val ins = sock.getInputStream()
        try {
            val magic = FrameCodec.readExact(ins, 4) ?: throw TunnelError("隧道握手中连接被关闭")
            if (!magic.contentEquals(MAGIC)) throw TunnelError("对面不是大司农隧道（magic 不对）")
            val hello = FrameCodec.readFrame(ins) ?: throw TunnelError("隧道握手中连接被关闭")
            if (hello.type != FrameType.AUTH_HELLO) throw TunnelError("隧道握手帧不对（预期 AUTH_HELLO）")
            val nonce = authField(hello.body, "nonce")
            if (nonce.isEmpty()) throw TunnelError("隧道握手缺少 nonce")
            FrameCodec.writeFrame(out, FrameType.AUTH_REQ, authJson("mac", authMac(passphrase, "client", nonce)))
            val res = FrameCodec.readFrame(ins) ?: throw TunnelError("隧道握手中连接被关闭")
            if (res.type == FrameType.AUTH_ERR) throw TunnelError("口令与对方不一致（对方拒绝了本次认证）")
            if (res.type != FrameType.AUTH_RES) throw TunnelError("隧道握手帧不对（预期 AUTH_RES）")
            if (passphrase.isNotEmpty() && authField(res.body, "mac2") != authMac(passphrase, "server", nonce)) {
                throw TunnelError("对方未通过口令校验（口令不一致？）")
            }
        } catch (e: TunnelError) {
            throw e
        } catch (e: Exception) {
            throw TunnelError("隧道握手失败：${e.message ?: e.javaClass.simpleName}")
        }
    }
}

/** 按口令构建的隧道上下文：持有身份与 TLS 上下文，可被多线程复用。 */
class TunnelContext(val passphrase: String) {

    /** 口令为空 = 不加密（原样返回裸 socket）。 */
    val enabled: Boolean = passphrase.isNotEmpty()

    /** 本机隧道公钥指纹（口令为空则空串）。 */
    val fingerprint: String = Tunnel.fingerprintOf(passphrase)

    private val provider = BouncyCastleProvider()
    private var serverCtx: SSLContext? = null
    private var clientCtx: SSLContext? = null

    init {
        // 身份由口令派生（两端相同）→ 服务端上下文需要一张自签证书
        if (enabled) {
            val priv = privateKeyOf()
            val pub = Tunnel.publicKeyOf(passphrase)
            serverCtx = buildServerContext(priv, buildSelfSigned(priv, pub), provider)
            clientCtx = buildClientContext(fingerprint)
        }
    }

    private fun privateKeyOf(): java.security.PrivateKey {
        val spec = ECNamedCurveTable.getParameterSpec("secp256r1") as ECNamedCurveParameterSpec
        val d = BigInteger(1, Tunnel.deriveKey(passphrase)).mod(spec.n.subtract(BigInteger.ONE)).add(BigInteger.ONE)
        return KeyFactory.getInstance("EC", provider).generatePrivate(ECPrivateKeySpec(d, spec))
    }

    private fun buildSelfSigned(
        priv: java.security.PrivateKey,
        pub: java.security.PublicKey,
    ): X509Certificate {
        val name = X500Name("CN=dasinong-tunnel")
        val now = System.currentTimeMillis()
        val builder = JcaX509v3CertificateBuilder(
            name,
            BigInteger(64, SecureRandom()),
            Date(now - 86_400_000L),
            Date(now + 3650L * 86_400_000L),
            name,
            pub,
        )
        val signer = JcaContentSignerBuilder("SHA256withECDSA").setProvider(provider).build(priv)
        return JcaX509CertificateConverter().setProvider(provider).getCertificate(builder.build(signer))
    }

    private fun buildServerContext(
        priv: java.security.PrivateKey,
        cert: X509Certificate,
        provider: BouncyCastleProvider,
    ): SSLContext {
        val ks = KeyStore.getInstance("PKCS12")
        ks.load(null, null)
        ks.setKeyEntry("dasinong", priv, charArrayOf(), arrayOf(cert))
        val kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm())
        kmf.init(ks, charArrayOf())
        val ctx = SSLContext.getInstance("TLS")
        ctx.init(kmf.keyManagers, null, SecureRandom())
        return ctx
    }

    /** 客户端只认"由口令推出的那把公钥"—— 口令不一致 → 指纹对不上 → 连不上。 */
    private fun buildClientContext(expectedFingerprint: String): SSLContext {
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>?, authType: String?) = Unit

            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

            override fun checkServerTrusted(chain: Array<X509Certificate>?, authType: String?) {
                val leaf = chain?.firstOrNull() ?: throw CertificateException("对端没给证书")
                val got = Tunnel.hex(MessageDigest.getInstance("SHA-256").digest(leaf.publicKey.encoded))
                if (got != expectedFingerprint) {
                    throw CertificateException("对方身份对不上 —— 口令不一致，或有人在冒充")
                }
            }
        }
        val ctx = SSLContext.getInstance("TLS")
        ctx.init(null, arrayOf<X509TrustManager>(tm), SecureRandom())
        return ctx
    }

    private fun preferTls(s: SSLSocket) {
        val ok = s.supportedProtocols.filter { it == "TLSv1.3" || it == "TLSv1.2" }
        if (ok.isNotEmpty()) s.enabledProtocols = ok.toTypedArray()
    }

    /** 服务端侧：包 TLS + 跑握手。口令为空则原样返回。 */
    fun accept(raw: Socket): Socket {
        if (!enabled) return raw
        val ssl = try {
            (serverCtx!!.socketFactory.createSocket(raw, null, raw.port, true) as SSLSocket).apply {
                useClientMode = false
                needClientAuth = false
                preferTls(this)
                startHandshake()
            }
        } catch (e: Exception) {
            runCatching { raw.close() }
            throw TunnelError("TLS 握手失败（对方可能未开加密）：${e.message ?: e.javaClass.simpleName}")
        }
        return try {
            ssl.soTimeout = 20000
            Tunnel.handshakeServer(ssl, passphrase)
            ssl.soTimeout = 0
            ssl
        } catch (e: Exception) {
            runCatching { ssl.close() }
            throw e
        }
    }

    /** 客户端侧：包 TLS + 钉指纹 + 跑握手。口令为空则原样返回。 */
    fun connect(raw: Socket, host: String = ""): Socket {
        if (!enabled) return raw
        val ssl = try {
            (clientCtx!!.socketFactory.createSocket(raw, host, raw.port, true) as SSLSocket).apply {
                useClientMode = true
                preferTls(this)
                startHandshake()
            }
        } catch (e: Exception) {
            runCatching { raw.close() }
            throw TunnelError("TLS 握手失败（对方可能未开加密）：${e.message ?: e.javaClass.simpleName}")
        }
        return try {
            ssl.soTimeout = 20000
            Tunnel.handshakeClient(ssl, passphrase)
            ssl.soTimeout = 0
            ssl
        } catch (e: Exception) {
            runCatching { ssl.close() }
            throw e
        }
    }

    /** 给发现报文的规范化字段串做 HMAC；口令为空回空串。 */
    fun signHello(fields: String): String =
        if (enabled) Tunnel.hmacHex(Tunnel.keyFor(passphrase, "hello"), fields) else ""

    fun close() {
        // 无临时文件需要清理（证书在内存里）
    }
}
