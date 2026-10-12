package com.dasinong.ui.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat
import com.dasinong.backend.store.ChatDb

/**
 * 开机广播：只有用户在设置里打开过「开机自启动」才拉起前台服务（默认关闭）。
 *
 * 稳态前台服务类型是 `connectedDevice` —— Android 15+ 禁止从 BOOT_COMPLETED 启动
 * `dataSync` 类型的前台服务，用 connectedDevice 才不会抛 ForegroundServiceStartNotAllowedException。
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val enabled = runCatching {
            ChatDb(context.applicationContext).settingGet("autostart", "0")
        }.getOrNull() == "1"
        if (!enabled) return
        runCatching {
            ContextCompat.startForegroundService(
                context.applicationContext,
                Intent(context.applicationContext, NodeService::class.java),
            )
        }
    }
}
