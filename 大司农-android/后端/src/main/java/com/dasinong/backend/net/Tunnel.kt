package com.dasinong.backend.net

import com.dasinong.backend.protocol.FrameCodec
import com.dasinong.backend.protocol.FrameType
import com.dasinong.backend.security.Identity
import org.bouncycastle.asn1.x500.X500Name
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder
import org.bouncycastle.jce.provider.BouncyCastleProvider
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder
import java.math.BigInteger
import java.net.Socket
import java.security.KeyStore
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.Base64
import java.util.Date
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager

/** 隧道层错误（对端不是本协议 / 签名不对 / TLS 失败）。 */
class TunnelError(message: String) : Exception(message)

/** 一条**已完成身份认证**的连接。 */
class PeerAuth(
    val sock: Socket,
    /** 已认证的对端公钥指纹（SPKI DER 的 SHA-256） */
    val fp: String,
    /** 对端公钥 SPKI DER */
    val pubDer: ByteArray,
    /** 本次握手的 nonce（原始字节；配对短码要用它） */
    val nonce: ByteArray,
)

/**
 * 加密隧道（dsn-tunnel **v4**）—— 与电脑端 `net/tunnel.py` 对接。
 *
 * v3 的身份是**口令派生**（同口令 = 同身份、可互相冒充）；v4 换成**每设备随机身份**，
 * 因此握手方式必须变：
 *
 *  · **加密**：标准 TLS（Android Conscrypt）。**密码学一行不自己造。**
 *  · **两侧都不预知对端指纹** → TLS 层**不校验**证书链；身份证明放在**应用层**：
 *    双方各自用私钥对 `方向|nonce|对端指纹` 签名，对方用它**声明的公钥**验签。
 *    nonce 防重放、对端指纹防中继/降级。
 *  · **是否信任**由**配对表**决定（控制层知道 device_id 后判定）—— 握手只负责
 *    "证明你持有你自称的那把钥匙"。
 *  · 发现报文的签名也从 HMAC 改为**设备私钥签名**。
 *
 * ⚠ 全是**阻塞**接口：给协程里 `withContext(Dispatchers.IO)` 用。
 */
object Tunnel {
    const val MAGIC_TEXT = "DSN1"
    val MAGIC: ByteArray = MAGIC_TEXT.toByteArray(Charsets.UTF_8)

    private val random = SecureRandom()

    fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }

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

    /** 用 hello 里声明的公钥验发现报文的签名。任何异常都当失败。 */
    fun verifyHello(pubB64: String, fields: String, sigB64: String): Boolean = try {
        Identity.verify(
            Base64.getDecoder().decode(pubB64),
            fields.toByteArray(Charsets.UTF_8),
            Base64.getDecoder().decode(sigB64),
        )
    } catch (_: Exception) {
        false
    }

    // ---- AUTH 帧载荷：手拼/正则解析，避免依赖 org.json（JVM 单测里是桩会抛 not mocked）----
    internal fun jsonOf(vararg pairs: Pair<String, String>): ByteArray =
        pairs.joinToString(",", "{", "}") { "\"${it.first}\":\"${it.second}\"" }
            .toByteArray(Charsets.UTF_8)

    internal fun authField(body: ByteArray, key: String): String {
        val text = String(body, Charsets.UTF_8)
        val m = Regex("\"" + Regex.escape(key) + "\"\\s*:\\s*\"([^\"]*)\"").find(text) ?: return ""
        return m.groupValues[1]
    }

    internal fun newNonce(): ByteArray = ByteArray(16).also { random.nextBytes(it) }

    // ---- 握手（模块级，便于单测）----

    private fun b64(b: ByteArray): String = Base64.getEncoder().encodeToString(b)

    private fun b64d(s: String): ByteArray = Base64.getDecoder().decode(s)

    /** 服务端侧：发 magic + AUTH_HELLO{nonce,fp}，验客户端签名，回 AUTH_RES。 */
    fun handshakeServer(sock: Socket, identity: Identity): PeerAuth {
        val out = sock.getOutputStream()
        val ins = sock.getInputStream()
        try {
            out.write(MAGIC)
            out.flush()
            val nonce = newNonce()
            val nonceHex = hex(nonce)
            // `fp` 是服务端**自报**的指纹：客户端把它纳入签名，服务端再核对自己是不是它
            FrameCodec.writeFrame(
                out, FrameType.AUTH_HELLO,
                jsonOf("nonce" to nonceHex, "fp" to identity.fingerprint),
            )
            val frame = FrameCodec.readFrame(ins) ?: throw TunnelError("隧道握手中连接被关闭")
            if (frame.type != FrameType.AUTH_REQ) throw TunnelError("隧道握手帧不对（预期 AUTH_REQ）")
            val fpC = authField(frame.body, "fp")
            val pubC = b64d(authField(frame.body, "pub"))
            val sigC = b64d(authField(frame.body, "sig"))
            if (fpC.isEmpty() || Identity.hex(java.security.MessageDigest.getInstance("SHA-256").digest(pubC)) != fpC) {
                throw TunnelError("对端公钥与声明指纹不符")
            }
            val payload = "client|$nonceHex|${identity.fingerprint}".toByteArray(Charsets.UTF_8)
            if (!Identity.verify(pubC, payload, sigC)) {
                FrameCodec.writeFrame(out, FrameType.AUTH_ERR, jsonOf("错" to "对方身份签名校验未通过"))
                throw TunnelError("对方身份签名校验未通过")
            }
            val payloadS = "server|$nonceHex|$fpC".toByteArray(Charsets.UTF_8)
            FrameCodec.writeFrame(
                out, FrameType.AUTH_RES,
                jsonOf("fp" to identity.fingerprint, "pub" to b64(identity.spkiDer),
                    "sig" to b64(identity.sign(payloadS))),
            )
            return PeerAuth(sock, fpC, pubC, nonce)
        } catch (e: TunnelError) {
            throw e
        } catch (e: Exception) {
            throw TunnelError("隧道握手失败：${e.message ?: e.javaClass.simpleName}")
        }
    }

    /** 客户端侧：收 magic + AUTH_HELLO，发 AUTH_REQ（带签名），验服务端签名。 */
    fun handshakeClient(sock: Socket, identity: Identity): PeerAuth {
        val out = sock.getOutputStream()
        val ins = sock.getInputStream()
        try {
            val magic = FrameCodec.readExact(ins, 4) ?: throw TunnelError("隧道握手中连接被关闭")
            if (!magic.contentEquals(MAGIC)) throw TunnelError("对面不是大司农隧道（magic 不对）")
            val hello = FrameCodec.readFrame(ins) ?: throw TunnelError("隧道握手中连接被关闭")
            if (hello.type != FrameType.AUTH_HELLO) throw TunnelError("隧道握手帧不对（预期 AUTH_HELLO）")
            val nonceHex = authField(hello.body, "nonce")
            if (nonceHex.isEmpty()) throw TunnelError("隧道握手缺少 nonce")
            val nonce = nonceHex.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
            // 客户端签名覆盖：方向 + nonce + 服务端自报指纹（防中继/降级）
            val sfp = authField(hello.body, "fp")
            val payload = "client|$nonceHex|$sfp".toByteArray(Charsets.UTF_8)
            FrameCodec.writeFrame(
                out, FrameType.AUTH_REQ,
                jsonOf("fp" to identity.fingerprint, "pub" to b64(identity.spkiDer),
                    "sig" to b64(identity.sign(payload))),
            )
            val res = FrameCodec.readFrame(ins) ?: throw TunnelError("隧道握手中连接被关闭")
            if (res.type == FrameType.AUTH_ERR) {
                throw TunnelError(authField(res.body, "错").ifEmpty { "对方拒绝了本次认证" })
            }
            if (res.type != FrameType.AUTH_RES) throw TunnelError("隧道握手帧不对（预期 AUTH_RES）")
            val fpS = authField(res.body, "fp")
            val pubS = b64d(authField(res.body, "pub"))
            val sigS = b64d(authField(res.body, "sig"))
            if (fpS.isEmpty() ||
                Identity.hex(java.security.MessageDigest.getInstance("SHA-256").digest(pubS)) != fpS
            ) {
                throw TunnelError("对端公钥与声明指纹不符")
            }
            if (sfp.isNotEmpty() && sfp != fpS) {
                throw TunnelError("对端身份与它自己声明的不一致（疑似中间人）")
            }
            val payloadS = "server|$nonceHex|${identity.fingerprint}".toByteArray(Charsets.UTF_8)
            if (!Identity.verify(pubS, payloadS, sigS)) {
                throw TunnelError("对方身份签名校验未通过")
            }
            return PeerAuth(sock, fpS, pubS, nonce)
        } catch (e: TunnelError) {
            throw e
        } catch (e: Exception) {
            throw TunnelError("隧道握手失败：${e.message ?: e.javaClass.simpleName}")
        }
    }
}

