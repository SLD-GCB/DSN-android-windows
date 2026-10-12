package com.dasinong.backend.net

import com.dasinong.backend.NodeConfig
import com.dasinong.backend.protocol.FrameCodec
import com.dasinong.backend.protocol.FrameType
import com.dasinong.backend.protocol.Messages
import com.dasinong.backend.protocol.Proto
import org.json.JSONObject
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.ConcurrentHashMap

/** 一条控制连接（长连接，只跑 JSON 帧）。 */
class ControlConnection(
    private val cfg: NodeConfig,
    val socket: Socket,
    val peerId: String,
    val isOutgoing: Boolean,
    private val onMessage: (ControlConnection, JSONObject) -> Unit,
    private val onClose: (ControlConnection) -> Unit,
) {
    private val out = socket.getOutputStream()
    private val input = socket.getInputStream()
    private val sendLock = Any()

    @Volatile var lastSeen: Long = System.currentTimeMillis()
    @Volatile var lastPong: Long = System.currentTimeMillis()
    @Volatile private var closed = false

    val alive: Boolean get() = !closed

    /** 这条连接对端的**当前** IP（数据连接就该往这里发，而不是查发现表/记忆地址）。 */
    fun remoteIp(): String? = try {
        if (closed) null else socket.inetAddress?.hostAddress
    } catch (_: Exception) {
        null
    }

    fun start() {
        Thread({ readLoop() }, "control-${peerId.take(8)}").apply { isDaemon = true }.start()
    }

    fun send(env: JSONObject): Boolean = try {
        val payload = Messages.encode(env)
        synchronized(sendLock) { FrameCodec.writeFrame(out, FrameType.JSON, payload) }
        true
    } catch (e: Exception) {
        close(); false
    }

    fun close() {
        if (closed) return
        closed = true
        try {
            socket.close()
        } catch (_: Exception) {
        }
        try {
            onClose(this)
        } catch (_: Exception) {
        }
    }

    private fun readLoop() {
        try {
            while (!closed) {
                val frame = FrameCodec.readFrame(input) ?: break
                if (frame.type != FrameType.JSON) continue
                val env = Messages.decode(frame.body) ?: continue
                lastSeen = System.currentTimeMillis()
                try {
                    onMessage(this, env)
                } catch (_: Exception) {
                }
            }
        } catch (_: Exception) {
        } finally {
            close()
        }
    }
}

/**
 * TCP 服务端 + 控制连接管理 + 数据连接分发。
 * 只保留「发起方 == 字典序较小 deviceId」的一条控制连接。
 */
