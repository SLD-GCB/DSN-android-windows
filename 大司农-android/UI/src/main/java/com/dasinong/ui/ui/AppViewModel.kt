package com.dasinong.ui.ui

import android.app.Application
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings
import androidx.core.app.NotificationManagerCompat
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.dasinong.backend.Event
import com.dasinong.backend.NodeEngine
import com.dasinong.backend.PairRequest
import kotlin.concurrent.thread
import com.dasinong.backend.model.ChatMessage
import com.dasinong.backend.model.Peer
import com.dasinong.backend.model.Transfer
import com.dasinong.backend.store.ChatDb
import com.dasinong.ui.EngineHolder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** UI 状态：订阅后端事件总线的数据镜像（网络线程写、Compose 主线程读）。 */
class AppViewModel(app: Application) : AndroidViewModel(app) {

    val engine: NodeEngine = EngineHolder.getOrCreate(app)

    private val _peers = MutableStateFlow(engine.peers())
    val peers: StateFlow<List<Peer>> = _peers.asStateFlow()

    private val _messages = MutableStateFlow<Map<String, List<ChatMessage>>>(emptyMap())
    val messages: StateFlow<Map<String, List<ChatMessage>>> = _messages.asStateFlow()

    private val _convs = MutableStateFlow<List<ConvItem>>(emptyList())
    val convs: StateFlow<List<ConvItem>> = _convs.asStateFlow()

    /** 每个任务的「速度 · 用时」文本（传输页展示用）。 */
    private val _transferStat = MutableStateFlow<Map<String, String>>(emptyMap())
    val transferStat: StateFlow<Map<String, String>> = _transferStat.asStateFlow()

    /** 速度/用时统计：[开始ms, 上次字节数, 上次时间ms, 速度B/s] */
    private val transferStatRaw = HashMap<String, LongArray>()

    private fun humanSpeed(bps: Long): String = when {
        bps >= 1024 * 1024 -> "%.1f MB/s".format(bps / 1024.0 / 1024.0)
        bps >= 1024 -> "%.0f KB/s".format(bps / 1024.0)
        else -> "$bps B/s"
    }

    private val _transfers = MutableStateFlow<Map<String, Transfer>>(emptyMap())
    val transfers: StateFlow<Map<String, Transfer>> = _transfers.asStateFlow()

    private val _currentConv = MutableStateFlow<String?>(null)
    val currentConv: StateFlow<String?> = _currentConv.asStateFlow()

    /** 一次性提示（直连成功/失败等），UI 弹完 toast 清掉。 */
    private val _notice = MutableStateFlow<String?>(null)
    val notice: StateFlow<String?> = _notice.asStateFlow()

    // ⚠ 这两个必须声明在 `init` **之前**：Kotlin 按书写顺序初始化字段，
    // `init` 里的 reloadPaired() 会写 `_paired`，声明在后就会是 null → 空指针。
    /** 等待用户确认的配对请求（弹 6 位短码）。 */
    private val _pairRequest = MutableStateFlow<PairRequest?>(null)
    val pairRequest: StateFlow<PairRequest?> = _pairRequest.asStateFlow()

    /** 已配对设备（设置页列表）。 */
    private val _paired = MutableStateFlow<List<ChatDb.PairedRow>>(emptyList())
    val paired: StateFlow<List<ChatDb.PairedRow>> = _paired.asStateFlow()

    data class ConvItem(val peerId: String, val title: String, val preview: String)

    init {
        engine.bus.subscribe(::onEvent)
        // 数据库读取出问题也要**让界面能打开**（把原因弹出来，而不是闪退）
        runCatching { refreshConvs() }
            .onFailure { _notice.value = "会话读取失败：${it.javaClass.simpleName}: ${it.message}" }
        runCatching { reloadTransfers() }
            .onFailure { _notice.value = "传输记录读取失败：${it.javaClass.simpleName}: ${it.message}" }
        runCatching { reloadPaired() }
            .onFailure { _notice.value = "配对表读取失败：${it.javaClass.simpleName}: ${it.message}" }
    }

