package com.dasinong.ui.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Email
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Place
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.IconButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.dasinong.backend.model.ChatMessage
import com.dasinong.backend.model.MsgKind
import com.dasinong.backend.model.Peer
import com.dasinong.backend.model.Transfer
import com.dasinong.backend.model.TransferState
import com.dasinong.backend.protocol.Proto
import com.dasinong.ui.ui.theme.DasinongTheme
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileInputStream

private val TABS = listOf("附近", "会话", "传输", "设置")

/** 底部导航图标（只挑 material-icons-core 里确实有的那几个）。 */
private val TAB_ICONS: List<ImageVector> = listOf(
    Icons.Filled.Place, Icons.Filled.Email, Icons.Filled.List, Icons.Filled.Settings,
)

@Composable
fun RootScreen(vm: AppViewModel = viewModel()) {
    var tab by rememberSaveable { mutableIntStateOf(0) }
    val context = LocalContext.current
    val notice by vm.notice.collectAsStateWithLifecycle()
    // 直连成功/失败等一次性提示
    LaunchedEffect(notice) {
        notice?.let {
            toast(context, it)
            vm.clearNotice()
        }
    }
    DasinongTheme {
        Scaffold(
            bottomBar = {
                NavigationBar {
                    TABS.forEachIndexed { index, label ->
                        NavigationBarItem(
                            selected = tab == index,
                            onClick = { tab = index },
                            icon = { Icon(TAB_ICONS[index], contentDescription = label) },
                            label = { Text(label) },
                        )
                    }
                }
            },
        ) { padding ->
            Box(Modifier.fillMaxSize().padding(padding)) {
                when (tab) {
                    0 -> NearbyScreen(vm, onOpenChat = { vm.openConv(it); tab = 1 })
                    1 -> ChatScreen(vm)
                    2 -> TransferScreen(vm)
                    else -> SettingsScreen(vm)
                }
            }
        }
        // 配对确认：任何页面都可能弹（后端在等这个回答）
        val pairReq by vm.pairRequest.collectAsStateWithLifecycle()
        pairReq?.let { req -> PairingDialog(req, onAnswer = { vm.answerPairing(req.peerId, it) }) }
    }
}

// ================================================================ 附近
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun NearbyScreen(vm: AppViewModel, onOpenChat: (String) -> Unit) {
    var refreshing by remember { mutableStateOf(false) }
    val refreshScope = rememberCoroutineScope()
    val peers by vm.peers.collectAsStateWithLifecycle()
    val context = LocalContext.current
    var target by remember { mutableStateOf<String?>(null) }
    var appTarget by remember { mutableStateOf<String?>(null) }   // 分享应用：目标设备

    // 连接建立/断开不产生事件，靠轮询刷新「已连接/未连接」
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(2000)
            tick++
        }
    }

    val filePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments(),
    ) { uris ->
        val t = target ?: return@rememberLauncherForActivityResult
        vm.sendFiles(t, uris.mapNotNull { UriFiles.fromUri(context, it) })
    }
    val imagePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.PickMultipleVisualMedia(),
    ) { uris ->
        val t = target ?: return@rememberLauncherForActivityResult
        vm.sendFiles(t, uris.mapNotNull { UriFiles.fromUri(context, it) })
    }
    val folderPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree(),
    ) { uri ->
        val t = target ?: return@rememberLauncherForActivityResult
        if (uri != null) vm.sendFiles(t, UriFiles.fromTree(context, uri))
    }

    val keepAliveOn by vm.keepAlive.collectAsStateWithLifecycle()

    PullToRefreshBox(
        isRefreshing = refreshing,
        onRefresh = {
            refreshing = true
            vm.refreshAll()
            refreshScope.launch { delay(500); refreshing = false }
        },
    ) {
    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Text(
            "同一局域网（含手机热点）下自动发现设备",
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(bottom = 8.dp),
        )
        if (peers.isEmpty()) {
            Text("正在发现设备…（热点下若长时间无结果，可在「设置」里手动添加对方 IP）")
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(peers, key = { it.deviceId }) { peer ->
                val connected = remember(tick) { vm.isConnected(peer.deviceId) }
                Card(Modifier.fillMaxWidth().clickable { onOpenChat(peer.deviceId) }) {
                    Column(Modifier.padding(12.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(
                                "${if (connected) "●" else "○"} ${peer.name}",
                                fontWeight = FontWeight.Bold,
                                modifier = Modifier.weight(1f),
                            )
                            // 胶囊「保持长连接」滑块（设备名那一行最右边）
                            Surface(
                                shape = RoundedCornerShape(50),
                                color = if (keepAliveOn) MaterialTheme.colorScheme.primaryContainer
                                else MaterialTheme.colorScheme.surfaceVariant,
                            ) {
                                Row(
                                    Modifier.padding(start = 10.dp, end = 2.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Text("保持", style = MaterialTheme.typography.labelSmall)
                                    Switch(
                                        checked = keepAliveOn,
                                        onCheckedChange = { vm.setKeepAliveEnabled(it) },
                                    )
                                }
                            }
                        }
                        Text(
                            "${if (peer.deviceType == "android") "手机" else "电脑"} · ${peer.ip} · " +
                                (if (connected) "已连接" else "未连接") +
                                (if (peer.incompatible) " · ⚠ 版本过旧"
                                else if (vm.isPaired(peer.deviceId)) " · 已配对" else " · 未配对"),
                            style = MaterialTheme.typography.bodySmall,
                        )
                        // 按钮分两行排列（一行四个会溢出被裁掉）
                        Row(
                            Modifier.fillMaxWidth().padding(top = 8.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            OutlinedButton(
                                onClick = { onOpenChat(peer.deviceId) },
                                modifier = Modifier.weight(1f),
                            ) { Text("会话") }
                            Button(
                                onClick = {
                                    target = peer.deviceId
                                    filePicker.launch(arrayOf("*/*"))
                                },
                                modifier = Modifier.weight(1f),
                            ) { Text("文件") }
                        }
                        Row(
                            Modifier.fillMaxWidth().padding(top = 8.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            OutlinedButton(
                                onClick = {
                                    target = peer.deviceId
                                    imagePicker.launch(
                                        PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly),
                                    )
                                },
                                modifier = Modifier.weight(1f),
                            ) { Text("图片") }
                            OutlinedButton(
                                onClick = {
                                    target = peer.deviceId
                                    folderPicker.launch(null)
                                },
                                modifier = Modifier.weight(1f),
                            ) { Text("文件夹") }
                        }
                        // 分享应用：把本机已安装的应用（APK）发给这台设备
                        OutlinedButton(
                            onClick = { appTarget = peer.deviceId },
                            modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                        ) { Text("分享应用") }
                        // 未配对时：手动发起配对（配对失败后不会再自动重试，需要在这里点一次）
                        if (!peer.incompatible && !vm.isPaired(peer.deviceId)) {
                            OutlinedButton(
                                onClick = { vm.pairWith(peer.deviceId) },
                                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                            ) { Text("配对") }
                        }
                        // 手动连接（退出再进软件后也能手动连上）
                        if (!peer.incompatible && !vm.isConnected(peer.deviceId)) {
                            OutlinedButton(
                                onClick = { vm.connectPeer(peer.deviceId) },
                                modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                            ) { Text("连接") }
                        }
                    }
                }
            }
        }
    }
    }

    val at = appTarget
    if (at != null) {
        AppPickerDialog(
            onDismiss = { appTarget = null },
            onSend = { lf ->
                vm.sendFiles(at, listOf(lf))
                toast(context, "已发送「${lf.name}」")
                appTarget = null
            },
        )
    }
}

