package com.dasinong.backend.net

import com.dasinong.backend.util.Ranges
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** .partmeta 元数据：断点续传判定依据是 sha256 + size，而非文件名。 */
data class PartMeta(
    val transferId: String,
    val fileId: String,
    val name: String,
    val size: Long,
    val sha256: String,
    val chunkSize: Int,
    val totalChunks: Int,
    val received: List<LongRange>,
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("transfer_id", transferId)
        put("file_id", fileId)
        put("name", name)
        put("size", size)
        put("sha256", sha256)
        put("chunk_size", chunkSize)
        put("total_chunks", totalChunks)
        put("received", Ranges.toJsonArray(received))
        put("updated_at", System.currentTimeMillis() / 1000)
    }

    companion object {
        fun fromJson(o: JSONObject): PartMeta? = try {
            PartMeta(
                transferId = o.optString("transfer_id", ""),
                fileId = o.optString("file_id", ""),
                name = o.optString("name", ""),
                size = o.optLong("size", 0L),
                sha256 = o.optString("sha256", ""),
                chunkSize = o.optInt("chunk_size", 65536),
                totalChunks = o.optInt("total_chunks", 0),
                received = Ranges.fromJsonArray(o.optJSONArray("received")),
            )
        } catch (e: Exception) {
            null
        }
    }
}

object ResumeStore {

    fun metaFile(part: File): File = File(part.parentFile, part.name + ".partmeta")

    fun load(part: File): PartMeta? {
        val f = metaFile(part)
        if (!f.exists()) return null
        return try {
            PartMeta.fromJson(JSONObject(f.readText()))
        } catch (e: Exception) {
            null
        }
    }

    fun save(part: File, meta: PartMeta) {
        try {
            val tmp = File(part.parentFile, part.name + ".partmeta.tmp")
            tmp.writeText(meta.toJson().toString())
            if (!tmp.renameTo(metaFile(part))) {
                metaFile(part).writeText(tmp.readText())
                tmp.delete()
            }
        } catch (_: Exception) {
        }
    }

    fun clear(part: File) {
        try {
            metaFile(part).delete()
        } catch (_: Exception) {
        }
    }
}