    private fun onEvent(event: Event) {
        when (event) {
            is Event.PeerAdded, is Event.PeerUpdated, is Event.PeerRemoved -> {
                _peers.value = engine.peers()
                refreshConvs()
            }
            is Event.MessageEvent -> {
                append(event.message)
                refreshConvs()
            }
            is Event.MessageStatusEvent -> updateStatus(event.msgId, event.status)
            is Event.TransferEvent -> {
                val t = event.transfer
                val key = t.file?.fileId ?: t.transferId
                _transfers.value = _transfers.value + (key to t)
                // 速度 + 用时（首次见到该任务记开始时间；用增量算瞬时速度，~0.4s 刷新）
                val nowMs = System.currentTimeMillis()
                val st = transferStatRaw.getOrPut(key) { longArrayOf(nowMs, 0L, nowMs, 0L) }
                val done = if (t.direction == "recv") t.received else t.sent
                val finished = t.state.name == "COMPLETED" || t.state.name == "FAILED"
                if (finished) {
                    st[3] = 0L
                } else {
                    val dt = nowMs - st[2]
                    if (dt >= 400) {
                        st[3] = ((done - st[1]).coerceAtLeast(0L) * 1000L) / dt
                        st[1] = done
                        st[2] = nowMs
                    }
                }
                val el = ((nowMs - st[0]).coerceAtLeast(0L)) / 1000L
                val speedTxt = if (st[3] > 0L) humanSpeed(st[3]) else "—"
                _transferStat.value = _transferStat.value +
                    (key to "%s · %02d:%02d".format(speedTxt, el / 60, el % 60))
                // 传输**完成**时，后端会在会话里补一条文件/图片消息（收发两端都补）。
                // 这里把该会话的消息重取一次 —— 聊天框才会像普通消息一样立刻出现。
                if (t.state.name == "COMPLETED") {
                    val convId = t.peerId
                    if (_messages.value.containsKey(convId)) {
                        _messages.value = _messages.value + (convId to engine.history(convId))
                    }
                    refreshConvs()
                }
            }
            is Event.TransferRemoved -> _transfers.value = _transfers.value - event.fileId
            is Event.ConversationRebound -> onConvRebound(event.oldId, event.newId)
            is Event.PairRequestEvent -> _pairRequest.value = event.request
            is Event.PairedChanged -> {
                reloadPaired()
                _peers.value = engine.peers()
            }
            is Event.LogEvent -> {
                // 瞬时「握手失败」不弹提示：App 从后台回前台时，双方几乎同时重连，
                // 其中一条会被「保留 canon 方向」的规则关掉 → 它的 TLS 握手半途而废，
                // 随后另一条正常接管。**功能不受影响**，弹出来只会误导用户。
                val txt = event.text
                val transient = txt.contains("握手失败") || txt.contains("隧道握手")
                if (!transient) _notice.value = txt
            }
        }
    }

    /** 手动 IP 会话升级成真实设备：迁移内存消息、切换当前会话、刷新列表。 */
    private fun onConvRebound(oldId: String, newId: String) {
        val moved = _messages.value[oldId]
        if (moved != null) {
            _messages.value = (_messages.value - oldId) +
                (newId to moved.map { it.copy(convId = newId) })
        }
        if (_currentConv.value == oldId) {
            _currentConv.value = newId
            _messages.value = _messages.value + (newId to engine.history(newId))
        }
        refreshConvs()
    }

    // ------------------------------------------------------------ 操作
    fun openConv(peerId: String) {
        // 打开即登记会话，保证对端离线后仍留在列表里
        engine.touchConversation(peerId)
        _currentConv.value = peerId
        _messages.value = _messages.value + (peerId to engine.history(peerId))
    }

    /** 新建会话：登记后立即打开。 */
    fun createConv(peerId: String, title: String? = null) {
        engine.touchConversation(peerId, title)
        refreshConvs()
        openConv(peerId)
    }

