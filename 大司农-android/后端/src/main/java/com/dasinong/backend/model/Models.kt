package com.dasinong.backend.model

import org.json.JSONObject

enum class PeerState { DISCOVERED, CONNECTING, ONLINE, DEGRADED, RECONNECT, REMOVED }

enum class MsgKind { TEXT, IMAGE, FILE, SYSTEM }

enum class TransferState { PENDING, ACTIVE, PAUSED, COMPLETED, FAILED, REJECTED, CANCELLED }

data class Peer(
    val deviceId: String,
    var name: String,
    var ip: String,
    var tcpPort: Int = 37021,
    var udpPort: Int = 37020,
    var deviceType: String = "android",
    var proto: Int = 3,
    var encrypted: Boolean = false,
    var lastSeen: Long = System.currentTimeMillis(),
    var state: PeerState = PeerState.ONLINE,
)

data class FileEntry(
    val fileId: String,
    var name: String,
    var relPath: String,
    var rootName: String? = null,
    var size: Long = 0,
    var kind: String = "file",
    var contentId: String = "",
    var chunkSize: Int = 65536,
    var totalChunks: Int = 0,
    var preview: String? = null,
    var mtime: Double = 0.0,
    /** **仅本地 UI 用**：本机上该文件的路径/内容 Uri（发给对方时**不会**带上，避免泄露本地路径）。 */
    var localPath: String? = null,
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("file_id", fileId)
        put("name", name)
        put("rel_path", relPath)
        put("root_name", rootName ?: JSONObject.NULL)
        put("size", size)
        put("kind", kind)
        put("content_id", contentId)
        put("chunk_size", chunkSize)
        put("total_chunks", totalChunks)
        put("preview", preview ?: JSONObject.NULL)
        put("mtime", mtime)
        // ⚠ 刻意不带 localPath
    }

    companion object {
        fun fromJson(o: JSONObject): FileEntry = FileEntry(
            fileId = o.optString("file_id", ""),
            name = o.optString("name", "unnamed"),
            relPath = o.optString("rel_path", o.optString("name", "unnamed")),
            rootName = if (o.isNull("root_name")) null else o.optString("root_name"),
            size = o.optLong("size", 0L),
            kind = o.optString("kind", "file"),
            contentId = o.optString("content_id", ""),
            chunkSize = o.optInt("chunk_size", 65536),
            totalChunks = o.optInt("total_chunks", 0),
            preview = if (o.isNull("preview")) null else o.optString("preview"),
            mtime = o.optDouble("mtime", 0.0),
        )
    }
}

data class Transfer(
    val transferId: String,
    val peerId: String,
    var file: FileEntry?,
    var direction: String,
    var state: TransferState = TransferState.PENDING,
    var sent: Long = 0,
    var received: Long = 0,
    var receivedRanges: List<LongRange> = emptyList(),
    var finalPath: String? = null,
    var error: String? = null,
) {
    val total: Long get() = file?.size ?: 0L
    val progress: Float
        get() {
            val t = total
            if (t <= 0) return 1f
            val done = if (direction == "recv") received else sent
            return (done.toDouble() / t).coerceIn(0.0, 1.0).toFloat()
        }
}

data class ChatMessage(
    val msgId: String,
    val convId: String,
    val kind: MsgKind,
    var text: String = "",
    var ts: Long = System.currentTimeMillis(),
    var outgoing: Boolean = false,
    var status: String = "sent",
    var transferId: String? = null,
    var file: FileEntry? = null,
)