// ================================================================ 会话
@Composable
private fun ChatScreen(vm: AppViewModel) {
    val current by vm.currentConv.collectAsStateWithLifecycle()
    val convId = current
    if (convId == null) ConversationList(vm) else ConversationView(vm, convId)
}

/** 会话列表：点一行就进会话，进去就能直接发（和电脑端一样）。 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun ConversationList(vm: AppViewModel) {
    val convs by vm.convs.collectAsStateWithLifecycle()
    val peers by vm.peers.collectAsStateWithLifecycle()
    var tick by remember { mutableStateOf(0) }
    var showNew by remember { mutableStateOf(false) }
    var pendingDelete by remember { mutableStateOf<AppViewModel.ConvItem?>(null) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(2000)
            tick++
        }
    }
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp, top = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "会话",
                style = MaterialTheme.typography.titleMedium,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = { showNew = true }) { Text("新建会话") }
        }
        if (convs.isEmpty()) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    "还没有会话\n点右上角「新建会话」，或去「附近」页点设备卡片\n（长按会话可删除）",
                    textAlign = TextAlign.Center,
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        } else {
            LazyColumn(Modifier.fillMaxSize()) {
                items(convs, key = { it.peerId }) { c ->
                    val online = remember(tick) { vm.isConnectedTo(c.peerId) }
                    Card(
                        Modifier
                            .fillMaxWidth()
                            .padding(horizontal = 12.dp, vertical = 4.dp)
                            .combinedClickable(
                                onClick = { vm.openConv(c.peerId) },
                                onLongClick = { pendingDelete = c },
                            ),
                    ) {
                        Row(
                            Modifier.fillMaxWidth().padding(12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(c.title, fontWeight = FontWeight.Bold)
                                Text(
                                    c.preview.ifEmpty { "（暂无消息）" },
                                    style = MaterialTheme.typography.bodySmall,
                                    maxLines = 1,
                                    overflow = TextOverflow.Ellipsis,
                                )
                            }
                            Text(
                                if (online) "● 已连接" else "○ 未连接",
                                style = MaterialTheme.typography.labelSmall,
                                color = if (online) MaterialTheme.colorScheme.tertiary
                                else MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                }
            }
        }
    }

    if (showNew) {
        NewConversationDialog(
            peers = peers,
            onDismiss = { showNew = false },
            onPick = { pid, title -> vm.createConv(pid, title); showNew = false },
            onManualIp = { ip -> vm.createConvFromIp(ip); showNew = false },
        )
    }

    pendingDelete?.let { c ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("删除会话") },
            text = {
                Text(
                    "删除与「${c.title}」的会话？\n\n聊天记录会一并清空，该设备即使在线也不再自动出现" +
                        "（已收到的文件仍保留在传输列表里）。",
                )
            },
            confirmButton = {
                TextButton(onClick = { vm.deleteConv(c.peerId); pendingDelete = null }) { Text("删除") }
            },
            dismissButton = { TextButton(onClick = { pendingDelete = null }) { Text("取消") } },
        )
    }
}

/** 新建会话：从已发现的设备里挑一个，或手动输入 IP。 */
@Composable
private fun NewConversationDialog(
    peers: List<Peer>,
    onDismiss: () -> Unit,
    onPick: (String, String) -> Unit,
    onManualIp: (String) -> Unit,
) {
    var manualIp by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("新建会话") },
        text = {
            Column {
                if (peers.isEmpty()) {
                    Text(
                        "暂未发现设备；可在下方手动输入对方 IP（同一局域网/热点下通常会几秒内自动发现）。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                LazyColumn(Modifier.heightIn(max = 240.dp)) {
                    items(peers, key = { it.deviceId }) { p ->
                        Row(
                            Modifier
                                .fillMaxWidth()
                                .clickable { onPick(p.deviceId, p.name) }
                                .padding(vertical = 10.dp),
                        ) {
                            Text("${p.name}  ·  ${p.ip}")
                        }
                    }
                }
                OutlinedTextField(
                    value = manualIp,
                    onValueChange = { manualIp = it },
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                    label = { Text("手动输入 IP") },
                    singleLine = true,
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onManualIp(manualIp.trim()) },
                enabled = manualIp.isNotBlank(),
            ) { Text("新建") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

/** 单个会话：顶部返回 + 消息流 + 输入区（输入框整行胶囊状，按钮在下面）。 */
@Composable
private fun ConversationView(vm: AppViewModel, convId: String) {
    val convs by vm.convs.collectAsStateWithLifecycle()
    val messages by vm.messages.collectAsStateWithLifecycle()
    val context = LocalContext.current
    var input by remember { mutableStateOf("") }
    var confirmClear by remember { mutableStateOf(false) }
    var pendingMsg by remember { mutableStateOf<ChatMessage?>(null) }
    var showAppPicker by remember { mutableStateOf(false) }   // 分享应用
    val title = convs.firstOrNull { it.peerId == convId }?.title ?: convId.take(8)

    val filePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments(),
    ) { uris -> vm.sendFiles(convId, uris.mapNotNull { UriFiles.fromUri(context, it) }) }
    val imagePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.PickMultipleVisualMedia(),
    ) { uris -> vm.sendFiles(convId, uris.mapNotNull { UriFiles.fromUri(context, it) }) }
    // 传文件夹（整棵目录树）
    val folderPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree(),
    ) { uri -> if (uri != null) vm.sendFiles(convId, UriFiles.fromTree(context, uri)) }

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            IconButton(onClick = { vm.backToList() }) {
                Icon(Icons.Filled.ArrowBack, contentDescription = "返回")
            }
            Text(title, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
            // 一键清空本会话聊天记录（只删本机）
            TextButton(onClick = { confirmClear = true }) { Text("清空") }
        }

        if (vm.isManualConv(convId)) {
            Text(
                "正在探测该 IP 上的设备（同一局域网/热点下通常几秒内发现）。" +
                    "发现后会自动切换成该设备；此时发送的消息会在设备出现后自动发出。",
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
            )
        }

        LazyColumn(
            Modifier.weight(1f).fillMaxWidth().padding(horizontal = 8.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            items(messages[convId].orEmpty(), key = { it.msgId }) { m ->
                MessageBubble(
                    m,
                    // 这一条对应的传输任务（用来在气泡上显示进度）
                    transfer = vm.transfers.collectAsStateWithLifecycle().value.values.firstOrNull {
                        // 优先按 transferId（组）匹配：发送端的消息 fileId 是新生成的，和传输任务不同
                        (m.transferId != null && it.transferId == m.transferId) ||
                            (m.file != null && it.file?.fileId == m.file?.fileId)
                    },
                    onLongPress = { pendingMsg = m },
                )
            }
        }

        Column(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 8.dp)) {
            OutlinedTextField(
                value = input, onValueChange = { input = it },
                modifier = Modifier.fillMaxWidth(),
                shape = CircleShape,
                placeholder = { Text("输入消息") },
                maxLines = 4,
                // 发送按钮：输入框最右侧的圆形纸飞机（原先下面那排的「发送」按钮会被挤扁）
                trailingIcon = {
                    IconButton(
                        onClick = { vm.sendText(convId, input); input = "" },
                        enabled = input.isNotBlank(),
                        modifier = Modifier
                            .size(36.dp)
                            .background(MaterialTheme.colorScheme.primary, CircleShape),
                    ) {
                        Icon(
                            Icons.AutoMirrored.Filled.Send,
                            contentDescription = "发送",
                            tint = MaterialTheme.colorScheme.onPrimary,
                            modifier = Modifier.size(20.dp),
                        )
                    }
                },
            )
            // 发送按钮**分两排**（一排两个，不会挤）
            Row(
                Modifier.fillMaxWidth().padding(top = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedButton(
                    onClick = {
                        imagePicker.launch(
                            PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly),
                        )
                    },
                    modifier = Modifier.weight(1f),
                ) { Text("图片") }
                OutlinedButton(
                    onClick = { filePicker.launch(arrayOf("*/*")) },
                    modifier = Modifier.weight(1f),
                ) { Text("文件") }
            }
            Row(
                Modifier.fillMaxWidth().padding(top = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedButton(
                    onClick = { folderPicker.launch(null) },
                    modifier = Modifier.weight(1f),
                ) { Text("文件夹") }
                OutlinedButton(
                    onClick = { showAppPicker = true },
                    modifier = Modifier.weight(1f),
                ) { Text("应用") }
            }
        }
    }

    if (showAppPicker) {
        AppPickerDialog(
            onDismiss = { showAppPicker = false },
            onSend = { lf ->
                vm.sendFiles(convId, listOf(lf))
                toast(context, "已发送「${lf.name}」")
                showAppPicker = false
            },
        )
    }

    // 一键清空本会话聊天记录
    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("清空聊天") },
            text = {
                Text("清空与「$title」的全部聊天记录？\n\n只删本机记录，不影响对方；会话会保留。")
            },
            confirmButton = {
                TextButton(onClick = {
                    vm.clearConvMessages(convId)
                    confirmClear = false
                }) { Text("清空") }
            },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("取消") } },
        )
    }

    // 长按气泡 → 复制 / 删除这一条
    pendingMsg?.let { m ->
        AlertDialog(
            onDismissRequest = { pendingMsg = null },
            title = { Text("这条消息") },
            text = { Text("要做什么？（删除只删本机记录，不影响对方）") },
            confirmButton = {
                // 只有带文字的消息（文本/系统）才谈得上复制
                if (m.text.isNotBlank()) {
                    TextButton(onClick = {
                        copyText(context, m.text)
                        toast(context, "已复制")
                        pendingMsg = null
                    }) { Text("复制") }
                }
            },
            dismissButton = {
                Row {
                    TextButton(onClick = {
                        vm.deleteMessage(convId, m.msgId)
                        pendingMsg = null
                    }) { Text("删除") }
                    TextButton(onClick = { pendingMsg = null }) { Text("取消") }
                }
            },
        )
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun MessageBubble(msg: ChatMessage, transfer: Transfer?, onLongPress: () -> Unit) {
    val align = if (msg.outgoing) Alignment.CenterEnd else Alignment.CenterStart
    val bg = if (msg.outgoing) MaterialTheme.colorScheme.primaryContainer
    else MaterialTheme.colorScheme.surfaceVariant
    Box(Modifier.fillMaxWidth(), contentAlignment = align) {
        Column(
            Modifier
                .background(bg, RoundedCornerShape(10.dp))
                // 长按气泡 → 打开消息菜单（复制 / 删除）
                .combinedClickable(onClick = {}, onLongClick = onLongPress)
                .padding(horizontal = 10.dp, vertical = 6.dp),
        ) {
            when (msg.kind) {
                MsgKind.TEXT -> Text(msg.text)
                MsgKind.IMAGE -> {
                    val bmp = rememberThumbnail(msg.file?.localPath)
                    if (bmp != null) {
                        Image(
                            bitmap = bmp,
                            contentDescription = msg.file?.name ?: "图片",
                            modifier = Modifier
                                .widthIn(max = 220.dp)
                                .heightIn(max = 260.dp)
                                .clip(RoundedCornerShape(8.dp)),
                            contentScale = ContentScale.Fit,
                        )
                        Text(
                            msg.file?.name ?: "图片",
                            style = MaterialTheme.typography.labelSmall,
                            modifier = Modifier.padding(top = 4.dp),
                        )
                    } else {
                        Text("🖼 ${msg.file?.name ?: "图片"}")
                    }
                }
                MsgKind.FILE -> Text("📄 ${msg.file?.name ?: "文件"}")
                MsgKind.SYSTEM -> Text(msg.text, style = MaterialTheme.typography.bodySmall)
            }
            // 文件/图片：在气泡里**就地显示进度**（不用去传输页看）
            if (transfer != null &&
                (transfer.state == TransferState.ACTIVE || transfer.state == TransferState.PENDING)) {
                LinearProgressIndicator(
                    progress = { transfer.progress },
                    modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
                )
                Text(
                    "${(transfer.progress * 100).toInt()}%  " +
                        if (transfer.direction == "recv") "接收中" else "发送中",
                    style = MaterialTheme.typography.labelSmall,
                )
            } else if (transfer != null && transfer.state == TransferState.FAILED) {
                Text(
                    "传输失败", color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.labelSmall,
                )
            }
            if (msg.outgoing) {
                Text(msgStatusText(msg.status), style = MaterialTheme.typography.labelSmall)
            }
        }
    }
}

// ================================================================ 配对确认
/**
 * 配对弹窗：两端显示**同一个 6 位数字**，核对一致再点「配对」。
 *
 * ⚠ 刻意**不显示任何指纹**（用户硬要求）—— 短码是本次会话的临时值。
 */
@Composable
private fun PairingDialog(req: com.dasinong.backend.PairRequest, onAnswer: (Boolean) -> Unit) {
    AlertDialog(
        onDismissRequest = { onAnswer(false) },
        title = { Text("配对确认") },
        text = {
            Column {
                Text("设备「${req.name}」请求与你配对。")
                Spacer(Modifier.height(8.dp))
                Text("请核对两台设备屏幕上显示的数字是否完全相同：")
                Spacer(Modifier.height(6.dp))
                Text(
                    req.sas.chunked(3).joinToString(" "),
                    style = MaterialTheme.typography.headlineMedium,
                    fontWeight = FontWeight.Bold,
                    textAlign = TextAlign.Center,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    "一致 → 点「配对」；不一致说明有人在中间冒充，请点「取消」。",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        },
        confirmButton = { TextButton(onClick = { onAnswer(true) }) { Text("配对") } },
        dismissButton = { TextButton(onClick = { onAnswer(false) }) { Text("取消") } },
    )
}

// ================================================================ 传输
@Composable
private fun TransferScreen(vm: AppViewModel) {
    val transfers by vm.transfers.collectAsStateWithLifecycle()
    val context = LocalContext.current
    val list = transfers.values.toList()
    var pendingDelete by remember { mutableStateOf<Transfer?>(null) }
    var confirmClear by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("传输", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
            TextButton(onClick = { confirmClear = true }, enabled = list.isNotEmpty()) {
                Text("一键清空")
            }
        }
        if (list.isEmpty()) {
            Text("暂无传输任务")
            return@Column
        }
        Text(
            "点一下条目就能打开文件/图片；「删除」删单条，「一键清空」清空列表 —— 都会把已下载到本机的文件一并删掉",
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(bottom = 6.dp),
        )
        // 导出到用户自选路径（系统保存对话框）
        val exportTarget = remember { mutableStateOf<Transfer?>(null) }
        val exportLauncher = rememberLauncherForActivityResult(
            ActivityResultContracts.CreateDocument("*/*"),
        ) { uri ->
            val t = exportTarget.value
            exportTarget.value = null
            val src = t?.finalPath
            if (uri == null || src == null) return@rememberLauncherForActivityResult
            val ok = runCatching {
                context.contentResolver.openOutputStream(uri)?.use { out ->
                    java.io.File(src).inputStream().use { it.copyTo(out) }
                }
            }.isSuccess
            toast(context, if (ok) "已导出到所选位置" else "导出失败")
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(list, key = { it.file?.fileId ?: it.transferId }) { t ->
                TransferRow(
                    t = t,
                    speedText = vm.transferStat.collectAsStateWithLifecycle().value[
                        t.file?.fileId ?: t.transferId],
                    onCancel = { vm.cancelTransfer(t.transferId) },
                    onOpen = { openLocalFile(context, t.finalPath) },
                    onExport = {
                        exportTarget.value = t
                        exportLauncher.launch(t.file?.name ?: "da-sinong-export")
                    },
                    onDelete = { pendingDelete = t },
                )
            }
        }
    }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("清空传输列表") },
            text = {
                Text(
                    "清空所有传输记录？\n\n已下载到本机的文件会一并删除（不可恢复）；\n" +
                        "正在进行中的任务会被保留。",
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    vm.clearTransfers()
                    confirmClear = false
                }) { Text("清空") }
            },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("取消") } },
        )
    }

    pendingDelete?.let { t ->
        val name = t.file?.name ?: t.transferId
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("删除文件") },
            text = {
                Text(
                    "删除「$name」？\n\n会把已下载到本机的那个文件一并删掉（不可恢复），" +
                        "并从传输列表移除。",
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    val err = vm.deleteTransfer(t.file?.fileId ?: t.transferId)
                    if (err != null) toast(context, err)
                    pendingDelete = null
                }) { Text("删除") }
            },
            dismissButton = { TextButton(onClick = { pendingDelete = null }) { Text("取消") } },
        )
    }
}

