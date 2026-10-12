package com.dasinong.backend.net

import com.dasinong.backend.Event
import com.dasinong.backend.EventBus
import com.dasinong.backend.NodeConfig
import com.dasinong.backend.model.Peer
import com.dasinong.backend.protocol.Messages
import com.dasinong.backend.protocol.Proto
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList

/**
 * UDP 多通道发现：受限广播 + 逐网卡定向广播 + 手动/历史 IP 单播 + 网关报文转发。
 * 去重键 = deviceId；超时或 bye 即下线。
 */
class DiscoveryService(
    private val cfg: NodeConfig,
    private val binder: LanNetworkBinder,
    private val bus: EventBus,
    private val tunnelProvider: (() -> TunnelContext?)? = null,
) {
    private val peers = ConcurrentHashMap<String, Peer>()
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val manualTargets = CopyOnWriteArrayList<String>()
    private val lastReply = ConcurrentHashMap<String, Long>()

    private fun currentTunnel(): TunnelContext? = try {
        tunnelProvider?.invoke()
    } catch (_: Exception) {
        null
    }

    @Volatile private var running = false
    private var recv: DatagramSocket? = null
    @Volatile private var interval = Proto.HELLO_INTERVAL_MS

    var gatewayHandler: ((JSONObject, String) -> Unit)? = null

    private val gatewayTypes = setOf(
        "gw_hello", "gw_ack", "gw_register", "gw_peer_list", "gw_goodbye",
    )

    fun snapshot(): List<Peer> = peers.values.toList()
    fun get(deviceId: String): Peer? = peers[deviceId]

    fun setBackground(background: Boolean) {
        interval = if (background) Proto.HELLO_INTERVAL_BG_MS else Proto.HELLO_INTERVAL_MS
    }

    fun addManualTarget(ip: String) {
        val clean = ip.trim()
        if (clean.isEmpty() || manualTargets.contains(clean)) return
        manualTargets.add(clean)
        sendHello(direct = true, dst = clean)
    }

    fun start() {
        if (running) return
        running = true
        recv = binder.openUdp(null, cfg.udpPort).also { it.soTimeout = 1000 }
        scope.launch { recvLoop() }
        scope.launch { announceLoop() }
        scope.launch { pruneLoop() }
    }

    fun stop() {
        if (!running) return
        running = false
        sendBye()
        try {
            recv?.close()
        } catch (_: Exception) {
        }
        recv = null
    }

    // ------------------------------------------------------------ 发送
    private fun buildHello(direct: Boolean): ByteArray {
        val ts = System.currentTimeMillis()
        val data = JSONObject().apply {
            put("name", cfg.deviceName)
            put("device_type", cfg.deviceType)
            put("tcp_port", cfg.tcpPort)
            put("udp_port", cfg.udpPort)
            put("proto_min", Proto.MIN_VERSION)
            put("proto_max", Proto.VERSION)
            put("ts", ts)
            if (direct) put("direct", true)
        }
        val tunnel = currentTunnel()
        if (tunnel != null && tunnel.enabled) {
            data.put("enc", 1)
            data.put(
                "mac",
                tunnel.signHello(
                    Tunnel.helloFields(cfg.deviceId, cfg.deviceName, cfg.deviceType, cfg.tcpPort, cfg.udpPort, ts),
                ),
            )
        }
        return Messages.encode(Messages.envelope("hello", cfg.deviceId, null, data))
    }

    fun sendHello(direct: Boolean = false, dst: String? = null) {
        val raw = buildHello(direct)
        if (dst != null) {
            val net = binder.pickPrimary() ?: return
            try {
                binder.openUdp(net, 0).use { s ->
                    s.send(DatagramPacket(raw, raw.size, InetAddress.getByName(dst), cfg.udpPort))
                }
            } catch (_: Exception) {
            }
            return
        }
        for (net in binder.candidateLans()) {
            val targets = ArrayList<String>()
            binder.directedBroadcast(net)?.hostAddress?.let { targets.add(it) }
            targets.add("255.255.255.255")
            targets.addAll(Proto.HOTSPOT_BROADCASTS)
            try {
                binder.openUdp(net, 0).use { s ->
                    for (t in targets) {
                        try {
                            s.send(DatagramPacket(raw, raw.size, InetAddress.getByName(t), cfg.udpPort))
                        } catch (_: Exception) {
                        }
                    }
                }
            } catch (_: Exception) {
            }
        }
    }

    private fun sendBye() {
        val raw = Messages.encode(Messages.envelope("bye", cfg.deviceId, null, JSONObject()))
        for (net in binder.candidateLans()) {
            val targets = ArrayList<String>()
            binder.directedBroadcast(net)?.hostAddress?.let { targets.add(it) }
            targets.add("255.255.255.255")
            try {
                binder.openUdp(net, 0).use { s ->
                    for (t in targets) {
                        try {
                            s.send(DatagramPacket(raw, raw.size, InetAddress.getByName(t), cfg.udpPort))
                        } catch (_: Exception) {
                        }
                    }
                }
            } catch (_: Exception) {
            }
        }
    }

    // ------------------------------------------------------------ 循环
    private suspend fun announceLoop() {
        while (running && scope.isActive) {
            try {
                sendHello(false)
                for (ip in manualTargets) sendHello(true, ip)
            } catch (_: Exception) {
            }
            delay(interval)
        }
    }

    private suspend fun pruneLoop() {
        while (running && scope.isActive) {
            val now = System.currentTimeMillis()
            val removed = ArrayList<String>()
            for ((id, peer) in peers) {
                if (now - peer.lastSeen > Proto.PEER_TIMEOUT_MS) {
                    peers.remove(id)
                    removed.add(id)
                }
            }
            for (id in removed) bus.publish(Event.PeerRemoved(id))
            delay(1000)
        }
    }

    private fun recvLoop() {
        val sock = recv ?: return
        val buf = ByteArray(65535)
        while (running) {
            try {
                val pkt = DatagramPacket(buf, buf.size)
                sock.receive(pkt)
                val text = String(pkt.data, 0, pkt.length, Charsets.UTF_8)
                handle(text, pkt.address.hostAddress)
            } catch (_: java.net.SocketTimeoutException) {
                continue
            } catch (_: Exception) {
                if (!running) break
            }
        }
    }

    private fun handle(text: String, srcIp: String) {
        val env = Messages.decode(text.toByteArray(Charsets.UTF_8)) ?: return
        if (env.optString("from") == cfg.deviceId) return
        val type = env.optString("type")
        if (type in gatewayTypes) {
            gatewayHandler?.invoke(env, srcIp)
            return
        }
        when (type) {
            "hello" -> {
                onHello(env, srcIp)
                val direct = env.optJSONObject("data")?.optBoolean("direct", false) ?: false
                if (!direct) maybeReply(srcIp)
            }
            "bye" -> removePeer(env.optString("from"))
        }
    }

    private fun onHello(env: JSONObject, srcIp: String) {
        val id = env.optString("from")
        if (id.isEmpty()) return
        val data = env.optJSONObject("data") ?: JSONObject()
        val encrypted = jsonFlag(data, "enc")

        // 本机已启用加密 → 只认签名正确的 hello（防同网段伪造设备冒进列表）
        val tunnel = currentTunnel()
        if (tunnel != null && tunnel.enabled) {
            val mac = data.optString("mac")
            val expect = tunnel.signHello(
                Tunnel.helloFields(
                    id,
                    data.optString("name"),
                    data.optString("device_type", "android"),
                    data.optInt("tcp_port", Proto.TCP_PORT),
                    data.optInt("udp_port", cfg.udpPort),
                    data.optLong("ts", 0L),
                ),
            )
            if (mac.isEmpty() || mac != expect) return
        }

        val now = System.currentTimeMillis()
        val existing = peers[id]
        if (existing == null) {
            val peer = Peer(
                deviceId = id,
                name = data.optString("name", id.take(8)),
                ip = srcIp,
                tcpPort = data.optInt("tcp_port", Proto.TCP_PORT),
                udpPort = data.optInt("udp_port", cfg.udpPort),
                deviceType = data.optString("device_type", "android"),
                proto = env.optInt("proto", 1),
                encrypted = encrypted,
                lastSeen = now,
            )
            peers[id] = peer
            bus.publish(Event.PeerAdded(peer))
        } else {
            val changed = existing.ip != srcIp ||
                existing.name != data.optString("name", existing.name) ||
                existing.tcpPort != data.optInt("tcp_port", existing.tcpPort) ||
                existing.encrypted != encrypted
            existing.ip = srcIp
            existing.name = data.optString("name", existing.name)
            existing.tcpPort = data.optInt("tcp_port", existing.tcpPort)
            existing.udpPort = data.optInt("udp_port", existing.udpPort)
            existing.encrypted = encrypted
            existing.lastSeen = now
            if (changed) bus.publish(Event.PeerUpdated(existing))
        }
    }

    private fun maybeReply(srcIp: String) {
        val now = System.currentTimeMillis()
        val last = lastReply[srcIp] ?: 0L
        if (now - last < 2000) return
        lastReply[srcIp] = now
        sendHello(direct = true, dst = srcIp)
    }

    private fun removePeer(deviceId: String) {
        if (deviceId.isEmpty()) return
        if (peers.remove(deviceId) != null) bus.publish(Event.PeerRemoved(deviceId))
    }

    /** 网关中继 / 直连把对端注入发现表。 */
    fun registerPeer(peer: Peer) {
        // 注入的 peer 可能来自「记忆里的地址」这种陈旧对象，lastSeen 很老；
        // 不刷新的话下一轮 prune 立刻又把它剪掉 → 附近列表每秒闪一次。
        peer.lastSeen = System.currentTimeMillis()
        val existing = peers[peer.deviceId]
        if (existing == null) {
            peers[peer.deviceId] = peer
            bus.publish(Event.PeerAdded(peer))
        } else {
            val changed = existing.ip != peer.ip ||
                existing.name != peer.name ||
                existing.tcpPort != peer.tcpPort ||
                existing.encrypted != peer.encrypted
            existing.ip = peer.ip
            existing.name = peer.name
            existing.tcpPort = peer.tcpPort
            existing.encrypted = peer.encrypted
            existing.lastSeen = System.currentTimeMillis()
            if (changed) bus.publish(Event.PeerUpdated(existing))
        }
    }

    /**
     * 刷新 lastSeen（不碰其它字段、不发事件）。
     *
     * 有活着的控制连接就说明对方还在，必须持续刷新，否则 UDP 通不了的对端
     * （典型：手机开热点时发不出广播）会每 10 秒被剪一次、再被补回来 → 列表闪。
     */
    fun touch(deviceId: String) {
        peers[deviceId]?.lastSeen = System.currentTimeMillis()
    }
}

/**
 * JSON 里 `1` / `true` / `"true"` 都当作「真」。
 *
 * ⚠ 不能用 `optBoolean`：org.json 的 `optBoolean` 只认 Boolean 和字符串，
 * 遇到数字 `1` 会返回默认值 —— 而我们的 hello 里 `enc` 就是数字 1，
 * 于是「已加密」永远被读成 false（界面一直显示「未加密」）。
 */
internal fun jsonFlag(o: JSONObject, key: String): Boolean = when (val v = o.opt(key)) {
    null -> false
    is Boolean -> v
    is Number -> v.toInt() != 0
    is String -> v == "1" || v.equals("true", ignoreCase = true)
    else -> false
}