/** 隧道上下文：持有本机身份与 TLS 上下文，可被多线程复用。 */
class TunnelContext(val identity: Identity) {

    /** v4 起恒为 true（引擎在没身份时不启动网络）。 */
    val enabled: Boolean = true

    /** 本机身份指纹。⚠ 不外显（用户硬要求：界面不显示本机指纹）。 */
    val fingerprint: String = identity.fingerprint

    /** 本机公钥（base64 SPKI），发现广播里带上它。 */
    val spkiB64: String = Base64.getEncoder().encodeToString(identity.spkiDer)

    private val provider = BouncyCastleProvider()
    private var serverCtx: SSLContext? = null
    private var clientCtx: SSLContext? = null

    init {
        serverCtx = buildServerContext(identity.privateKey, identity.cert, provider)
        clientCtx = buildClientContext()
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

    /**
     * 客户端上下文**不校验证书链** —— 对端指纹事先不知道，身份由应用层签名证明
     * （见 [Tunnel.handshakeClient]）。TLS 只负责机密性与完整性。
     */
    private fun buildClientContext(): SSLContext {
        val tm = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>?, authType: String?) = Unit
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
            override fun checkServerTrusted(chain: Array<X509Certificate>?, authType: String?) = Unit
        }
        val ctx = SSLContext.getInstance("TLS")
        ctx.init(null, arrayOf<X509TrustManager>(tm), SecureRandom())
        return ctx
    }

    private fun preferTls(s: SSLSocket) {
        val ok = s.supportedProtocols.filter { it == "TLSv1.3" || it == "TLSv1.2" }
        if (ok.isNotEmpty()) s.enabledProtocols = ok.toTypedArray()
    }

    /** 服务端侧：包 TLS + 双向签名握手，返回**已认证**的对端信息。 */
    fun accept(raw: Socket): PeerAuth {
        val ssl = try {
            (serverCtx!!.socketFactory.createSocket(raw, null, raw.port, true) as SSLSocket).apply {
                useClientMode = false
                needClientAuth = false
                preferTls(this)
                startHandshake()
            }
        } catch (e: Exception) {
            runCatching { raw.close() }
            throw TunnelError("TLS 握手失败：${e.message ?: e.javaClass.simpleName}")
        }
        return try {
            ssl.soTimeout = 20000
            val auth = Tunnel.handshakeServer(ssl, identity)
            ssl.soTimeout = 0
            auth
        } catch (e: Exception) {
            runCatching { ssl.close() }
            throw e
        }
    }

    /**
     * 客户端侧：包 TLS + 双向签名握手。
     *
     * **不覆盖调用方设的 socket 超时**（直连要靠它做短超时），握手完再清掉。
     */
    fun connect(raw: Socket, host: String = ""): PeerAuth {
        val ssl = try {
            (clientCtx!!.socketFactory.createSocket(raw, host, raw.port, true) as SSLSocket).apply {
                useClientMode = true
                preferTls(this)
                startHandshake()
            }
        } catch (e: Exception) {
            runCatching { raw.close() }
            throw TunnelError("TLS 握手失败：${e.message ?: e.javaClass.simpleName}")
        }
        return try {
            val auth = Tunnel.handshakeClient(ssl, identity)
            ssl.soTimeout = 0
            auth
        } catch (e: Exception) {
            runCatching { ssl.close() }
            throw e
        }
    }

    /** 用设备私钥签发现报文的规范化字段串（base64 的 DER 签名）。 */
    fun signHello(fields: String): String =
        Base64.getEncoder().encodeToString(identity.sign(fields.toByteArray(Charsets.UTF_8)))

    fun close() {
        // 证书在内存里，无临时文件需要清理
    }
}
