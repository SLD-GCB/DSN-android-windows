package com.dasinong.backend.store

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.dasinong.backend.model.ChatMessage
import com.dasinong.backend.model.FileEntry
import com.dasinong.backend.model.MsgKind
import com.dasinong.backend.model.Transfer
import com.dasinong.backend.model.TransferState
import org.json.JSONObject

/** 会话/消息/传输的本地持久化（SQLite）。 */
/**
 * ⚠ **数据库版本号只能单调递增**：一旦降号，`SQLiteOpenHelper` 会走 `onDowngrade`，
 * 而它的默认实现是**直接抛异常** —— 表现为 App 一打开就闪退。
 * v4 曾把版本提到 3（paired 表），随后功能回滚但**版本号必须留在 3**。
 * v5（会话表）提到 4 —— 同样只许加不许减。
 */
class ChatDb(context: Context) :
    SQLiteOpenHelper(context.applicationContext, "dasinong.db", null, 4) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS setting (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """.trimIndent(),
        )
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS message (
                msg_id TEXT PRIMARY KEY,
                conv_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT,
                ts INTEGER NOT NULL,
                outgoing INTEGER NOT NULL,
                status TEXT,
                transfer_id TEXT,
                file_json TEXT
            )
            """.trimIndent(),
        )
        db.execSQL("CREATE INDEX IF NOT EXISTS idx_message_conv ON message(conv_id, ts)")
        db.execSQL(
            """
            CREATE TABLE IF NOT EXISTS transfer (
                file_id TEXT PRIMARY KEY,
                group_id TEXT,
                peer_id TEXT,
                direction TEXT,
                state TEXT,
                name TEXT,
                rel_path TEXT,
                size INTEGER,
                received INTEGER,
                final_path TEXT,
                error TEXT,
                updated_at INTEGER
            )
            """.trimIndent(),
        )
        db.execSQL("CREATE INDEX IF NOT EXISTS idx_transfer_group ON transfer(group_id)")
        db.execSQL(CREATE_CONVERSATION)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) {
            db.execSQL(
                """
                CREATE TABLE IF NOT EXISTS setting (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """.trimIndent(),
            )
        }
        // oldVersion == 2 → 3：v4 曾在此建 paired 表，功能已回滚，无需再建。
        if (oldVersion < 4) {
            db.execSQL(CREATE_CONVERSATION)
        }
    }

    companion object {
        /**
         * 会话表：让「会话」成为可持久化、可新建/可删除的实体。
         * 有消息的会话在写消息时自动登记；用户也能手动新建；删除 = 清空消息 + 打隐藏标记
         * （hidden=1 让它即使对端仍在线也不再被自动列出）。
         */
        private const val CREATE_CONVERSATION = """
            CREATE TABLE IF NOT EXISTS conversation (
                conv_id    TEXT PRIMARY KEY,
                title      TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                hidden     INTEGER NOT NULL DEFAULT 0
            )
        """
    }

    // ------------------------------------------------------------ 设置项
    fun settingGet(key: String, default: String? = null): String? =
        readableDatabase.query("setting", arrayOf("value"), "key=?", arrayOf(key), null, null, null)
            .use { c -> if (c.moveToFirst()) c.getString(0) else default }

    fun settingSet(key: String, value: String) {
        val cv = ContentValues().apply {
            put("key", key)
            put("value", value)
        }
        writableDatabase.insertWithOnConflict("setting", null, cv, SQLiteDatabase.CONFLICT_REPLACE)
    }

    // ------------------------------------------------------------ 消息
    fun addMessage(msg: ChatMessage) {
        val cv = ContentValues().apply {
            put("msg_id", msg.msgId)
            put("conv_id", msg.convId)
            put("kind", msg.kind.name.lowercase())
            put("text", msg.text)
            put("ts", msg.ts)
            put("outgoing", if (msg.outgoing) 1 else 0)
            put("status", msg.status)
            put("transfer_id", msg.transferId)
            // ⚠ local_path 只落**本地**库（重启后仍能显示图片缩略图），不进协议
            put(
                "file_json",
                msg.file?.let { f ->
                    f.toJson().apply { put("local_path", f.localPath ?: JSONObject.NULL) }.toString()
                },
            )
        }
        writableDatabase.insertWithOnConflict("message", null, cv, SQLiteDatabase.CONFLICT_REPLACE)
        // 有消息 = 会话存在：顺带登记进 conversation 表（取消隐藏），标题稍后由上层补齐
        ensureConversationRow(msg.convId)
    }

    fun updateMessageStatus(msgId: String, status: String) {
        val cv = ContentValues().apply { put("status", status) }
        writableDatabase.update("message", cv, "msg_id=?", arrayOf(msgId))
    }

    fun messagesFor(convId: String, limit: Int = 500): List<ChatMessage> {
        val out = ArrayList<ChatMessage>()
        readableDatabase.query(
            "message", null, "conv_id=?", arrayOf(convId), null, null, "ts ASC", limit.toString(),
        ).use { c ->
            while (c.moveToNext()) {
                val kindStr = c.getString(c.getColumnIndexOrThrow("kind"))
                val fileJson = c.getString(c.getColumnIndexOrThrow("file_json"))
                out.add(
                    ChatMessage(
                        msgId = c.getString(c.getColumnIndexOrThrow("msg_id")),
                        convId = c.getString(c.getColumnIndexOrThrow("conv_id")),
                        kind = runCatching { MsgKind.valueOf(kindStr.uppercase()) }.getOrDefault(MsgKind.TEXT),
                        text = c.getString(c.getColumnIndexOrThrow("text")) ?: "",
                        ts = c.getLong(c.getColumnIndexOrThrow("ts")),
                        outgoing = c.getInt(c.getColumnIndexOrThrow("outgoing")) == 1,
                        status = c.getString(c.getColumnIndexOrThrow("status")) ?: "sent",
                        transferId = c.getString(c.getColumnIndexOrThrow("transfer_id")),
                        file = fileJson?.let {
                            runCatching {
                                val obj = JSONObject(it)
                                FileEntry.fromJson(obj).apply {
                                    localPath = if (obj.isNull("local_path")) null else obj.optString("local_path")
                                }
                            }.getOrNull()
                        },
                    ),
                )
            }
        }
        return out
    }

    data class ConvSummary(val convId: String, val title: String, val text: String, val ts: Long)

    // ------------------------------------------------------------ 会话
    /**
     * 返回**可见**会话（hidden=0），带最后一条消息预览，供会话列表使用。
     *
     * 列表来源是 conversation 表而不是直接聚合 message：这样即使用户新建了一个
     * 「还没发过消息」的会话、或对端离线了，它依然留在列表里。
     */
    fun conversations(): List<ConvSummary> {
        val out = ArrayList<ConvSummary>()
        readableDatabase.rawQuery(
            """
            SELECT c.conv_id, c.title,
                   COALESCE((SELECT m.text FROM message m
                             WHERE m.conv_id = c.conv_id AND m.text IS NOT NULL AND m.text <> ''
                             ORDER BY m.ts DESC LIMIT 1), '') AS text,
                   COALESCE((SELECT MAX(m.ts) FROM message m WHERE m.conv_id = c.conv_id),
                            c.updated_at) AS ts
            FROM conversation c WHERE c.hidden = 0 ORDER BY ts DESC
            """.trimIndent(),
            null,
        ).use { c ->
            while (c.moveToNext()) {
                out.add(
                    ConvSummary(
                        c.getString(0), c.getString(1) ?: "", c.getString(2) ?: "", c.getLong(3),
                    ),
                )
            }
        }
        return out
    }

    /** 登记（或取消隐藏）一个会话。写消息 / 打开会话 / 手动新建都会走这里。 */
    fun ensureConversation(convId: String, title: String? = null) {
        val now = System.currentTimeMillis()
        val db = writableDatabase
        db.execSQL(
            "INSERT OR IGNORE INTO conversation(conv_id,title,created_at,updated_at,hidden)" +
                " VALUES(?,?,?,?,0)",
            arrayOf<Any?>(convId, title ?: "", now, now),
        )
        if (!title.isNullOrEmpty()) {
            db.execSQL(
                "UPDATE conversation SET title=?, updated_at=?, hidden=0 WHERE conv_id=?",
                arrayOf<Any?>(title, now, convId),
            )
        } else {
            db.execSQL(
                "UPDATE conversation SET updated_at=?, hidden=0 WHERE conv_id=?",
                arrayOf<Any?>(now, convId),
            )
        }
    }

    private fun ensureConversationRow(convId: String) = ensureConversation(convId, null)

    /** 删除会话：清空该会话的全部消息，并把会话标记为 hidden（防止被自动重新列出）。 */
    /** 清空某个会话的聊天记录（**不动会话本身**，会话仍留在列表里）。返回删掉条数。 */
    fun clearMessages(convId: String): Int =
        writableDatabase.delete("message", "conv_id=?", arrayOf(convId))

    /** 删掉一条消息（只删本机记录，不影响对方）。 */
    fun deleteMessage(msgId: String): Int =
        writableDatabase.delete("message", "msg_id=?", arrayOf(msgId))

    fun deleteConversation(convId: String) {
        val now = System.currentTimeMillis()
        val db = writableDatabase
        db.delete("message", "conv_id=?", arrayOf(convId))
        db.execSQL(
            "INSERT OR IGNORE INTO conversation(conv_id,title,created_at,updated_at,hidden)" +
                " VALUES(?,?,?,?,1)",
            arrayOf<Any?>(convId, "", now, now),
        )
        db.execSQL(
            "UPDATE conversation SET hidden=1, updated_at=? WHERE conv_id=?",
            arrayOf<Any?>(now, convId),
        )
    }

    /** 被删除（隐藏）的会话 id 集合，供 UI 过滤掉仍在线但不该出现的对端。 */
    fun hiddenConversationIds(): Set<String> {
        val out = HashSet<String>()
        readableDatabase.rawQuery("SELECT conv_id FROM conversation WHERE hidden=1", null).use { c ->
            while (c.moveToNext()) out.add(c.getString(0))
        }
        return out
    }

    fun hasConversation(convId: String): Boolean =
        readableDatabase.query(
            "conversation", arrayOf("conv_id"), "conv_id=?", arrayOf(convId), null, null, null,
        ).use { it.moveToFirst() }

    /**
     * 把「手动 IP 会话」升级成真实设备会话：消息整批迁移到 [newId]，旧的合成会话行删掉。
     * 用于用户输入 IP 后、设备被发现时无缝切到 deviceId。
     */
    fun rebindConversation(oldId: String, newId: String, title: String?) {
        if (oldId == newId) return
        val now = System.currentTimeMillis()
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.execSQL("UPDATE message SET conv_id=? WHERE conv_id=?", arrayOf<Any?>(newId, oldId))
            db.delete("conversation", "conv_id=?", arrayOf(oldId))
            db.execSQL(
                "INSERT OR IGNORE INTO conversation(conv_id,title,created_at,updated_at,hidden)" +
                    " VALUES(?,?,?,?,0)",
                arrayOf<Any?>(newId, title ?: "", now, now),
            )
            if (!title.isNullOrEmpty()) {
                db.execSQL(
                    "UPDATE conversation SET title=?, updated_at=?, hidden=0 WHERE conv_id=?",
                    arrayOf<Any?>(title, now, newId),
                )
            } else {
                db.execSQL(
                    "UPDATE conversation SET updated_at=?, hidden=0 WHERE conv_id=?",
                    arrayOf<Any?>(now, newId),
                )
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    // ------------------------------------------------------------ 传输
    fun upsertTransfer(t: Transfer) {
        val cv = ContentValues().apply {
            put("file_id", t.file?.fileId ?: t.transferId)
            put("group_id", t.transferId)
            put("peer_id", t.peerId)
            put("direction", t.direction)
            put("state", t.state.name.lowercase())
            put("name", t.file?.name ?: "")
            put("rel_path", t.file?.relPath ?: "")
            put("size", t.file?.size ?: 0L)
            put("received", if (t.direction == "recv") t.received else t.sent)
            put("final_path", t.finalPath)
            put("error", t.error)
            put("updated_at", System.currentTimeMillis())
        }
        writableDatabase.insertWithOnConflict("transfer", null, cv, SQLiteDatabase.CONFLICT_REPLACE)
    }

    data class TransferRow(
        val fileId: String, val groupId: String, val peerId: String, val direction: String,
        val state: String, val name: String, val size: Long, val received: Long,
        val finalPath: String?, val error: String?,
    )

    fun transfers(limit: Int = 300): List<TransferRow> {
        val out = ArrayList<TransferRow>()
        readableDatabase.query(
            "transfer", null, null, null, null, null, "updated_at DESC", limit.toString(),
        ).use { c ->
            while (c.moveToNext()) {
                out.add(
                    TransferRow(
                        c.getString(c.getColumnIndexOrThrow("file_id")),
                        c.getString(c.getColumnIndexOrThrow("group_id")) ?: "",
                        c.getString(c.getColumnIndexOrThrow("peer_id")) ?: "",
                        c.getString(c.getColumnIndexOrThrow("direction")) ?: "recv",
                        c.getString(c.getColumnIndexOrThrow("state")) ?: TransferState.PENDING.name.lowercase(),
                        c.getString(c.getColumnIndexOrThrow("name")) ?: "",
                        c.getLong(c.getColumnIndexOrThrow("size")),
                        c.getLong(c.getColumnIndexOrThrow("received")),
                        c.getString(c.getColumnIndexOrThrow("final_path")),
                        c.getString(c.getColumnIndexOrThrow("error")),
                    ),
                )
            }
        }
        return out
    }

    /** 按 file_id 取一条传输记录（删除时用来找本地路径）。 */
    fun transferRow(fileId: String): TransferRow? =
        readableDatabase.query(
            "transfer", null, "file_id=?", arrayOf(fileId), null, null, null,
        ).use { c ->
            if (!c.moveToFirst()) {
                null
            } else {
                TransferRow(
                    c.getString(c.getColumnIndexOrThrow("file_id")),
                    c.getString(c.getColumnIndexOrThrow("group_id")) ?: "",
                    c.getString(c.getColumnIndexOrThrow("peer_id")) ?: "",
                    c.getString(c.getColumnIndexOrThrow("direction")) ?: "recv",
                    c.getString(c.getColumnIndexOrThrow("state")) ?: TransferState.PENDING.name.lowercase(),
                    c.getString(c.getColumnIndexOrThrow("name")) ?: "",
                    c.getLong(c.getColumnIndexOrThrow("size")),
                    c.getLong(c.getColumnIndexOrThrow("received")),
                    c.getString(c.getColumnIndexOrThrow("final_path")),
                    c.getString(c.getColumnIndexOrThrow("error")),
                )
            }
        }

    fun deleteTransfer(fileId: String) {
        writableDatabase.delete("transfer", "file_id=?", arrayOf(fileId))
    }

    /** 批量删除传输记录，返回删掉的条数。 */
    fun deleteTransfers(fileIds: List<String>): Int {
        if (fileIds.isEmpty()) return 0
        val placeholders = fileIds.joinToString(",") { "?" }
        return writableDatabase.delete("transfer", "file_id IN ($placeholders)", fileIds.toTypedArray())
    }
}