    /** 按 IP 新建会话：立刻出现占位会话，同时**后台直连**。
     *
     * 直连（TCP + 身份应答）不依赖 UDP 发现 —— 手机开热点时 Android 不暴露热点网卡，
     * 广播 hello 根本发不出去，只有这条路能连上。成功后对端会被登记进发现表，
     * `ip:<addr>` 会话自动升级成真实设备。
     */
    fun createConvFromIp(ip: String) {
        val clean = ip.trim()
        val id = engine.createConversationByIp(clean)
        if (id.isEmpty()) return
        refreshConvs()
        openConv(id)
        viewModelScope.launch(Dispatchers.IO) {
            val result = runCatching { engine.connectByIp(clean) }
                .getOrElse { false to (it.message ?: "直连失败") }
            _notice.value = if (result.first) result.second else "直连 $clean 失败：${result.second}"
        }
    }

    /** 一次性提示（UI 弹 toast 后调 clearNotice 清掉）。 */
    fun clearNotice() {
        _notice.value = null
    }

    /** 只知 IP 的直连（不建会话，只把连接建起来并登记对端）。 */
    fun directConnect(ip: String) {
        val clean = ip.trim()
        if (clean.isEmpty()) return
        viewModelScope.launch(Dispatchers.IO) {
            val result = runCatching { engine.connectByIp(clean) }
                .getOrElse { false to (it.message ?: "直连失败") }
            if (result.first) refreshConvs()
            _notice.value = if (result.first) result.second else "直连 $clean 失败：${result.second}"
        }
    }

    /** 是否是「手动 IP」合成会话（还没发现对应设备）。 */
    fun isManualConv(convId: String): Boolean = engine.isManualConv(convId)

    /** 清空某会话的聊天记录（会话保留在列表里）。 */
    fun clearConvMessages(convId: String) {
        engine.clearMessages(convId)
        _messages.value = _messages.value + (convId to engine.history(convId))
        refreshConvs()
    }

    /** 删掉某一条消息（只删本机记录，不影响对方）。 */
    fun deleteMessage(convId: String, msgId: String) {
        engine.deleteMessage(msgId)
        _messages.value = _messages.value + (convId to engine.history(convId))
        refreshConvs()
    }

    /** 删除会话：清空聊天记录 + 从列表移除（该设备在线也不再自动出现）。 */
    fun deleteConv(peerId: String) {
        engine.deleteConversation(peerId)
        if (_currentConv.value == peerId) _currentConv.value = null
        _messages.value = _messages.value - peerId
        refreshConvs()
    }

    /** 从会话返回会话列表。 */
    fun backToList() {
        _currentConv.value = null
    }

    fun isConnectedTo(peerId: String): Boolean = engine.isConnected(peerId)

    /**
     * v4：业务数据只在**已配对**后才发得出去。未配对/版本过旧时给明确提示并拦下，
     * 而不是让它静默失败（后端对此类帧也会直接回 `error{code:"unpaired"}`）。
     */
    private fun guardPaired(peerId: String): Boolean {
        val peer = engine.peers().firstOrNull { it.deviceId == peerId }
        if (peer?.incompatible == true) {
            _notice.value = "双方版本不一致，请把两端都升级到最新版"
            return false
        }
        if (!engine.isPaired(peerId)) {
            _notice.value = "还没配对完成：请等配对弹窗出现，核对两端 6 位数字一致后点「配对」"
            return false
        }
        return true
    }

    fun sendText(peerId: String, text: String) {
        if (text.isBlank()) return
        if (!guardPaired(peerId)) return
        // 放**后台线程**：发送里可能含同步拨号（最多几秒），别卡住界面（"发送慢"的主因）
        thread(name = "send-text") {
            runCatching {
                engine.sendText(peerId, text)
                _messages.value = _messages.value +
                    (peerId to engine.history(peerId).distinctBy { it.msgId })
                refreshConvs()
            }
        }
    }

    fun sendFiles(peerId: String, files: List<com.dasinong.backend.net.LocalFile>) {
        if (files.isEmpty()) return
        if (!guardPaired(peerId)) return
        // 放**后台线程**：发送里可能含同步拨号/读库/刷新，别卡住界面（这是"发送慢"的主因）
        thread(name = "send-files") {
            runCatching {
                engine.sendFiles(peerId, files)
                // **立刻重取**该会话消息（气泡含进度当场出现），并**按 msgId 去重**
                _messages.value = _messages.value +
                    (peerId to engine.history(peerId).distinctBy { it.msgId })
                refreshConvs()
            }
        }
    }

