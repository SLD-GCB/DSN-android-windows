package com.dasinong.backend.net

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
import org.json.JSONArray
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.util.concurrent.ConcurrentHashMap

/**
 * 网关中继：无互联网热点 / AP 隔离场景下，客户端向网关(热点主机)单播注册，
 * 主机回推设备表，从而在客户端↔客户端广播被拦时仍能互相发现。
 */
class GatewayRendezvous(
    private val cfg: NodeConfig,
    private val binder: LanNetworkBinder,
    private val bus: EventBus,
    private val discovery: DiscoveryService,
    private val tunnelProvider: (() -> TunnelContext?)? = null,
) {
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val gateways = (Proto.CANDIDATE_GATEWAYS).toMutableList()
    private val clients = ConcurrentHashMap<String, JSONObject>()   // ip -> device_info
    private val lastRegister = ConcurrentHashMap<String, Long>()
    @Volatile private var running = false

    /** 本机是否启用了加密隧道（写进 device_info，让别的设备也能显示「已加密」）。 */
    private fun localEncrypted(): Boolean = try {
        tunnelProvider?.invoke()?.enabled == true
    } catch (_: Exception) {
        false
    }

    fun addCandidate(ip: String) {
        if (ip.isNotBlank() && !gateways.contains(ip)) gateways.add(ip)
    }

    fun start() {
        if (running) return
        running = true
        discovery.gatewayHandler = { env, ip -> handle(env, ip) }
        scope.launch {
            while (running && scope.isActive) {
                for (ip in gateways.toList()) {
                    try {
                        send("gw_hello", JSONObject(), ip)
                    } catch (_: Exception) {
                    }
                }
                delay(Proto.GATEWAY_PROBE_INTERVAL_MS)
            }
        }
    }

    fun stop() {
        running = false
        for (ip in clients.keys.toList()) {
            try {
                send("gw_goodbye", JSONObject().put("device_id", cfg.deviceId), ip)
            } catch (_: Exception) {
            }
        }
    }

    // ------------------------------------------------------------ 发送
    private fun sourceIpFor(dstIp: String): String? {
        for (net in binder.candidateLans()) {
            val ip = binder.sourceIp(net) ?: continue
            val bc = binder.directedBroadcast(net) ?: continue
            // 同网段判断：目标与广播同前缀
            val ipStr = ip.hostAddress
            val prefixLen = ipStr.split('.').size
            if (prefixLen == 4) {
                val a = ipStr.substringBeforeLast('.')
                val b = dstIp.substringBeforeLast('.')
                if (a == b) return ipStr
                if (bc.hostAddress.startsWith(a + ".")) return ipStr
            }
        }
        return binder.pickPrimary()?.let { binder.sourceIp(it)?.hostAddress }
    }

    private fun send(type: String, data: JSONObject, dstIp: String) {
        val raw = Messages.encode(Messages.envelope(type, cfg.deviceId, null, data))
        val net = binder.pickPrimary()
        try {
            binder.openUdp(net, 0).use { s ->
                s.send(DatagramPacket(raw, raw.size, InetAddress.getByName(dstIp), cfg.udpPort))
            }
        } catch (_: Exception) {
        }
    }

    // ------------------------------------------------------------ 设备信息
    private fun myIp(): String =
        binder.pickPrimary()?.let { binder.sourceIp(it)?.hostAddress } ?: "0.0.0.0"

    private fun deviceInfo(): JSONObject = JSONObject().apply {
        put("device_id", cfg.deviceId)
        put("name", cfg.deviceName)
        put("device_type", cfg.deviceType)
        put("ip", myIp())
        put("tcp_port", cfg.tcpPort)
        put("udp_port", cfg.udpPort)
        put("proto", Proto.VERSION)
        put("enc", if (localEncrypted()) 1 else 0)
        put("ts", System.currentTimeMillis())
    }

    private fun peerInfo(p: Peer): JSONObject = JSONObject().apply {
        put("device_id", p.deviceId)
        put("name", p.name)
        put("device_type", p.deviceType)
        put("ip", p.ip)
        put("tcp_port", p.tcpPort)
        put("udp_port", p.udpPort)
        put("proto", p.proto)
        put("enc", if (p.encrypted) 1 else 0)
        put("ts", System.currentTimeMillis())
    }

    private fun peerList(): JSONArray {
        val seen = HashSet<String>()
        seen.add(cfg.deviceId)
        val arr = JSONArray()
        for (p in discovery.snapshot()) {
            if (seen.add(p.deviceId)) arr.put(peerInfo(p))
        }
        for (info in clients.values) {
            val id = info.optString("device_id")
            if (id.isNotEmpty() && seen.add(id)) arr.put(info)
        }
        return arr
    }

    // ------------------------------------------------------------ 处理
    fun handle(env: JSONObject, srcIp: String) {
        val data = env.optJSONObject("data") ?: JSONObject()
        when (env.optString("type")) {
            "gw_hello" -> send("gw_ack", JSONObject().put("host", deviceInfo()), srcIp)
            "gw_ack" -> {
                val now = System.currentTimeMillis()
                if (now - (lastRegister[srcIp] ?: 0L) >= 5000) {
                    lastRegister[srcIp] = now
                    send("gw_register", JSONObject().put("device", deviceInfo()), srcIp)
                }
            }
            "gw_register" -> {
                val info = data.optJSONObject("device") ?: return
                val id = info.optString("device_id")
                if (id.isEmpty() || id == cfg.deviceId) return
                if (info.isNull("ip")) info.put("ip", srcIp)
                clients[srcIp] = info
                inject(info)
                send("gw_peer_list", JSONObject().put("peers", peerList()), srcIp)
                pushPeerList(exclude = srcIp)
            }
            "gw_peer_list" -> {
                val peers = data.optJSONArray("peers") ?: return
                for (i in 0 until peers.length()) {
                    peers.optJSONObject(i)?.let { inject(it) }
                }
            }
            "gw_goodbye" -> {
                clients.remove(srcIp)
                pushPeerList()
            }
        }
    }

    private fun pushPeerList(exclude: String? = null) {
        val targets = clients.keys.filter { it != exclude }
        if (targets.isEmpty()) return
        val payload = JSONObject().put("peers", peerList())
        for (ip in targets) send("gw_peer_list", payload, ip)
    }

    private fun inject(info: JSONObject) {
        val id = info.optString("device_id")
        val ip = info.optString("ip")
        if (id.isEmpty() || ip.isEmpty() || id == cfg.deviceId) return
        discovery.registerPeer(
            Peer(
                deviceId = id,
                name = info.optString("name", id.take(8)),
                ip = ip,
                tcpPort = info.optInt("tcp_port", Proto.TCP_PORT),
                udpPort = info.optInt("udp_port", cfg.udpPort),
                deviceType = info.optString("device_type", "android"),
                proto = info.optInt("proto", 1),
                encrypted = jsonFlag(info, "enc"),
            )
        )
    }

    fun prune() {
        val now = System.currentTimeMillis()
        var removed = false
        for ((ip, info) in clients) {
            val ts = info.optLong("ts", 0L)
            if (ts > 0 && now - ts > Proto.PEER_TIMEOUT_MS) {
                clients.remove(ip)
                removed = true
            }
        }
        if (removed) pushPeerList()
    }
}
