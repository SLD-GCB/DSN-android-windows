package com.dasinong.ui.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.dasinong.ui.EngineHolder
import com.dasinong.ui.MainActivity
import com.dasinong.ui.R
import com.dasinong.ui.notify.Notifier

/**
 * 前台服务：承载 NodeEngine 的网络生命周期，规避 Doze / 后台限制。
 *
 * 稳态固定用 `connectedDevice` 类型，原因：
 * - Android 15+ **禁止**从 BOOT_COMPLETED 启动 `dataSync` 前台服务；
 * - `dataSync` 还有每天 6 小时的上限。
 * `connectedDevice` 两者都不受影响（本 App 本就是「与外部设备保持网络连接」）。
 */
class NodeService : Service() {

    private var notifier: Notifier? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForegroundCompat(buildNotification(getString(R.string.notif_title)))

        val engine = EngineHolder.getOrCreate(applicationContext)
        notifier = Notifier(applicationContext, engine) { text -> updateProgress(text) }
            .also { it.start() }

        if (!EngineHolder.started) {
            engine.start()
            EngineHolder.started = true
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        notifier?.stop()
        notifier = null
        EngineHolder.stopIfStarted()
        super.onDestroy()
    }

    /** 用传输进度刷新常驻通知；传 null 表示没有活动任务，回到空闲文案。 */
    private fun updateProgress(text: String?) {
        val body = text ?: getString(R.string.notif_title)
        runCatching {
            NotificationManagerCompat.from(this).notify(NOTIF_ID, buildNotification(body))
        }
    }

    private fun startForegroundCompat(n: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = getSystemService(NotificationManager::class.java)
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notif_channel_name),
                NotificationManager.IMPORTANCE_LOW,   // 常驻通知不响不弹
            )
            mgr.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification {
        val intent = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_dasinong)
            .setColor(0xFF1E4346.toInt())
            .setOngoing(true)
            .setSilent(true)
            .setContentIntent(intent)
            .build()
    }

    companion object {
        private const val CHANNEL_ID = Notifier.CHANNEL_FOREGROUND
        private const val NOTIF_ID = 1001
    }
}
