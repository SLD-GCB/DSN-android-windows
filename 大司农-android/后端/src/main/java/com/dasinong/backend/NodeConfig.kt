package com.dasinong.backend

import android.content.Context
import java.io.File
import java.util.UUID

/** 运行时配置：设备标识、端口、数据目录。 */
data class NodeConfig(
    val deviceId: String,
    val deviceName: String,
    val deviceType: String = "android",
    val udpPort: Int = 37020,
    val tcpPort: Int = 37021,
    val dataDir: File,
) {
    val receiveDir: File get() = File(dataDir, "Downloads").apply { mkdirs() }
    val dbFile: File get() = File(dataDir, "dasinong.db")

    companion object {
        fun load(ctx: Context): NodeConfig {
            val dir = File(ctx.filesDir, "dasinong").apply { mkdirs() }
            return NodeConfig(
                deviceId = loadOrCreateDeviceId(dir),
                deviceName = android.os.Build.MODEL ?: "Android",
                dataDir = dir,
            )
        }

        private fun loadOrCreateDeviceId(dir: File): String {
            val f = File(dir, ".device_id")
            if (f.exists()) {
                val v = f.readText().trim()
                if (v.isNotEmpty()) return v
            }
            val v = UUID.randomUUID().toString()
            f.writeText(v)
            return v
        }
    }
}