    fun addManualIp(ip: String) = engine.addManualIp(ip)

    // ------------------------------------------------------------ 后台驻留
    /** 开机自启开关（默认关）。 */
    fun autostart(): Boolean = engine.autostartEnabled()

    fun setAutostart(enabled: Boolean) = engine.setAutostartEnabled(enabled)

    /** 是否已在电池优化白名单里（不在 = 后台容易被系统杀掉）。 */
    fun isIgnoringBatteryOptimizations(context: Context): Boolean {
        val pm = context.getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return true
        return runCatching { pm.isIgnoringBatteryOptimizations(context.packageName) }.getOrDefault(true)
    }

    /** 跳到「忽略电池优化」授权页（该权限已声明；个别 ROM 不支持带 package 的直达，回退到列表页）。 */
    fun requestIgnoreBatteryOptimization(context: Context) {
        val direct = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.parse("package:${context.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (runCatching { context.startActivity(direct) }.isFailure) {
            runCatching {
                context.startActivity(
                    Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            }
        }
    }

    fun notificationsAllowed(context: Context): Boolean =
        NotificationManagerCompat.from(context).areNotificationsEnabled()

    fun openNotificationSettings(context: Context) {
        runCatching {
            context.startActivity(
                Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                    .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
        }
    }

    fun cancelTransfer(transferId: String) = engine.cancelTransfer(transferId)

    /** 从数据库回填历史传输记录（启动时 + 清空后重建）。 */
    fun reloadTransfers() {
        val list = engine.persistedTransfers()
        _transfers.value = list.associateBy { it.file?.fileId ?: it.transferId }
    }

    /** 删除一条传输（连同已下载到本机的文件）。成功返回 null，失败返回提示文字。 */
    fun deleteTransfer(fileId: String): String? {
        val (ok, msg) = engine.deleteTransfer(fileId)
        return if (ok) null else msg
    }

    /** 一键清空传输列表（已结束的记录 + 文件一起删，进行中的保留）。提示通过 notice 弹出。 */
    fun clearTransfers() {
        val (_, msg) = engine.clearTransfers()
        reloadTransfers()
        _notice.value = msg
    }

    fun deviceName(): String = engine.cfg.deviceName

    fun deviceId(): String = engine.cfg.deviceId

    fun receiveDir(): String = engine.cfg.receiveDir.absolutePath

    fun isConnected(peerId: String): Boolean = engine.isConnected(peerId)

    /** 启动期异常（如端口被占用），用于在设置页展示。 */
    fun startErrors(): List<String> = engine.startErrors()

    fun peersCount(): Int = engine.peers().size

    // ------------------------------------------------------------ 身份 / 配对
    /** v4 起加密恒开（每设备独立身份）—— 只用于显示状态，**不显示指纹**。 */
    fun isEncrypted(): Boolean = engine.isEncrypted()

    fun reloadPaired() {
        _paired.value = engine.pairedDevices()
    }

    fun isPaired(deviceId: String): Boolean = engine.isPaired(deviceId)

    /**
     * **手动配对**：没有连接会先直连一次再发配对请求（放后台线程，避免卡界面）。
     */
    fun pairWith(deviceId: String) {
        thread(name = "manual-pair") {
            val ok = runCatching { engine.requestPairing(deviceId) }.getOrDefault(false)
            _notice.value = if (ok) {
                "已向对方发起配对：核对两端 6 位数字一致后点「配对」"
            } else {
                "暂时连不上，请确认在同一局域网后重试"
            }
        }
    }

    /** **手动连接**某台设备（退出再进软件后可手动连上；不自动）。 */
    fun connectPeer(deviceId: String) {
        val peer = engine.peers().firstOrNull { it.deviceId == deviceId } ?: return
        thread(name = "manual-connect") {
            val (ok, msg) = runCatching { engine.connectByIp(peer.ip, peer.tcpPort) }
                .getOrDefault(false to "连接异常")
            _notice.value = msg.ifEmpty { if (ok) "已连接" else "连接失败" }
        }
    }

    /** 「保持长连接」开关（附近页胶囊开关）。 */
    private val _keepAlive = MutableStateFlow(engine.keepAlive)
    val keepAlive: StateFlow<Boolean> = _keepAlive.asStateFlow()

    fun setKeepAliveEnabled(on: Boolean) {
        engine.setKeepAliveEnabled(on)
        _keepAlive.value = on
    }

    /** 连接闲置多少分钟后自动关闭（分钟）。 */
    fun connTimeoutMinutes(): Int = engine.connTimeoutMinutes()

    fun setConnTimeoutMinutes(minutes: Int) = engine.setConnTimeoutMinutes(minutes)

    /** 「TCP 并发」：一次传输并发几条数据连接（1~8）。 */
    fun concurrency(): Int = engine.concurrency()

    fun setConcurrency(n: Int) = engine.setConcurrency(n)

    /** 手动刷新：设备列表 + 会话 + 配对（下拉刷新 / 刷新按钮都用它）。 */
    fun refreshAll() {
        runCatching { _peers.value = engine.peers() }
        runCatching { refreshConvs() }
        runCatching { reloadPaired() }
    }

    /** 手动发起/重试配对（配对失败后不会再自动重试）。 */
    fun requestPairing(deviceId: String): Boolean = engine.requestPairing(deviceId)

    /** 用户对配对弹窗的回答。 */
    fun answerPairing(peerId: String, accept: Boolean) {
        engine.answerPairing(peerId, accept)
        _pairRequest.value = null
    }

    fun unpair(deviceId: String) {
        engine.unpair(deviceId)
        reloadPaired()
    }

    // ------------------------------------------------------------ 诊断
    private val _diagText = MutableStateFlow<String?>(null)
    val diagText: StateFlow<String?> = _diagText.asStateFlow()

    private val _diagRunning = MutableStateFlow(false)
    val diagRunning: StateFlow<Boolean> = _diagRunning.asStateFlow()

    fun runDiagnose() {
        if (_diagRunning.value) return
        _diagRunning.value = true
        viewModelScope.launch {
            val text = withContext(Dispatchers.IO) {
                runCatching { engine.diagnose() }.getOrElse { "诊断失败：${it.message}" }
            }
            _diagText.value = text
            _diagRunning.value = false
        }
    }

    fun clearDiag() {
        _diagText.value = null
    }

    // ------------------------------------------------------------ 内部
    private fun append(msg: ChatMessage) {
        val list = _messages.value[msg.convId].orEmpty()
        if (list.any { it.msgId == msg.msgId }) return
        // 按 msgId 去重：后端可能同时"写库 + 发总线事件"，重取时同一条会被捞到两次
        _messages.value = _messages.value +
            (msg.convId to (list.filterNot { it.msgId == msg.msgId } + msg))
    }

    private fun updateStatus(msgId: String, status: String) {
        val updated = _messages.value.mapValues { (_, list) ->
            list.map { if (it.msgId == msgId) it.copy(status = status) else it }
        }
        _messages.value = updated
    }

    fun refreshConvs() {
        val peersById = engine.peers().associateBy { it.deviceId }
        val hidden = engine.hiddenConversationIds()
        val items = ArrayList<ConvItem>()
        val seen = HashSet<String>()
        // 1) 已登记（新建过 / 收发过消息）的会话
        for (c in engine.recentConversations()) {
            seen.add(c.convId)
            val title = peersById[c.convId]?.name ?: c.title.ifEmpty { c.convId.take(8) }
            items.add(ConvItem(c.convId, title, c.text))
        }
        // 2) 附近发现、还没聊过的设备（被删除过的除外）
        for (p in engine.peers()) {
            if (p.deviceId in seen || p.deviceId in hidden) continue
            items.add(ConvItem(p.deviceId, p.name, "（暂无消息）"))
        }
        _convs.value = items
    }
}
