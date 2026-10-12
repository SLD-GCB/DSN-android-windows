package com.dasinong.backend

import android.content.Context
import android.net.ConnectivityManager
import com.dasinong.backend.model.ChatMessage
import com.dasinong.backend.model.FileEntry
import com.dasinong.backend.model.MsgKind
import com.dasinong.backend.model.Peer
import com.dasinong.backend.model.Transfer
import com.dasinong.backend.model.TransferState
import com.dasinong.backend.net.ControlConnection
import com.dasinong.backend.net.DiscoveryService
import com.dasinong.backend.net.GatewayRendezvous
import com.dasinong.backend.net.LanNetworkBinder
import com.dasinong.backend.net.LocalFile
import com.dasinong.backend.net.TransferEngine
import com.dasinong.backend.net.TransportManager
import com.dasinong.backend.net.TunnelContext
import com.dasinong.backend.protocol.Messages
import com.dasinong.backend.protocol.Proto
import com.dasinong.backend.security.AndroidKeyStoreSecretStore
import com.dasinong.backend.security.Identity
import com.dasinong.backend.security.SecretStore
import com.dasinong.backend.store.ChatDb
import com.dasinong.backend.util.Paths
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.net.Socket
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean

/** 记住对端地址的时长：发现表（UDP）把对端剪掉后，靠它继续发文件/重连。 */
private const val ENDPOINT_TTL_MS = 60_000L

/**
 * NodeEngine：编排发现 / 网关中继 / 控制连接 / 传输，对外暴露高层 API。
 * UI 只与本类交互，事件通过 [bus] 发布。
 */
class NodeEngine(context: Context) {

    val cfg: NodeConfig = NodeConfig.load(context)
    val bus = EventBus()

    private val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
    private val binder = LanNetworkBinder(cm)

    /** 本地库（建表一律「有就不建、没有就建」= `CREATE TABLE IF NOT EXISTS`）。 */
    val store = ChatDb(context)

    /** 敏感数据的静态加密存储（系统密钥库；密钥不出库）。 */
    private val secret: SecretStore = AndroidKeyStoreSecretStore()

    /** 启动期异常（供 UI 展示 / 诊断）。要放在身份构造之前 —— 构造失败时要往里记。 */
    private val startErrors = java.util.Collections.synchronizedList(ArrayList<String>())

    /**
     * 本机**长期身份** + 隧道上下文（v4：每设备随机，不再是口令派生）。
     *
     * ⚠ 构造失败**绝不能让 App 崩**（密钥库/磁盘在个别机型上会出意外）：记进 `startErrors`
     * 并把两者置空 → 后续所有连接 fail-closed，但界面照常打开、把原因显示给用户。
     */
    private val identityAndTunnel: Pair<Identity?, TunnelContext?> = runCatching {
        val hadIdentity = java.io.File(cfg.dataDir, Identity.KEY_FILE).isFile
        val (ident, reused) = Identity.loadOrCreate(cfg.dataDir, secret)
        if (hadIdentity && !reused) {
            bus.publish(Event.LogEvent("本机身份已重新生成（原身份丢失），对端需要重新配对"))
        }
        ident to TunnelContext(ident)
    }.getOrElse { e ->
        val msg = "本机身份/加密初始化失败：${e.javaClass.simpleName}: ${e.message}"
        startErrors.add(msg)
        bus.publish(Event.LogEvent(msg))
        null to null
    }

    val identity: Identity? = identityAndTunnel.first

    /** 隧道上下文（v4 起恒为加密）；构造失败时为 null。 */
    @Volatile
    private var tunnelCtx: TunnelContext? = identityAndTunnel.second

    val discovery = DiscoveryService(cfg, binder, bus, tunnelProvider = { tunnelCtx })
    val gateway = GatewayRendezvous(cfg, binder, bus, discovery, tunnelProvider = { tunnelCtx })
    val transport = TransportManager(
        cfg, binder, ::onControl, ::onData,
        tunnelProvider = { tunnelCtx },
        log = { bus.publish(Event.LogEvent(it)) },
        onLearnPeer = { info, ip -> learnIncomingPeer(info, ip) },
    )
    val transfer = TransferEngine(
        cfg, binder, bus, transport, { resolvePeer(it) },
        tunnelProvider = { tunnelCtx },
        needSession = { kickSession(it) },
    )

