package com.dasinong.ui.notify

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.dasinong.backend.Event
import com.dasinong.backend.NodeEngine
import com.dasinong.backend.model.MsgKind
import com.dasinong.backend.model.TransferState
import com.dasinong.backend.notify.TransferNotifPolicy
import com.dasinong.ui.AppState
import com.dasinong.ui.MainActivity
import com.dasinong.ui.R

/**
 * 把后端事件翻译成系统通知。
 *
 * - 收到**文本消息** → 消息渠道（会弹；同会话共用一个通知 id，不堆叠）
 * - 传输**完成/失败** → 传输渠道（同一 (fileId,state) 只弹一次）
 * - 传输**进行中** → 只回调 [onProgress] 去刷新常驻通知，**不弹**
 *
 * 注意：收到的文件/图片**不在这里提示** —— `NodeEngine.onTransferForChat` 已经补了一条
 * FILE/IMAGE 消息，而完成事件那条也会通知；这里只处理 TEXT，避免双弹。
 */
class Notifier(
    private val context: Context,
    private val engine: NodeEngine,
    private val onProgress: (String?) -> Unit,
) {
    private val policy = TransferNotifPolicy()
    private val listener: (Event) -> Unit = { onEvent(it) }

    fun start() {
        createChannels()
        engine.bus.subscribe(listener)
    }

    fun stop() {
        engine.bus.unsubscribe(listener)
    }

    // ------------------------------------------------------------ 事件
    private fun onEvent(event: Event) {
        runCatching {
            when (event) {
                is Event.MessageEvent -> onMessage(event)
                is Event.TransferEvent -> onTransfer(event)
                else -> Unit
            }
        }
    }

    private fun onMessage(event: Event.MessageEvent) {
        val m = event.message
        if (m.outgoing || m.kind != MsgKind.TEXT || AppState.foreground) return
        post(
            id = conversationNotifId(m.convId),
            channel = CHANNEL_MESSAGES,
            title = context.getString(R.string.notif_from, peerName(m.convId)),
            body = m.text.ifBlank { "（空消息）" },
        )
    }

    private fun onTransfer(event: Event.TransferEvent) {
        val t = event.transfer
        val fid = t.file?.fileId ?: t.transferId
        val name = t.file?.name ?: fid
        val now = System.currentTimeMillis()
        when (t.state) {
            TransferState.ACTIVE -> {
                val pct = (t.progress * 100).toInt().coerceIn(0, 100)
                if (policy.shouldUpdateProgress(fid, pct, now)) {
                    val n = policy.activeCount
                    onProgress(
                        if (n > 1) {
                            context.getString(R.string.notif_transfer_progress_multi, n, pct, name)
                        } else {
                            context.getString(R.string.notif_transfer_progress, pct, name)
                        },
                    )
                }
            }

            TransferState.COMPLETED, TransferState.FAILED -> {
                val firstTime = policy.shouldNotifyTerminal(fid, t.state.name)
                if (!policy.hasActive) onProgress(null)   // 没有别的任务了 → 常驻通知回到空闲文案
                if (!firstTime || AppState.foreground) return
                val text = when {
                    t.state == TransferState.FAILED ->
                        context.getString(R.string.notif_transfer_failed, t.error ?: name)
                    t.direction == "recv" ->
                        context.getString(R.string.notif_transfer_done, name)
                    else ->
                        context.getString(R.string.notif_transfer_sent, name)
                }
                post(
                    id = transferNotifId(fid),
                    channel = CHANNEL_TRANSFERS,
                    title = text,
                    body = peerName(t.peerId),
                )
            }

            else -> {
                policy.onFinished(fid)
                if (!policy.hasActive) onProgress(null)
            }
        }
    }

    // ------------------------------------------------------------ 工具
    private fun peerName(peerId: String): String =
        engine.peers().firstOrNull { it.deviceId == peerId }?.name ?: peerId.take(8)

    /** 同一条会话共用一个通知 id，后来的消息覆盖前面的，不会堆一屏。 */
    private fun conversationNotifId(convId: String): Int =
        2000 + (convId.hashCode() and 0x7fffffff) % 1000

    /** 每个文件单独一个 id：多设备同时传完时，几条完成通知不会互相覆盖。 */
    private fun transferNotifId(fileId: String): Int =
        TRANSFER_NOTIF_ID + (fileId.hashCode() and 0x7fffffff) % 800

    private fun post(id: Int, channel: String, title: String, body: String) {
        val n = NotificationCompat.Builder(context, channel)
            .setSmallIcon(R.drawable.ic_stat_dasinong)
            .setColor(0xFF1E4346.toInt())
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setContentIntent(contentIntent())
            .build()
        // 没有通知权限时 notify 会静默失败，不会崩
        runCatching { NotificationManagerCompat.from(context).notify(id, n) }
    }

    private fun contentIntent(): PendingIntent = PendingIntent.getActivity(
        context,
        0,
        Intent(context, MainActivity::class.java),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
    )

    private fun createChannels() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val mgr = context.getSystemService(NotificationManager::class.java) ?: return
        // 前台渠道由 NodeService 建
        mgr.createNotificationChannel(
            NotificationChannel(
                CHANNEL_MESSAGES,
                context.getString(R.string.notif_channel_messages),
                NotificationManager.IMPORTANCE_HIGH,
            ),
        )
        mgr.createNotificationChannel(
            NotificationChannel(
                CHANNEL_TRANSFERS,
                context.getString(R.string.notif_channel_transfers),
                NotificationManager.IMPORTANCE_DEFAULT,
            ),
        )
    }

    companion object {
        const val CHANNEL_FOREGROUND = "dasinong_foreground"
        const val CHANNEL_MESSAGES = "dasinong_messages"
        const val CHANNEL_TRANSFERS = "dasinong_transfers"

        /** 传输完成/失败通知 id 的基数（每个文件再按 fileId 偏移，避免互相覆盖）。 */
        const val TRANSFER_NOTIF_ID = 1002
    }
}