@Composable
private fun TransferRow(
    t: Transfer,
    speedText: String?,
    onCancel: () -> Unit,
    onOpen: () -> Unit,
    onExport: () -> Unit,
    onDelete: () -> Unit,
) {
    val busy = t.state == TransferState.ACTIVE || t.state == TransferState.PENDING
    Card(Modifier.fillMaxWidth().clickable { onOpen() }) {
        Column(Modifier.padding(12.dp)) {
            Text(
                "${if (t.direction == "send") "↑" else "↓"} ${t.file?.name ?: t.transferId}",
                fontWeight = FontWeight.Bold,
            )
            Text(
                "${stateText(t.state)}  ${(t.progress * 100).toInt()}%" +
                    if (speedText != null) "  ·  $speedText" else "",
                style = MaterialTheme.typography.bodySmall,
            )
            LinearProgressIndicator(
                progress = { t.progress },
                modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
            )
            if (t.error != null) Text(t.error!!, color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall)
            // 按钮**分两排**排列（一排两个，不会挤在一起）
            if (t.finalPath != null) {
                Row(
                    Modifier.fillMaxWidth().padding(top = 6.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    OutlinedButton(onClick = onOpen, modifier = Modifier.weight(1f)) { Text("打开") }
                    // 导出：用户自选保存路径
                    OutlinedButton(onClick = onExport, modifier = Modifier.weight(1f)) { Text("导出") }
                }
            }
            Row(
                Modifier.fillMaxWidth().padding(top = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                if (busy || t.state == TransferState.PAUSED) {
                    OutlinedButton(onClick = onCancel, modifier = Modifier.weight(1f)) { Text("取消") }
                }
                // 进行中不给删（先取消），其余状态都可以自由删
                if (!busy) {
                    OutlinedButton(onClick = onDelete, modifier = Modifier.weight(1f)) { Text("删除") }
                }
            }
        }
    }
}

// ================================================================ 打开本地文件

/**
 * 用系统默认程序打开文件；`.apk` 走系统安装器（需允许「安装未知应用」）。
 *
 * 注意：从 Android 8（API 26）起，应用要拉起「安装 APK」流程**必须**声明
 * `REQUEST_INSTALL_PACKAGES`，否则系统会**静默忽略**（表现就是「点了没反应」）。
 * `.apks`/`.apkm`/`.xapk` 这类分包容器系统安装器不认，只能交给支持它们的工具（如 SAI）。
 */
private fun openLocalFile(context: Context, source: String?) {
    if (source.isNullOrEmpty()) {
        toast(context, "这个任务还没有本地文件")
        return
    }
    val uri = try {
        if (source.startsWith("content://")) {
            Uri.parse(source)
        } else {
            val file = File(source)
            if (!file.exists()) {
                toast(context, "文件不在了：$source")
                return
            }
            // 应用私有目录 → 必须经 FileProvider 授权，别的应用才读得到
            FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        }
    } catch (e: Exception) {
        toast(context, "打不开：${e.message ?: "共享文件失败"}")
        return
    }

    val ext = source.substringAfterLast('.', "").lowercase()
    val mime = when (ext) {
        // 分包容器：让 SAI 之类的工具接手（系统安装器处理不了）
        "apks", "apkm", "xapk" -> "*/*"
        else -> mimeOf(context, source)
    }
    try {
        context.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, mime)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    } catch (e: Exception) {
        val hint = when (ext) {
            "apk" -> "没有可用的安装器（请在系统设置里允许本应用「安装未知应用」）"
            "apks", "apkm", "xapk" -> "这是分包安装包，请用 SAI 等工具安装"
            else -> e.message ?: "没有能打开它的应用"
        }
        toast(context, "打不开：$hint")
    }
}

private fun mimeOf(context: Context, source: String): String {
    if (source.startsWith("content://")) {
        runCatching { context.contentResolver.getType(Uri.parse(source)) }.getOrNull()?.let { return it }
        return "*/*"
    }
    return when (source.substringAfterLast('.', "").lowercase()) {
        "apk" -> "application/vnd.android.package-archive"
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "gif" -> "image/gif"
        "webp" -> "image/webp"
        "bmp" -> "image/bmp"
        "pdf" -> "application/pdf"
        "txt", "md" -> "text/plain"
        "mp4" -> "video/mp4"
        "mp3" -> "audio/mpeg"
        "zip" -> "application/zip"
        else -> "*/*"
    }
}

private fun toast(context: Context, message: String) {
    runCatching { Toast.makeText(context, message, Toast.LENGTH_SHORT).show() }
}

/** 把文本放进系统剪贴板（供长按消息弹窗的「复制」用）。 */
private fun copyText(context: Context, text: String) {
    runCatching {
        val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("消息", text))
    }
}

