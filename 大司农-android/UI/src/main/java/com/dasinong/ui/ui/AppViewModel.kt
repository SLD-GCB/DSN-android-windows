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
import com.dasinong.backend.model.ChatMessage
import com.dasinong.backend.model.Peer
import com.dasinong.backend.model.Transfer
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

    private val _transfers = MutableStateFlow<Map<String, Transfer>>(emptyMap())
    val transfers: StateFlow<Map<String, Transfer>> = _transfers.asStateFlow()

    private val _currentConv = MutableStateFlow<String?>(null)
    val currentConv: StateFlow<String?> = _currentConv.asStateFlow()

    /** 一次性提示（直连成功/失败等），UI 弹完 toast 清掉。 */
    private val _notice = MutableStateFlow<String?>(null)
    val notice: StateFlow<String?> = _notice.asStateFlow()

    data class ConvItem(val peerId: String, val title: String, val preview: String)

    init {
        engine.bus.subscribe(::onEvent)
        refreshConvs()
        reloadTransfers()   // 回填历史传输记录（否则关掉重开就看不到）
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
                val key = event.transfer.file?.fileId ?: event.transfer.transferId
                _transfers.value = _transfers.value + (key to event.transfer)
            }
            is Event.TransferRemoved -> _transfers.value = _transfers.value - event.fileId
            is Event.ConversationRebound -> onConvRebound(event.oldId, event.newId)
            else -> Unit
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

    fun sendText(peerId: String, text: String) {
        if (text.isBlank()) return
        engine.sendText(peerId, text)
    }

    fun sendFiles(peerId: String, files: List<com.dasinong.backend.net.LocalFile>) {
        if (files.isEmpty()) return
        engine.sendFiles(peerId, files)
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

    // ------------------------------------------------------------ 隧道口令
    private val _passphrase = MutableStateFlow(engine.passphrase())
    val passphrase: StateFlow<String> = _passphrase.asStateFlow()

    private val _fingerprint = MutableStateFlow(engine.fingerprint())
    val fingerprint: StateFlow<String> = _fingerprint.asStateFlow()

    fun savePassphrase(value: String) {
        engine.setPassphrase(value)
        _passphrase.value = engine.passphrase()
        _fingerprint.value = engine.fingerprint()
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
        _messages.value = _messages.value + (msg.convId to (list + msg))
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