class TransportManager(
    private val cfg: NodeConfig,
    private val binder: LanNetworkBinder,
    private val onControl: (ControlConnection, JSONObject) -> Unit,
    private val onData: (Socket, Int, ByteArray, String) -> Unit,
    private val tunnelProvider: (() -> TunnelContext?)? = null,
    private val log: (String) -> Unit = {},
    /** 直连时被叫方学到拨号方身份的钩子：(identityJson, 来源IP) */
    private val onLearnPeer: ((JSONObject, String) -> Unit)? = null,
) {
    private val sessions = ConcurrentHashMap<String, ControlConnection>()
    @Volatile private var server: ServerSocket? = null
    @Volatile private var running = false

    /** 自我介绍（直连握手用）。 */
    private fun identity(): JSONObject = JSONObject()
        .put("device_id", cfg.deviceId)
        .put("name", cfg.deviceName)
        .put("device_type", cfg.deviceType)
        .put("tcp_port", cfg.tcpPort)
        .put("udp_port", cfg.udpPort)
        .put("proto", Proto.VERSION)

    /** 直连结果：会话 / 对方身份 / 错误文案。 */
    class DirectResult(val conn: ControlConnection?, val identity: JSONObject?, val error: String?)

    private fun currentTunnel(): TunnelContext? = try {
        tunnelProvider?.invoke()
    } catch (_: Exception) {
        null
    }

    /**
     * 服务端侧：口令非空时先包 TLS + 跑隧道握手，之后才是业务首帧。
     * 返回 null 表示隧道阶段就失败了（socket 已关）。
     */
    private fun wrapIncoming(raw: Socket): Socket? {
        val tunnel = currentTunnel() ?: return raw
        if (!tunnel.enabled) return raw
        return try {
            tunnel.accept(raw)
        } catch (e: Exception) {
            log("隧道握手失败（${raw.inetAddress?.hostAddress}）：${e.message}")
            runCatching { raw.close() }
            null
        }
    }

    fun start() {
        if (running) return
        running = true
        val srv = ServerSocket()
        srv.reuseAddress = true
        srv.bind(InetSocketAddress(cfg.tcpPort))
        server = srv
        Thread({ acceptLoop(srv) }, "tcp-accept").apply { isDaemon = true }.start()
    }

    fun stop() {
        running = false
        try {
            server?.close()
        } catch (_: Exception) {
        }
        val list = sessions.values.toList()
        sessions.clear()
        list.forEach { it.close() }
    }

    private fun acceptLoop(srv: ServerSocket) {
        while (running) {
            val sock = try {
                srv.accept()
            } catch (e: Exception) {
                if (running) continue else break
            }
            try {
                sock.tcpNoDelay = true
            } catch (_: Exception) {
            }
            Thread({ handleIncoming(sock) }, "tcp-in").apply { isDaemon = true }.start()
        }
    }

    private fun handleIncoming(raw: Socket) {
        val sock = wrapIncoming(raw) ?: return
        try {
            val frame = FrameCodec.readFrame(sock.getInputStream())
            if (frame == null) {
                sock.close(); return
            }
            when (frame.type) {
                FrameType.JSON -> {
                    val env = Messages.decode(frame.body)
                    if (env == null || env.optString("type") != "connect") {
                        sock.close(); return
                    }
                    val peerId = env.optString("from")
                    if (peerId.isEmpty() || peerId == cfg.deviceId) {
                        sock.close(); return
                    }
                    val conn = ControlConnection(cfg, sock, peerId, false, onControl, ::onSessionClose)
                    register(conn)
                    conn.start()
                    // **任何**入站连接都要把拨号方登记下来（不只是「只知 IP 直连」那条路）：
                    // 拨号方在 connect 里带了自己的身份，这里连 IP 一起记进发现表/端点缓存，
                    // 否则被叫方虽然能收，却不知道对方在哪 —— 数据连接就发不回去。
                    // 热点场景下对方发不出广播，这是被叫方唯一的信息来源。
                    runCatching {
                        val info = env.optJSONObject("data") ?: JSONObject()
                        if (info.optString("device_id").isEmpty()) info.put("device_id", peerId)
                        val fromIp = sock.inetAddress?.hostAddress
                        if (fromIp != null) onLearnPeer?.invoke(info, fromIp)
                    }
                    // 「只知 IP 的直连」再多回一条身份应答（拨号方靠它知道我们是谁）。
                    if (env.optJSONObject("data")?.optBoolean("ip_only") == true) {
                        conn.send(Messages.envelope("connect_ok", cfg.deviceId, peerId, identity()))
                    }
                }
                FrameType.DATA_HELLO -> onData(sock, frame.type, frame.body, sock.inetAddress.hostAddress ?: "")
                else -> sock.close()
            }
        } catch (_: Exception) {
            try {
                sock.close()
            } catch (_: Exception) {
            }
        }
    }

    fun connectControl(ip: String, peerId: String, port: Int = cfg.tcpPort): ControlConnection? = try {
        var sock = binder.connectTcpAny(ip, port, Proto.CONNECT_TIMEOUT_MS)
        // 客户端侧：口令非空时包 TLS + 钉指纹 + 跑握手
        val tunnel = currentTunnel()
        if (tunnel != null && tunnel.enabled) {
            try {
                sock = tunnel.connect(sock, ip)
            } catch (e: Exception) {
                runCatching { sock.close() }
                log("与 ${peerId.take(8)} 建立加密隧道失败：${e.message}")
                return null
            }
        }
        val conn = ControlConnection(cfg, sock, peerId, true, onControl, ::onSessionClose)
        // 握手：**总是**带上自己的身份（deviceId/名字/端口）。老版本读不懂就忽略多余字段，
        // 但这样任何一次入站连接，被叫方都能立刻知道拨号方"现在"的地址和端口 ——
        // 数据连接才有地方可发（热点下 UDP 发现是废的）。
        val data = JSONObject().put("role", "control")
        val me = identity()
        for (key in me.keys()) data.put(key, me.get(key))
        val hello = Messages.envelope("connect", cfg.deviceId, peerId, data)
        if (!conn.send(hello)) {
            null
        } else {
            register(conn)
            conn.start()
            conn
        }
    } catch (e: Exception) {
        null
    }

    /**
     * 只知 IP 的**直连**：TCP（+隧道）→ 发 `connect{ip_only}` → 读 `connect_ok` → 建会话。
     *
     * 为什么先读应答再建会话：此时才知道对方 deviceId，而会话是按 deviceId 分键的；
     * 同步读到应答就省掉了「建完再改名」的竞态。
     */
    fun connectDirect(ip: String, port: Int = cfg.tcpPort, timeoutMs: Int = 6000): DirectResult {
        var sock: Socket? = null
        try {
            var s = binder.connectTcpAny(ip, port, Proto.CONNECT_TIMEOUT_MS)
            val tunnel = currentTunnel()
            if (tunnel != null && tunnel.enabled) {
                try {
                    s = tunnel.connect(s, ip)
                } catch (e: Exception) {
                    runCatching { s.close() }
                    // 口令不一致时这里就是那句「对方身份对不上」，必须上抛给 UI
                    return DirectResult(null, null, e.message ?: "口令不一致或对方未开加密")
                }
            }
            sock = s
            val data = JSONObject().put("role", "control").put("ip_only", true)
            val me = identity()
            for (key in me.keys()) data.put(key, me.get(key))
            FrameCodec.writeFrame(
                s.getOutputStream(), FrameType.JSON,
                Messages.encode(Messages.envelope("connect", cfg.deviceId, null, data)),
            )
            s.soTimeout = timeoutMs            // 只在读应答这一小段用超时
            val frame = FrameCodec.readFrame(s.getInputStream())
            s.soTimeout = 0                    // 之后恢复阻塞，交给读循环
            if (frame == null || frame.type != FrameType.JSON) {
                runCatching { s.close() }
                return DirectResult(null, null, "对方没有回应直连（可能是旧版本，请改用扫描发现）")
            }
            val env = Messages.decode(frame.body)
            if (env == null || env.optString("type") != "connect_ok") {
                runCatching { s.close() }
                return DirectResult(null, null, "对方不支持直连（版本较旧）")
            }
            val info = env.optJSONObject("data") ?: JSONObject()
            val peerId = info.optString("device_id")
            if (peerId.isEmpty() || peerId == cfg.deviceId) {
                runCatching { s.close() }
                return DirectResult(null, null, "对方返回的身份无效")
            }
            val conn = ControlConnection(cfg, s, peerId, true, onControl, ::onSessionClose)
            register(conn)
            conn.start()
            return DirectResult(conn, info, null)
        } catch (e: java.net.SocketTimeoutException) {
            runCatching { sock?.close() }
            return DirectResult(null, null, "对方没有回应直连（可能是旧版本，请改用扫描发现）")
        } catch (e: Exception) {
            runCatching { sock?.close() }
            return DirectResult(null, null, "连不上 $ip:$port（${e.message}）")
        }
    }

    private fun register(conn: ControlConnection) {
        // 每个 peer 只保留一条控制连接：两端同时互连时保留 canon 方向（较小 deviceId 发起），
        // 但对端从未连过来时允许非 canon 方向先建立（应对单向防火墙/路由）。
        val canonOutgoing = cfg.deviceId < conn.peerId
        synchronized(sessions) {
            val existing = sessions[conn.peerId]
            if (existing != null && existing.alive) {
                if (conn.isOutgoing == canonOutgoing && existing.isOutgoing != canonOutgoing) {
                    existing.close()
                } else {
                    conn.close(); return
                }
            }
            sessions[conn.peerId] = conn
        }
    }

    private fun onSessionClose(conn: ControlConnection) {
        synchronized(sessions) {
            if (sessions[conn.peerId] === conn) sessions.remove(conn.peerId)
        }
    }

    fun getSession(peerId: String): ControlConnection? = sessions[peerId]?.takeIf { it.alive }

    /** TCP 服务端是否在监听（诊断用）。 */
    val isListening: Boolean get() = server != null && running

    fun sessions(): List<ControlConnection> = sessions.values.filter { it.alive }

    fun sendTo(peerId: String, env: JSONObject): Boolean = getSession(peerId)?.send(env) ?: false
}