    /** 有人要用控制连接了（比如发文件）→ 立刻重连，别等下一次退避。 */
    private fun kickSession(peerId: String) {
        backoffUntil.remove(peerId)
        attempts.remove(peerId)
        noSessionSince[peerId] = 0L      // 立刻允许兜底反连
        // **按需补连**：发送方（发消息/发文件）要用连接但没有 → 立刻拨一次。
        // 这是"用户主动发送"触发的，不是后台循环，所以不会撞接口。
        if (transport.getSession(peerId) == null) {
            val peer = discovery.get(peerId)
            if (peer != null) {
                runCatching { transport.connectControl(peer.ip, peerId, peer.tcpPort) }
            }
        }
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    /** 配对：首次连接要人工确认（两端显示同一个 6 位短码），之后长期信任。 */
    private val pairing = PairingManager(scope)
    private val seen = ConcurrentHashMap<String, Boolean>()
    private val backoffUntil = ConcurrentHashMap<String, Long>()
    private val attempts = ConcurrentHashMap<String, Int>()
    private val lastPing = ConcurrentHashMap<String, Long>()
    private val noSessionSince = ConcurrentHashMap<String, Long>()

    // deviceId -> (最近见到的地址, 时间)：发现表（UDP）把对端剪掉后靠它兜底
    private val endpoints = ConcurrentHashMap<String, Pair<Peer, Long>>()

    // 记住「连上过」的对端地址（存 setting 表）：手机开热点时 UDP 发现不到，
    // 下次启动可以直接拨回去，不必每次手输 IP。
    private val knownPeers: MutableMap<String, JSONObject> = loadKnownPeers()
    private val stopping = AtomicBoolean(false)
    private val outbox = java.util.concurrent.CopyOnWriteArrayList<PendingText>()

    private companion object {
        /** 非 canon 一方等待多久才兜底主动反连。 */
        const val FALLBACK_DELAY_MS = 8_000L

        /** 手动 IP 会话的临时 id 前缀（设备被发现后会被替换成真实 deviceId）。 */
        const val MANUAL_PREFIX = "ip:"

        /** 「保持长连接」：心跳更密、容忍更久无响应（空闲也不断会话）。 */
        const val KEEPALIVE_PING_MS = 2_000L
        const val KEEPALIVE_TIMEOUT_MS = 60_000L

    }

    private val transferTypes = setOf(
        "offer", "accept", "reject", "resume_ack", "window_update",
        "chunk_ack", "progress", "complete", "done", "cancel", "error",
    )

    // ------------------------------------------------------------ 查询
    fun peers(): List<Peer> = discovery.snapshot()
    fun isConnected(peerId: String): Boolean = transport.getSession(peerId) != null
    fun history(peerId: String): List<ChatMessage> = store.messagesFor(peerId)

    /** 清空某会话的聊天记录（会话条目保留）。返回删掉的条数。 */
    fun clearMessages(convId: String): Int {
        val n = runCatching { store.clearMessages(convId) }.getOrDefault(0)
        bus.publish(Event.LogEvent("已清空聊天记录（$n 条）"))
        return n
    }

    /** 删掉一条消息（只删本机记录）。 */
    fun deleteMessage(msgId: String): Boolean =
        runCatching { store.deleteMessage(msgId) > 0 }.getOrDefault(false)
    fun recentConversations(): List<ChatDb.ConvSummary> = store.conversations()
    fun transferRows(): List<ChatDb.TransferRow> = store.transfers()

    /**
     * 从数据库读出历史传输记录，供 UI 启动时回填列表。
     *
     * 否则关掉应用再打开，传输页只剩「本次会话」的记录（历史记录看不到）。
     */
    fun persistedTransfers(): List<Transfer> {
        val rows = runCatching { store.transfers(limit = 100000) }.getOrDefault(emptyList())
        val out = ArrayList<Transfer>(rows.size)
        for (r in rows) {
            val state = runCatching { TransferState.valueOf(r.state.uppercase()) }
                .getOrDefault(TransferState.FAILED)
            val dir = r.direction
            val done = r.received
            out.add(
                Transfer(
                    transferId = r.groupId.ifEmpty { r.fileId },
                    peerId = r.peerId,
                    file = FileEntry(fileId = r.fileId, name = r.name, relPath = "").apply { size = r.size },
                    direction = dir,
                    state = state,
                    sent = if (dir == "send") done else 0L,
                    received = if (dir == "recv") done else 0L,
                    finalPath = r.finalPath,
                    error = r.error,
                )
            )
        }
        return out
    }

    /** 被删除（隐藏）的会话 id：UI 据此过滤掉仍在线的对端。 */
    fun hiddenConversationIds(): Set<String> = store.hiddenConversationIds()

    /** 新建 / 打开会话时登记，使其在列表里持久存在（对端离线也不消失）。 */
    fun touchConversation(peerId: String, title: String? = null) {
        runCatching { store.ensureConversation(peerId, title ?: peerName(peerId)) }
    }

    /** 删除会话：清空聊天记录，并让该对端不再自动出现在列表里。 */
    fun deleteConversation(peerId: String) {
        runCatching { store.deleteConversation(peerId) }
    }

    /** 是否是「手动 IP」合成会话（还没发现设备）。 */
    fun isManualConv(convId: String): Boolean = convId.startsWith(MANUAL_PREFIX)

    /**
     * 按 IP 新建会话：
     * - 该 IP 的设备若已发现 → 直接用真实 deviceId 建会话；
     * - 否则先建一个 `ip:<addr>` 的合成会话（立刻可见），同时主动单播探测；
     *   设备被发现后会自动升级成真实 deviceId（消息一并迁移）。
     */
    fun createConversationByIp(ip: String): String {
        val clean = ip.trim()
        if (clean.isEmpty()) return ""
        discovery.addManualTarget(clean)
        gateway.addCandidate(clean)
        val found = discovery.snapshot().firstOrNull { it.ip == clean }
        if (found != null) {
            runCatching { store.ensureConversation(found.deviceId, found.name) }
            return found.deviceId
        }
        val convId = MANUAL_PREFIX + clean
        runCatching { store.ensureConversation(convId, clean) }
        return convId
    }

    private fun peerName(peerId: String): String? = discovery.get(peerId)?.name

    /**
     * 解析对端地址。发现表里没有 ≠ 对方不在线：UDP 广播丢一个 hello 就会被剪掉，
     * 此时只要控制连接还在，就用最近记下的地址，别让发文件白白报「对端不可达」。
     */
    private fun resolvePeer(peerId: String): Peer? {
        val peer = discovery.get(peerId)
        if (peer != null) {
            endpoints[peerId] = peer to System.currentTimeMillis()
            return peer
        }
        val cached = endpoints[peerId]
        val sess = transport.getSession(peerId)
        if (cached != null && sess != null) return cached.first
        // 发现表/缓存都没有，但控制连接还在：数据连接就用这条连接的**远端地址** ——
        // 手机开热点时发不出广播，这是唯一能拿到的地址。
        val liveIp = sess?.remoteIp()
        if (liveIp != null) {
            val live = Peer(
                deviceId = peerId,
                name = cached?.first?.name ?: peerId.take(8),
                ip = liveIp,
                tcpPort = cached?.first?.tcpPort ?: cfg.tcpPort,
                udpPort = cfg.udpPort,
            )
            endpoints[peerId] = live to System.currentTimeMillis()
            return live
        }
        return null
    }

    // ------------------------------------------------------------ 入站登记 / 直连
    /**
     * 把「主动连进来」的对端登记进发现表 + 端点缓存。
     *
     * 拨号方在 `connect` 里带了自己的身份（deviceId/名字/端口），这里连来源 IP 一起记下来。
     * 不记的话被叫方虽然**能收**，却不知道对方在哪、**发不回去** ——
     * 热点下对方发不出广播，这是唯一的信息来源。
     */
    /**
     * 接口池：对端在身份里通告的 **给本机的专属口**（`tcp_ports[0]`）优先；
     * 没有（老版本 v4 / 池满）则回落主口 `tcp_port`。
     */
    private fun dialPortOf(info: JSONObject): Int {
        val arr = info.optJSONArray("tcp_ports")
        if (arr != null && arr.length() > 0) {
            val p = arr.optInt(0, 0)
            if (p > 0) return p
        }
        return info.optInt("tcp_port", cfg.tcpPort)   // 回落主口（老版本 v4 / 池满）
    }

    private fun learnIncomingPeer(info: JSONObject, ip: String) {
        val did = info.optString("device_id")
        if (did.isEmpty() || did == cfg.deviceId || ip.isEmpty()) return
        val peer = Peer(
            deviceId = did,
            name = info.optString("name").ifEmpty { did.take(8) },
            ip = ip,
            tcpPort = dialPortOf(info),
            udpPort = info.optInt("udp_port", cfg.udpPort),
            deviceType = info.optString("device_type").ifEmpty { "unknown" },
            proto = info.optInt("proto", 1),
        )
        endpoints[did] = peer to System.currentTimeMillis()
        rememberPeer(peer)
        runCatching { discovery.registerPeer(peer) }   // 发 PeerAdded → ip: 会话自动升级
    }

    /** 这个 IP 上「我们已知」的设备（发现表 / 最近见到 / 记忆里）。 */
    private fun peersAtIp(ip: String): List<Peer> {
        val out = ArrayList<Peer>()
        val seen = HashSet<String>()
        for (p in discovery.snapshot()) {
            if (p.ip == ip && seen.add(p.deviceId)) out.add(p)
        }
        for ((id, cached) in endpoints) {
            if (cached.first.ip == ip && seen.add(id)) out.add(cached.first)
        }
        for ((id, info) in knownPeers) {
            if (info.optString("ip") != ip || !seen.add(id)) continue
            out.add(
                Peer(
                    deviceId = id,
                    name = info.optString("name").ifEmpty { id.take(8) },
                    ip = ip,
                    tcpPort = dialPortOf(info),
                    udpPort = cfg.udpPort,
                    deviceType = info.optString("device_type").ifEmpty { "unknown" },
                ),
            )
        }
        return out
    }

    /**
     * 只知 IP 的**直连**：不依赖 UDP 发现（手机开热点时广播发不出去）。
     *
     * 已知 deviceId 的对端走普通按 id 连接（不需要身份应答）；只有**完全陌生**的 IP
     * 才用 `ip_only` 直连去问对方是谁 —— 避免和后台维护循环同时拨号产生两条连接
     * （多出来的那条会被「保留 canon 方向」的规则关掉，反而让本次调用看起来失败）。
     */
    fun connectByIp(ip: String, port: Int? = null, timeoutMs: Int = 6000): Pair<Boolean, String> {
        val clean = ip.trim()
        if (clean.isEmpty()) return false to "请填写对方的 IP"
        val known = peersAtIp(clean)
        for (peer in known) {
            if (transport.getSession(peer.deviceId) != null) {
                endpoints[peer.deviceId] = peer to System.currentTimeMillis()
                rememberPeer(peer)
                return true to "已连接到 ${peer.name}"
            }
        }
        for (peer in known) {
            transport.connectControl(clean, peer.deviceId, port ?: peer.tcpPort)
            if (transport.getSession(peer.deviceId) != null) {
                endpoints[peer.deviceId] = peer to System.currentTimeMillis()
                rememberPeer(peer)
                if (discovery.get(peer.deviceId) == null) {
                    runCatching { discovery.registerPeer(peer) }
                }
                // 配对**手动**：连上 ≠ 配对。
                return true to "已连接 ${peer.name}"
            }
            return false to "连不上 ${peer.name}（$clean）"
        }
        val result = transport.connectDirect(clean, port ?: cfg.tcpPort, timeoutMs)
        val conn = result.conn ?: return false to (result.error ?: "直连失败")
        val info = result.identity ?: JSONObject()
        val peer = Peer(
            deviceId = conn.peerId,
            name = info.optString("name").ifEmpty { conn.peerId.take(8) },
            ip = clean,
            tcpPort = dialPortOf(info),
            udpPort = info.optInt("udp_port", cfg.udpPort),
            deviceType = info.optString("device_type").ifEmpty { "unknown" },
            proto = info.optInt("proto", 1),
        )
        endpoints[peer.deviceId] = peer to System.currentTimeMillis()
        rememberPeer(peer)
        runCatching { discovery.registerPeer(peer) }   // 发 PeerAdded → ip: 会话自动升级
        bus.publish(Event.LogEvent("已直连 ${peer.name}（$clean）"))
        return true to "已直连 ${peer.name}"
    }

    // ------------------------------------------------------------ 已知设备记忆
    private fun loadKnownPeers(): MutableMap<String, JSONObject> {
        val out = HashMap<String, JSONObject>()
        try {
            val raw = store.settingGet("known_peers", "") ?: ""
            if (raw.isNotEmpty()) {
                val obj = JSONObject(raw)
                for (key in obj.keys()) {
                    val value = obj.optJSONObject(key) ?: continue
                    if (value.optString("ip").isNotEmpty()) out[key] = value
                }
            }
        } catch (_: Exception) {
        }
        return out
    }

    /** 把「确实连上过」的对端地址落盘，供下次启动直连回去。 */
    private fun rememberPeer(peer: Peer) {
        if (peer.ip.isEmpty()) return
        val entry = JSONObject()
            .put("ip", peer.ip)
            .put("tcp_port", peer.tcpPort)
            .put("name", peer.name)
            .put("device_type", peer.deviceType)
        if (knownPeers[peer.deviceId]?.toString() == entry.toString()) return
        knownPeers[peer.deviceId] = entry
        while (knownPeers.size > 50) {
            val first = knownPeers.keys.firstOrNull() ?: break
            knownPeers.remove(first)
        }
        runCatching {
            val obj = JSONObject()
            for ((key, value) in knownPeers) obj.put(key, value)
            store.settingSet("known_peers", obj.toString())
        }
    }

    /** 把发送目标从「ip:<addr>」解析成真实 deviceId（未发现则为 null）。 */
    private fun resolveSendTarget(peerId: String): String? {
        if (!peerId.startsWith(MANUAL_PREFIX)) return peerId
        val ip = peerId.removePrefix(MANUAL_PREFIX)
        return discovery.snapshot().firstOrNull { it.ip == ip }?.deviceId
    }

    /** 发现设备后，把对应的手动 IP 会话升级成真实 deviceId。 */
    private fun onPeerForBind(event: Event) {
        val peer = when (event) {
            is Event.PeerAdded -> event.peer
            is Event.PeerUpdated -> event.peer
            else -> return
        }
        bindManualConversation(peer)
    }

    private fun bindManualConversation(peer: Peer) {
        val oldId = MANUAL_PREFIX + peer.ip
        if (peer.deviceId == oldId) return
        val has = runCatching { store.hasConversation(oldId) }.getOrDefault(false)
        if (!has) return
        runCatching { store.rebindConversation(oldId, peer.deviceId, peer.name) }
        // 待发队列里指向「ip:」的消息改指向真实设备
        for (p in outbox.toList()) {
            if (p.peerId == oldId) {
                outbox.remove(p)
                runCatching { p.env.put("to", peer.deviceId) }
                outbox.add(PendingText(peer.deviceId, p.env, p.msg))
            }
        }
        bus.publish(Event.ConversationRebound(oldId, peer.deviceId))
    }

    fun addManualIp(ip: String) {
        val clean = ip.trim()
        if (clean.isEmpty()) return
        discovery.addManualTarget(clean)     // 同网段普通 WiFi：UDP 探测仍有用
        gateway.addCandidate(clean)
        // **socket 直连**（不依赖 UDP 广播）—— 热点主机场景唯一能成的一条
        scope.launch { runCatching { connectByIp(clean) } }
    }

    // ------------------------------------------------------------ 后台驻留
    /** 开机自启开关（默认关闭）。 */
    fun autostartEnabled(): Boolean = store.settingGet("autostart", "0") == "1"

    fun setAutostartEnabled(enabled: Boolean) {
        runCatching { store.settingSet("autostart", if (enabled) "1" else "0") }
    }

    // ------------------------------------------------------------ 身份 / 配对
    /** 本机身份公钥指纹。⚠ **不要显示到界面上**（用户硬要求）。 */
    fun fingerprint(): String = identity?.fingerprint ?: ""

    /** v4 起恒为 true：身份是本机必需的，不存在"没加密"的运行模式。 */
    fun isEncrypted(): Boolean = tunnelCtx != null

    // ---- 配对表 ----
    fun pairedDevices(): List<ChatDb.PairedRow> = store.pairAll()

    fun isPaired(deviceId: String): Boolean = store.pairGet(deviceId)?.trusted == true

    /** 解绑：删掉信任记录并断开当前会话（下次连接需重新配对）。 */
    fun unpair(deviceId: String) {
        runCatching { store.pairRemove(deviceId) }
        transport.sessions().forEach { if (it.peerId == deviceId) runCatching { it.close() } }
        bus.publish(Event.PairedChanged(deviceId, false))
        bus.publish(Event.LogEvent("已解绑 ${peerNameOf(deviceId)}"))
    }

    /** UI 回答「是否与这台设备配对」（配对弹窗的按钮回调）。 */
    fun answerPairing(peerId: String, accept: Boolean) = pairing.answer(peerId, accept)

    private fun peerNameOf(deviceId: String): String =
        discovery.snapshot().firstOrNull { it.deviceId == deviceId }?.name ?: deviceId.take(8)

    // ---- 配对流程：未配对时首次连接走一遍 ----
    /** 懒判定该会话是否已配对；顺带发现"对方换了身份"。 */
    private fun pairState(conn: ControlConnection): Boolean {
        if (conn.peerFp.isEmpty()) return false
        val row = store.pairGet(conn.peerId)
        if (row == null) return false
        if (row.fp != conn.peerFp) {
            if (identityWarned.add(conn.peerId)) {
                bus.publish(Event.LogEvent("对方身份已变更（可能重装/换机），需要重新配对"))
            }
            return false
        }
        return row.trusted
    }

    private val identityWarned = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /** 配对失败过的对端：不再**自动**发起配对，等用户手动（见 [requestPairing]）。 */
    private val pairBlocked = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /**
     * **手动**发起/重试配对（UI 的「配对」按钮）。
     *
     * 配对失败后不会再自动重试（否则「拒绝→重连→再问」会变成连接风暴）；
     * 要让某台设备重新配对，就手动点一次。返回 false 表示当前没有到该设备的连接。
     */
    fun requestPairing(peerId: String): Boolean {
        pairBlocked.remove(peerId)
        var conn = transport.getSession(peerId)
        if (conn == null) {
            // 还没连接 → **先直连一次**再发配对请求（这样"谁都能发配对请求"；
            // 否则未连接时直接返回 false，表现就是"点配对没反应"）。
            val peer = discovery.get(peerId)
            if (peer != null) {
                runCatching { transport.connectControl(peer.ip, peerId, peer.tcpPort) }
                conn = transport.getSession(peerId)
            }
        }
        if (conn == null) return false
        beginPairing(conn)
        return true
    }

    /** 由**拨号方**发起配对：先发 `pair_request`，同时在后台问本端用户。 */
    private fun beginPairing(conn: ControlConnection) {
        if (pairState(conn)) return
        if (conn.peerId in pairBlocked) return   // 之前配对失败过：不再自动发起，等用户手动
        val sas = Pairing.sasCode(conn.peerFp, identity?.fingerprint ?: "", conn.peerNonce)
        val name = peerNameOf(conn.peerId)
        conn.send(Messages.envelope("pair_request", cfg.deviceId, conn.peerId,
            JSONObject().put("name", cfg.deviceName)))
        pairing.askAsync(
            peerId = conn.peerId,
            peerName = name,
            sas = sas,
            onAsk = { bus.publish(Event.PairRequestEvent(it)) },
            onAnswer = { ok ->
                conn.pairOwnOk = ok
                conn.send(Messages.envelope(
                    if (ok) "pair_accept" else "pair_reject", cfg.deviceId, conn.peerId, JSONObject()))
                if (ok) finishPairing(conn, name) else {
                    pairBlocked.add(conn.peerId)
                    bus.publish(Event.LogEvent("与 $name 的配对未完成（可在设备列表里手动点「配对」）"))
                }
            },
        )
    }

    /** 被叫方收到配对请求：算短码 → 问本端用户 → 回 accept/reject。 */
    private fun onPairRequest(conn: ControlConnection, env: JSONObject) {
        val data = env.optJSONObject("data") ?: JSONObject()
        val name = data.optString("name").ifEmpty { peerNameOf(conn.peerId) }
        val sas = Pairing.sasCode(conn.peerFp, identity?.fingerprint ?: "", conn.peerNonce)
        pairing.askAsync(
            peerId = conn.peerId,
            peerName = name,
            sas = sas,
            onAsk = { bus.publish(Event.PairRequestEvent(it)) },
            onAnswer = { ok ->
                conn.pairOwnOk = ok
                conn.send(Messages.envelope(
                    if (ok) "pair_accept" else "pair_reject", cfg.deviceId, conn.peerId, JSONObject()))
                if (ok) finishPairing(conn, name) else bus.publish(Event.LogEvent("已取消配对"))
            },
        )
    }

    /** 收到对端的 `pair_accept` / `pair_ok` → 记下"对端已同意"。 */
    private fun onPeerAcceptedPairing(conn: ControlConnection) {
        conn.pairPeerOk = true
        finishPairing(conn, peerNameOf(conn.peerId))
    }

    /** **两端都同意**后才落库（不产生"半信任"）。 */
    private fun finishPairing(conn: ControlConnection, name: String) {
        if (pairState(conn)) return
        if (!(conn.pairOwnOk && conn.pairPeerOk)) return
        val label = name.ifEmpty { peerNameOf(conn.peerId) }
        val ok = runCatching {
            store.pairUpsert(conn.peerId, conn.peerFp, label)
            true
        }.getOrDefault(false)
        if (!ok) {
            bus.publish(Event.LogEvent("保存配对失败"))
            return
        }
        conn.send(Messages.envelope("pair_ok", cfg.deviceId, conn.peerId,
            JSONObject().put("fp", identity?.fingerprint ?: "")))
        bus.publish(Event.PairedChanged(conn.peerId, true))
        bus.publish(Event.LogEvent("已与 $label 配对"))
    }

    // ------------------------------------------------------------ 生命周期
    fun start() {
        bus.subscribe(::persistTransfer)
        // 收到的文件/图片完成后，往会话里补一条消息（否则接收端聊天里看不到）
        bus.subscribe(::onTransferForChat)
        // 设备被发现后，把手动 IP 会话升级成真实 deviceId
        bus.subscribe(::onPeerForBind)
        // 逐个启动：任一子系统失败不影响其余，否则会表现为「能发现设备但建不起 TCP 连接」
        startErrors.clear()
        // **强制加密**：没设口令就什么都不启（不监听、不广播），
        // 否则等于把明文服务默认暴露在同网段里。在「设置」里设好口令后重启即可。
        if (!isEncrypted()) {
            val msg = "未设置隧道口令 —— 为安全起见已停用网络功能。请在「设置」里设置口令后重试。"
            startErrors.add(msg)
            bus.publish(Event.LogEvent(msg))
            return
        }
        val steps = listOf<Pair<String, () -> Unit>>(
            "发现服务" to { discovery.start() },
            "网关中继" to { gateway.start() },
            "TCP 传输" to { transport.start() },
        )
        for ((label, step) in steps) {
            try {
                step()
            } catch (e: Exception) {
                val msg = "$label 启动失败：${e.message}"
                startErrors.add(msg)
                bus.publish(Event.LogEvent(msg))
            }
        }
        scope.launch { maintenanceLoop() }
    }

    /** 启动期异常（供 UI 展示 / 诊断）。 */
    fun startErrors(): List<String> = startErrors.toList()

    fun stop() {
        if (!stopping.compareAndSet(false, true)) return
        runCatching { transfer.stop() }
        runCatching { transport.stop() }
        runCatching { gateway.stop() }
        runCatching { discovery.stop() }
        scope.cancel()
    }

    private fun persistTransfer(event: Event) {
        if (event is Event.TransferEvent) {
            runCatching { store.upsertTransfer(event.transfer) }
        }
    }

    /** 已处理过的接收文件（避免同一个文件重复插消息）。 */
    private val chatNotified = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()

    /** 接收完成 → 在会话里生成一条 file/image 消息（带本地落盘路径，UI 可直接显示缩略图）。 */
    private fun onTransferForChat(event: Event) {
        val t = (event as? Event.TransferEvent)?.transfer ?: return
        if (t.direction != "recv" || t.state != TransferState.COMPLETED) return
        val f = t.file ?: return
        if (!chatNotified.add(f.fileId)) return
        val entry = f.copy(localPath = t.finalPath ?: f.localPath)
        val msg = ChatMessage(
            msgId = UUID.randomUUID().toString(),
            convId = t.peerId,
            kind = if (entry.kind == "image") MsgKind.IMAGE else MsgKind.FILE,
            outgoing = false,
            status = "received",
            transferId = t.transferId,
            file = entry,
        )
        persistMessage(msg)
        bus.publish(Event.MessageEvent(msg))
    }

    // ------------------------------------------------------------ 文本
    fun sendText(peerId: String, text: String): ChatMessage {
        val clientId = UUID.randomUUID().toString()
        // 手动 IP 会话：设备若已发现就发往真实 deviceId，否则先排队
        val target = resolveSendTarget(peerId)
        val sendTo = target ?: peerId
        val env = Messages.envelope(
            "text", cfg.deviceId, sendTo,
            JSONObject().put("text", text).put("client_id", clientId),
        )
        val ok = target != null && transport.sendTo(target, env)
        val msg = ChatMessage(
            msgId = clientId, convId = peerId, kind = MsgKind.TEXT, text = text,
            outgoing = true, status = if (ok) "sent" else "pending",
        )
        remember(clientId)
        persistMessage(msg)
        // 连不上 / 还没发现设备都不丢消息：先入队，连上后自动补发
        if (!ok) {
            if (target == null) {
                bus.publish(Event.LogEvent("还没发现该 IP 上的设备，消息已排队，发现后自动发送"))
            }
            outbox.add(PendingText(sendTo, env, msg))
        }
        bus.publish(Event.MessageEvent(msg))
        return msg
    }

    private fun onText(env: JSONObject, peerId: String) {
        val data = env.optJSONObject("data") ?: JSONObject()
        val clientId = data.optString("client_id").ifEmpty { env.optString("id") }
        val text = data.optString("text")
        if (clientId.isNotEmpty() && isDuplicate(clientId)) return
        val msgId = clientId.ifEmpty { UUID.randomUUID().toString() }
        val msg = ChatMessage(
            msgId = msgId, convId = peerId, kind = MsgKind.TEXT, text = text,
            ts = env.optLong("ts", System.currentTimeMillis()), outgoing = false, status = "received",
        )
        persistMessage(msg)
        bus.publish(Event.MessageEvent(msg))
        transport.getSession(peerId)?.send(
            Messages.envelope("delivered", cfg.deviceId, peerId, JSONObject().put("msg_id", msgId)),
        )
    }

    private fun onDelivered(env: JSONObject) {
        val msgId = env.optJSONObject("data")?.optString("msg_id") ?: return
        if (msgId.isEmpty()) return
        // ⚠ 不要直接改 msg.status：UI 的 _messages 里是同一个实例，原地改会让 StateFlow
        // 认为值没变而不发通知（Compose 不刷新）。状态统一走 DB + MessageStatusEvent。
        runCatching { store.updateMessageStatus(msgId, "delivered") }
        bus.publish(Event.MessageStatusEvent(msgId, "delivered"))
    }

    // ------------------------------------------------------------ 文件
    fun sendFiles(peerId: String, sources: List<com.dasinong.backend.net.LocalFile>): String? {
        if (sources.isEmpty()) return null
        // 手动 IP 会话：设备若已发现就发往真实 deviceId，否则提示稍后再试
        val target = resolveSendTarget(peerId)
        if (target == null) {
            bus.publish(Event.LogEvent("还没连上该 IP 上的设备，暂时发不了文件/图片；可在会话里先「直连」该 IP"))
            return null
        }
        val tid = transfer.sendFiles(target, sources) ?: return null
        for (src in sources) {
            val kind = if (src.kind == "image") MsgKind.IMAGE else MsgKind.FILE
            val entry = FileEntry(
                fileId = UUID.randomUUID().toString(), name = src.name, relPath = src.relPath,
                rootName = src.rootName, size = src.size, kind = src.kind, mtime = src.mtime,
                localPath = src.source.ifEmpty { null },
            )
            val msg = ChatMessage(
                msgId = UUID.randomUUID().toString(), convId = peerId, kind = kind,
                outgoing = true, status = "sent", transferId = tid, file = entry,
            )
            persistMessage(msg)
            bus.publish(Event.MessageEvent(msg))
        }
        return tid
    }

    fun cancelTransfer(transferId: String) = transfer.cancel(transferId)

    /**
     * 删除一条传输记录：连同**这一条**对应的落盘文件一起删掉。
     *
     * final_path 是逐文件的，所以文件夹传输里其它文件不受影响。
     * 进行中（等待/传输中）拒绝删除，先「取消」再删。返回 (是否成功, 说明)。
     */
    fun deleteTransfer(fileId: String): Pair<Boolean, String> {
        val row = runCatching { store.transferRow(fileId) }.getOrNull()
            ?: return false to "记录不存在（可能已被删除）"
        if (row.state == "pending" || row.state == "active") {
            return false to "传输进行中，请先「取消」再删除"
        }
        var removed = false
        val path = row.finalPath
        if (!path.isNullOrEmpty()) {
            val f = java.io.File(path)
            try {
                if (f.isDirectory) {
                    // 兜底：万一记录指向目录，只移除记录，不整棵树删掉
                } else if (f.exists()) {
                    if (!f.delete()) return false to "删除文件失败（可能被其它程序占用）"
                    removed = true
                }
            } catch (e: Exception) {
                return false to "删除文件失败：${e.message}"
            }
        }
        val delErr = runCatching { store.deleteTransfer(fileId) }.exceptionOrNull()
        if (delErr != null) return false to "删除记录失败：${delErr.message}"
        bus.publish(Event.TransferRemoved(fileId))
        return true to (if (removed) "已删除文件" else "已从列表移除")
    }

    /**
     * 一键清空传输列表：删掉所有**已结束**的记录，进行中的保留；
     * 每条记录对应的落盘文件也一并删除（与单条删除一致）。返回 (是否成功, 说明)。
     */
    fun clearTransfers(): Pair<Boolean, String> {
        val rows = runCatching { store.transfers(limit = 100000) }.getOrElse {
            return false to "读取记录失败：${it.message}"
        }
        val ids = ArrayList<String>()
        var files = 0
        var kept = 0
        for (r in rows) {
            if (r.state == "pending" || r.state == "active") { kept++; continue }
            val path = r.finalPath
            if (!path.isNullOrEmpty()) {
                val f = java.io.File(path)
                try {
                    if (!f.isDirectory && f.exists() && f.delete()) files++
                } catch (_: Exception) {
                    // 删不掉就只移除记录
                }
            }
            ids.add(r.fileId)
        }
        val delErr = runCatching { store.deleteTransfers(ids) }.exceptionOrNull()
        if (delErr != null) return false to "清空记录失败：${delErr.message}"
        bus.publish(Event.LogEvent("已清空传输列表（${ids.size} 条，删除文件 $files 个）"))
        var msg = "已清空 ${ids.size} 条记录"
        if (files > 0) msg += "，删除 $files 个文件"
        if (kept > 0) msg += "；$kept 个进行中任务已保留"
        return true to msg
    }

    // ------------------------------------------------------------ 控制派发
    private fun onControl(conn: ControlConnection, env: JSONObject) {
        val peerId = conn.peerId
        val mtype = env.optString("type")
        // ---- 配对协商（任何一侧都可能发）----
        when (mtype) {
            "pair_request" -> { onPairRequest(conn, env); return }
            "pair_accept", "pair_ok" -> { onPeerAcceptedPairing(conn); return }
            "pair_reject" -> {
                pairBlocked.add(peerId)
                bus.publish(Event.LogEvent("对方拒绝配对（可在设备列表里手动点「配对」重试）"))
                return
            }
        }
        // ---- 保活（配对前也允许）----
        if (mtype == "ping") {
            conn.send(Messages.envelope("pong", cfg.deviceId, peerId, JSONObject()))
            return
        }
        if (mtype == "pong") {
            conn.lastPong = System.currentTimeMillis()
            return
        }
        // ---- 未配对闸门：除保活/配对外，业务帧一律拒绝 ----
        if (!pairState(conn)) {
            runCatching {
                conn.send(Messages.envelope(
                    "error", cfg.deviceId, peerId,
                    JSONObject().put("code", "unpaired").put("message", "尚未配对：请先在对端确认配对"),
                ))
            }
            return
        }
        when (mtype) {
            "text" -> onText(env, peerId)
            "delivered" -> onDelivered(env)
            else -> if (mtype in transferTypes) {
                runCatching { transfer.handleControl(conn, env) }
            }
        }
    }

    private fun onData(sock: Socket, type: Int, body: ByteArray, ip: String) {
        runCatching { transfer.handleDataConnection(sock, type, body, ip) }
            .onFailure { runCatching { sock.close() } }
    }

    // ------------------------------------------------------------ 诊断
    fun diagnose(): String {
        val sb = StringBuilder()
        if (startErrors.isNotEmpty()) {
            sb.append("启动异常：\n")
            startErrors.forEach { sb.append("  ! ").append(it).append('\n') }
        }
        sb.append("设备名：").append(cfg.deviceName).append('\n')
        sb.append("设备 ID：").append(cfg.deviceId).append('\n')
        sb.append("监听：UDP ").append(cfg.udpPort).append(" / TCP ").append(cfg.tcpPort)
        sb.append(if (transport.isListening) "（TCP 服务端运行中）" else "（TCP 服务端未运行！）").append('\n')

        val nets = binder.candidateLans()
        sb.append("候选局域网：").append(nets.size).append(" 个\n")
        for (n in nets) {
            sb.append("  - ").append(binder.interfaceName(n) ?: "?")
                .append("  ").append(binder.sourceIp(n)?.hostAddress ?: "?")
                .append("  广播 ").append(binder.directedBroadcast(n)?.hostAddress ?: "?")
                .append('\n')
        }
        if (nets.isEmpty()) sb.append("  （未识别到 WiFi/以太网：无互联网热点下可能取不到 Network）\n")

        val list = peers()
        if (list.isEmpty()) {
            sb.append("未发现任何设备\n")
        } else {
            sb.append("发现 ").append(list.size).append(" 台：\n")
            for (p in list) sb.append("  ").append(diagnosePeer(p)).append('\n')
        }
        return sb.toString()
    }

    private fun diagnosePeer(peer: Peer): String {
        val head = peer.name + " @ " + peer.ip + ":" + peer.tcpPort + "  " +
            (if (isConnected(peer.deviceId)) "控制连接=已建立" else "控制连接=未建立")
        val probe = try {
            binder.connectTcpAny(peer.ip, peer.tcpPort, 3000).use { "TCP 探测=成功" }
        } catch (e: java.net.SocketTimeoutException) {
            "TCP 探测=超时（对方多半拦了入站 TCP，多为防火墙）"
        } catch (e: java.net.ConnectException) {
            "TCP 探测=被拒绝（对方端口没在监听：程序没运行或端口不一致）"
        } catch (e: Exception) {
            "TCP 探测=失败（${e.javaClass.simpleName}: ${e.message}）"
        }
        return head + "  " + probe
    }

    // ------------------------------------------------------------ 待发队列
    private class PendingText(val peerId: String, val env: JSONObject, val msg: ChatMessage)

    private fun flushOutbox() {
        if (outbox.isEmpty()) return
        for (pending in outbox.toList()) {
            if (transport.sendTo(pending.peerId, pending.env)) {
                outbox.remove(pending)
                // ⚠ 不要原地改 pending.msg.status：UI 的 _messages 里是同一个实例，
                // 原地改会让 StateFlow 认为值没变而不发通知（气泡一直卡在“待发送”）。
                // 状态统一走 DB + MessageStatusEvent，由 UI 生成不可变的新对象。
                runCatching { store.updateMessageStatus(pending.msg.msgId, "sent") }
                bus.publish(Event.MessageStatusEvent(pending.msg.msgId, "sent"))
            }
        }
    }

    // ------------------------------------------------------------ 维护循环
    /** 上次「直连网关做发现」的时间（节流）。 */
    private var gwDiscoverAt = System.currentTimeMillis()

    /** 探网关是否正在进行（防重入叠加）。 */
    @Volatile private var gwProbing = false

    /**
     * **自动发现**兜底：TCP 直连默认网关（热点主机）把对方登记进发现表。
     *
     * 谁当客户端谁就探一次 —— 这样「手机开热点」和「电脑开热点」两种组网都能互相发现；
     * 局域网里 UDP 广播本来就好使，这里只是兜底。⚠ 只做发现，不自动配对。
     */
    private fun autoDiscoverGateway(now: Long) {
        if (discovery.snapshot().isNotEmpty()) return
        if (transport.sessions().isNotEmpty()) return      // 已有会话，不必再探
        if (gwProbing) return                              // 上一次还在探 → 别叠加
        if (now - gwDiscoverAt < 8_000L) return
        gwDiscoverAt = now
        val gw = runCatching { binder.defaultGateway() }.getOrNull() ?: return
        gwProbing = true
        scope.launch {
            try {
                runCatching { connectByIp(gw) }
            } finally {
                gwProbing = false
            }
        }
    }

    private suspend fun maintenanceLoop() {
        while (scope.isActive && !stopping.get()) {
            val now = System.currentTimeMillis()
            ensureSessions(now)
            autoDiscoverGateway(now)           // 自动发现（只登记，不配对）
            pingSessions(now)
            flushOutbox()
            runCatching { gateway.prune() }
            delay(1000)
        }
    }

    private fun ensureSessions(now: Long) {
        val peers = ArrayList(discovery.snapshot())
        // 发现表被 UDP 丢包剪掉、但我们最近还见过的对端也纳入进来，保证还能重连
        val known = peers.mapTo(HashSet()) { it.deviceId }
        for ((pid, cached) in endpoints) {
            if (pid !in known && now - cached.second < ENDPOINT_TTL_MS) {
                peers.add(cached.first)
                known.add(pid)
            }
        }
        // 再补上「以前连上过、地址已落盘」的对端：手机开热点时 UDP 永远发现不到，
        // 只有靠记忆里的地址直连回去。
        for ((pid, info) in knownPeers) {
            if (pid in known) continue
            val ip = info.optString("ip")
            if (ip.isEmpty()) continue
            peers.add(
                Peer(
                    deviceId = pid,
                    name = info.optString("name").ifEmpty { pid.take(8) },
                    ip = ip,
                    tcpPort = dialPortOf(info),
                    udpPort = cfg.udpPort,
                    deviceType = info.optString("device_type").ifEmpty { "unknown" },
                ),
            )
            known.add(pid)
        }
        for (peer in peers) {
            val pid = peer.deviceId
            if (peer.incompatible) {
                // 版本协商不通过：能看见但不拨号，避免白发连接与一堆失败日志
                continue
            }
            var remember = false
            var online = peer
            val sess = transport.getSession(pid)
            if (sess != null) {
                // 在线会话的**远端地址**才是真地址：发现表/记忆里可能是旧的（换网络后）。
                // 不修正的话，数据连接会拿着旧 IP（比如上一次 WLAN 的地址）去发，必然失败。
                val liveIp = sess.remoteIp()
                if (liveIp != null && liveIp != peer.ip) {
                    online = Peer(
                        deviceId = peer.deviceId, name = peer.name, ip = liveIp,
                        tcpPort = peer.tcpPort, udpPort = peer.udpPort,
                        deviceType = peer.deviceType, proto = peer.proto,
                        encrypted = peer.encrypted,
                    )
                    runCatching { discovery.registerPeer(online) }   // 修正地址（变更时会通知 UI）
                }
                endpoints[pid] = online to now
                remember = true
                // 有连接就说明对方活着：
                // 1) 刷新 lastSeen —— 否则 UDP 通不了的对端（手机热点）每 10 秒被剪一次，
                //    附近列表就会「删了又加」地闪；
                // 2) 真被剪掉了就补回去（附近列表也会跟着恢复）。
                if (discovery.get(pid) == null) {
                    runCatching { discovery.registerPeer(online) }
                } else {
                    discovery.touch(pid)
                }
                attempts.remove(pid)
                backoffUntil.remove(pid)
                noSessionSince.remove(pid)
            } else {
                // ⚠ **不做任何自动重连**：闲置关闭时长见设置页；「保持长连接」= 无限期不关。
                // 掉线后要连请手动点「连接」。
            }
            if (remember) rememberPeer(online)
        }
    }

    /** 「保持长连接」开关（落盘，重启后保持）：更密心跳 + 空闲不断开。 */
    @Volatile
    var keepAlive: Boolean =
        runCatching { store.settingGet("keep_alive", "0") == "1" }.getOrDefault(false)

    /** 连接闲置多久自动关闭（分钟，落盘）。「保持长连接」开着时无限期。 */
    @Volatile
    var connTimeoutMin: Int =
        runCatching { (store.settingGet("conn_timeout_min", "10") ?: "10").toInt() }
            .getOrDefault(10)

    /** 「TCP 并发」：一次传输并发几条数据连接（1~8，填 1 = 单连接）。落盘。 */
    @Volatile
    var tcpConcurrency: Int =
        runCatching { (store.settingGet("tcp_concurrency", "3") ?: "3").toInt() }
            .getOrDefault(3)
            .coerceIn(1, 8)

    // 启动即把设置里的并发数写进传输引擎（否则只有改设置那一刻才生效）
    init { runCatching { transfer.maxParallel = tcpConcurrency } }

    fun concurrency(): Int = tcpConcurrency

    fun setConcurrency(n: Int) {
        tcpConcurrency = n.coerceIn(1, 8)
        runCatching { transfer.maxParallel = tcpConcurrency }   // 立刻生效（并发发送数）
        runCatching { store.settingSet("tcp_concurrency", tcpConcurrency.toString()) }
    }

    fun connTimeoutMinutes(): Int = connTimeoutMin

    fun setConnTimeoutMinutes(minutes: Int) {
        connTimeoutMin = minutes.coerceAtLeast(1)
        runCatching { store.settingSet("conn_timeout_min", connTimeoutMin.toString()) }
    }

    fun setKeepAliveEnabled(on: Boolean) {
        keepAlive = on
        runCatching { store.settingSet("keep_alive", if (on) "1" else "0") }
        bus.publish(Event.LogEvent(if (on) "已开启「保持长连接」" else "已关闭「保持长连接」"))
        if (on) {
            // 一次性：把已发现的设备各接一次（之后不再自动重连、也不会自动关）
            scope.launch {
                runCatching {
                    for (peer in discovery.snapshot()) {
                        if (transport.getSession(peer.deviceId) == null) {
                            runCatching {
                                transport.connectControl(peer.ip, peer.deviceId, peer.tcpPort)
                            }
                        }
                    }
                }
            }
        }
    }

    private fun pingSessions(now: Long) {
        val pingMs = if (keepAlive) KEEPALIVE_PING_MS else Proto.PING_INTERVAL_MS
        // 闲置关闭：保持长连接 = **无限期**；否则用设置里的分钟数
        val timeoutMs = if (keepAlive) Long.MAX_VALUE else connTimeoutMin.toLong() * 60_000L
        for (conn in transport.sessions()) {
            if (now - conn.lastPong > timeoutMs) {
                conn.close()
                continue
            }
            if (now - (lastPing[conn.peerId] ?: 0L) >= pingMs) {
                lastPing[conn.peerId] = now
                conn.send(Messages.envelope("ping", cfg.deviceId, conn.peerId, JSONObject()))
            }
        }
    }

    // ------------------------------------------------------------ 辅助
    private fun remember(key: String) {
        seen[key] = true
        if (seen.size > 2048) {
            val iter = seen.keys.iterator()
            repeat(512) { if (iter.hasNext()) { iter.next(); iter.remove() } }
        }
    }

    private fun isDuplicate(key: String): Boolean {
        if (seen.containsKey(key)) return true
        remember(key)
        return false
    }

    private fun persistMessage(msg: ChatMessage) = runCatching {
        // addMessage 已顺带登记会话；这里再把对端名字补成标题（离线时也能显示）
        store.addMessage(msg)
        val name = peerName(msg.convId)
        if (!name.isNullOrEmpty()) store.ensureConversation(msg.convId, name)
    }
}