/** 人类可读的文件大小。 */
private fun humanSize(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    val kb = bytes / 1024.0
    if (kb < 1024) return String.format(java.util.Locale.US, "%.1f KB", kb)
    val mb = kb / 1024.0
    if (mb < 1024) return String.format(java.util.Locale.US, "%.1f MB", mb)
    return String.format(java.util.Locale.US, "%.2f GB", mb / 1024.0)
}

// ================================================================ 分享应用
/**
 * 应用选择器：列出本机已安装的应用，选中后把它的 APK（分包应用则打成 `.apks`）交给
 * `onSend` 去发送。（列表读取 / 分包打包较慢，都放后台线程；带搜索框，应用多也好找。）
 */
@Composable
private fun AppPickerDialog(
    onDismiss: () -> Unit,
    onSend: (com.dasinong.backend.net.LocalFile) -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var apps by remember { mutableStateOf<List<InstalledApp>?>(null) }
    var query by remember { mutableStateOf("") }
    var preparing by remember { mutableStateOf<String?>(null) }   // 正在准备/打包的应用名
    LaunchedEffect(Unit) {
        apps = withContext(Dispatchers.IO) { InstalledApps.list(context) }
    }
    val filtered = remember(apps, query) {
        val all = apps ?: return@remember emptyList()
        if (query.isBlank()) all else all.filter { it.label.contains(query.trim(), ignoreCase = true) }
    }

    AlertDialog(
        onDismissRequest = { if (preparing == null) onDismiss() },
        title = { Text("分享应用") },
        text = {
            Column {
                OutlinedTextField(
                    value = query, onValueChange = { query = it },
                    label = { Text("搜索应用") }, singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                when {
                    apps == null -> Text("正在读取已安装应用…")
                    filtered.isEmpty() -> Text("没有匹配的应用")
                    else -> LazyColumn(Modifier.heightIn(max = 420.dp)) {
                        items(filtered, key = { it.packageName }) { app ->
                            Row(
                                Modifier.fillMaxWidth()
                                    .clickable(enabled = preparing == null) {
                                        preparing = app.label
                                        scope.launch {
                                            val lf = withContext(Dispatchers.IO) {
                                                InstalledApps.prepare(context, app)
                                            }
                                            preparing = null
                                            onSend(lf)
                                        }
                                    }
                                    .padding(vertical = 8.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                app.icon?.let {
                                    Icon(
                                        it, contentDescription = null,
                                        modifier = Modifier.size(36.dp).clip(RoundedCornerShape(8.dp)),
                                    )
                                    Spacer(Modifier.width(12.dp))
                                }
                                Column(Modifier.weight(1f)) {
                                    Text(app.label, maxLines = 1, overflow = TextOverflow.Ellipsis)
                                    Text(
                                        if (app.hasSplits) {
                                            "${app.packageName} · 分包 · ${humanSize(app.size)}"
                                        } else {
                                            "${app.packageName} · ${humanSize(app.size)}"
                                        },
                                        style = MaterialTheme.typography.bodySmall,
                                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                                    )
                                }
                            }
                        }
                    }
                }
                // 分包应用需要在发送前打包成 .apks，给个明确的进行中提示
                preparing?.let {
                    Spacer(Modifier.height(8.dp))
                    Text("正在准备「$it」…", style = MaterialTheme.typography.bodySmall)
                }
            }
        },
        confirmButton = {},
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

// ================================================================ 缩略图

/** 缩略图最长边（像素）。 */
private const val THUMB_MAX_PX = 512

/**
 * 从本地路径 / `content://` 解码一张缩略图（纯 Android API，不引第三方图片库）。
 * 先量尺寸算采样率再解码，避免大图 OOM。
 */
@Composable
private fun rememberThumbnail(source: String?): ImageBitmap? {
    val context = LocalContext.current
    var bitmap by remember(source) { mutableStateOf<ImageBitmap?>(null) }
    LaunchedEffect(source) {
        bitmap = null
        if (source.isNullOrEmpty()) return@LaunchedEffect
        bitmap = withContext(Dispatchers.IO) { decodeThumbnail(context, source) }
    }
    return bitmap
}

private fun decodeThumbnail(context: Context, source: String): ImageBitmap? = runCatching {
    fun open(): java.io.InputStream? = if (source.startsWith("content://")) {
        context.contentResolver.openInputStream(Uri.parse(source))
    } else {
        runCatching { FileInputStream(source) }.getOrNull()
    }

    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    open()?.use { BitmapFactory.decodeStream(it, null, bounds) }
    val w = bounds.outWidth
    val h = bounds.outHeight
    if (w <= 0 || h <= 0) return@runCatching null

    var sample = 1
    while (w / sample > THUMB_MAX_PX || h / sample > THUMB_MAX_PX) sample *= 2
    val opts = BitmapFactory.Options().apply { inSampleSize = sample }
    val bmp = open()?.use { BitmapFactory.decodeStream(it, null, opts) } ?: return@runCatching null
    bmp.asImageBitmap()
}.getOrNull()

private fun msgStatusText(status: String): String = when (status) {    "pending" -> "待发送（未连接，连上后自动发出）"
    "sent" -> "已发送"
    "delivered" -> "已送达"
    "received" -> "已接收"
    "failed" -> "发送失败"
    else -> status
}

private fun stateText(s: TransferState): String = when (s) {
    TransferState.PENDING -> "等待对方接受"
    TransferState.ACTIVE -> "传输中"
    TransferState.PAUSED -> "已暂停（可续传）"
    TransferState.COMPLETED -> "完成"
    TransferState.FAILED -> "失败"
    TransferState.REJECTED -> "被拒绝"
    TransferState.CANCELLED -> "已取消"
}

// ================================================================ 设置
@Composable
private fun SettingsScreen(vm: AppViewModel) {
    var ip by remember { mutableStateOf("") }
    var tick by remember { mutableStateOf(0) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(2000)
            tick++
        }
    }
    val errors = remember(tick) { vm.startErrors() }
    val peerCount = remember(tick) { vm.peersCount() }
    val diagText by vm.diagText.collectAsStateWithLifecycle()
    val diagRunning by vm.diagRunning.collectAsStateWithLifecycle()
    val paired by vm.paired.collectAsStateWithLifecycle()

    Column(
        Modifier.fillMaxSize().padding(16.dp).verticalScroll(rememberScrollState()),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        if (errors.isNotEmpty()) {
            errors.forEach {
                Text("⚠ $it", color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall)
            }
        }
        Text("设备名称：${vm.deviceName()}")
        Text("接收目录：${vm.receiveDir()}", style = MaterialTheme.typography.bodySmall)
        Text("已发现设备：$peerCount 台")
        Text("协议版本：v${Proto.VERSION}")

        Text("— 后台运行 —", style = MaterialTheme.typography.labelLarge)
        val context = LocalContext.current
        var autostart by remember { mutableStateOf(vm.autostart()) }
        val batteryOk = remember(tick) { vm.isIgnoringBatteryOptimizations(context) }
        val notifOk = remember(tick) { vm.notificationsAllowed(context) }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("开机自启动", modifier = Modifier.weight(1f))
            Switch(
                checked = autostart,
                onCheckedChange = { autostart = it; vm.setAutostart(it) },
            )
        }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("电池优化白名单", modifier = Modifier.weight(1f))
            OutlinedButton(onClick = { vm.requestIgnoreBatteryOptimization(context) }) {
                Text(if (batteryOk) "已加入" else "去加入")
            }
        }
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("通知权限", modifier = Modifier.weight(1f))
            OutlinedButton(onClick = { vm.openNotificationSettings(context) }) {
                Text(if (notifOk) "已允许" else "去允许")
            }
        }
        Text(
            "加入电池优化白名单后，锁屏/长时间后台也不容易被系统杀掉，能持续收消息与文件。",
            style = MaterialTheme.typography.bodySmall,
        )

        // 显示版本号：确认「手机上跑的到底是哪个包」——之前反复排查就吃了这个亏
        val versionName = remember {
            runCatching {
                context.packageManager.getPackageInfo(context.packageName, 0).versionName
            }.getOrNull() ?: "?"
        }
        Text("大司农 版本：$versionName（数据库版本 7）", style = MaterialTheme.typography.bodySmall)

        // 连接闲置多久后自动关闭（分钟）——「保持长连接」打开时无限期
        var ctText by remember { mutableStateOf(vm.connTimeoutMinutes().toString()) }
        OutlinedTextField(
            value = ctText,
            onValueChange = {
                ctText = it.filter { ch -> ch.isDigit() }.take(4)
                vm.setConnTimeoutMinutes(ctText.toIntOrNull() ?: 10)
            },
            label = { Text("连接闲置多少分钟后自动关闭（分钟）") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Text("空闲超过该时间会断开（省资源）；打开「保持长连接」则无限期保持，不会被关。",
            style = MaterialTheme.typography.bodySmall)

        // TCP 并发（1~8）：一次传输并发几条数据连接；配合接口池提速，填 1 = 单连接
        var ccText by remember { mutableStateOf(vm.concurrency().toString()) }
        OutlinedTextField(
            value = ccText,
            onValueChange = {
                ccText = it.filter { ch -> ch.isDigit() }.take(1)
                vm.setConcurrency(ccText.toIntOrNull() ?: 3)
            },
            label = { Text("TCP 并发（1~8 条数据连接）") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        Text("— 已配对设备 —", style = MaterialTheme.typography.labelLarge)
        if (paired.isEmpty()) {
            Text("还没有配对的设备。首次与某台设备连接时会弹出 6 位数字，两边核对一致即可配对。",
                style = MaterialTheme.typography.bodySmall)
        } else {
            paired.forEach { row ->
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(row.name ?: row.deviceId.take(8))
                        Text("已配对：对方身份已记住", style = MaterialTheme.typography.bodySmall)
                    }
                    OutlinedButton(onClick = { vm.unpair(row.deviceId) }) { Text("解绑") }
                }
            }
        }
        Text(
            "提示：加密**始终开启**（每设备独立身份），不需要设置口令；" +
                "想让它不再信任某台设备，点上面的「解绑」即可（下次连接要重新配对）。",
            style = MaterialTheme.typography.bodySmall,
        )
        Text(
            "提示：热点下若互相发现不了，请确认已连接同一热点，并允许本应用的通知/附近设备权限。",
            style = MaterialTheme.typography.bodySmall,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = ip, onValueChange = { ip = it },
                label = { Text("手动添加对方 IP") }, modifier = Modifier.weight(1f),
            )
            Button(onClick = {
                if (ip.isNotBlank()) {
                    vm.addManualIp(ip.trim())
                    vm.directConnect(ip.trim())     // 只发 UDP 探测在热点下没用，直接拨 TCP
                    ip = ""
                }
            }) { Text("直连") }
        }
        Button(onClick = { vm.runDiagnose() }, enabled = !diagRunning) {
            Text(if (diagRunning) "诊断中…" else "网络诊断（把结果发我）")
        }
    }

    val text = diagText
    if (text != null) {
        AlertDialog(
            onDismissRequest = { vm.clearDiag() },
            title = { Text("网络诊断") },
            text = {
                Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
                    Text(text, style = MaterialTheme.typography.bodySmall)
                }
            },
            confirmButton = { TextButton(onClick = { vm.clearDiag() }) { Text("关闭") } },
        )
    }
}
