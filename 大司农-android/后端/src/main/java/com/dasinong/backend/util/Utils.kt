package com.dasinong.backend.util

import java.text.Normalizer

/** 字节区间（闭区间）运算，与 Windows 端 utils.py 对齐。 */
object Ranges {

    fun merge(ranges: List<LongRange>): List<LongRange> {
        if (ranges.isEmpty()) return emptyList()
        val sorted = ranges.filter { it.last >= it.first }.sortedBy { it.first }
        val out = ArrayList<LongRange>()
        var curStart = sorted[0].first
        var curEnd = sorted[0].last
        for (r in sorted.drop(1)) {
            if (r.first <= curEnd + 1) {
                if (r.last > curEnd) curEnd = r.last
            } else {
                out.add(curStart..curEnd)
                curStart = r.first
                curEnd = r.last
            }
        }
        out.add(curStart..curEnd)
        return out
    }

    fun totalLen(ranges: List<LongRange>): Long =
        ranges.sumOf { if (it.last >= it.first) it.last - it.first + 1 else 0L }

    /** [0,size) 中未被 ranges 覆盖的区间。 */
    fun complement(size: Long, ranges: List<LongRange>): List<LongRange> {
        if (size <= 0) return emptyList()
        val out = ArrayList<LongRange>()
        var cursor = 0L
        for (r in merge(ranges)) {
            if (r.first > cursor) {
                out.add(cursor until minOf(r.first, size))
            }
            cursor = maxOf(cursor, r.last + 1)
            if (cursor >= size) break
        }
        if (cursor < size) out.add(cursor until size)
        return out.filter { it.last >= it.first }
    }

    fun toJsonArray(ranges: List<LongRange>): org.json.JSONArray {
        val arr = org.json.JSONArray()
        for (r in ranges) arr.put(org.json.JSONArray().put(r.first).put(r.last))
        return arr
    }

    fun fromJsonArray(arr: org.json.JSONArray?): List<LongRange> {
        if (arr == null) return emptyList()
        val out = ArrayList<LongRange>()
        for (i in 0 until arr.length()) {
            val pair = arr.optJSONArray(i) ?: continue
            if (pair.length() >= 2) out.add(pair.optLong(0)..pair.optLong(1))
        }
        return out
    }
}

/** 路径清洗：归一化、替换非法字符、去 Windows 保留名与结尾点/空格、防路径穿越。 */
object Paths {

    private val ILLEGAL = Regex("[\\\\/:*?\"<>|\\u0000-\\u001f]")
    private val RESERVED = setOf(
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    )
    private val IMAGE_EXT = setOf(".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif")

    fun sanitizeComponent(name: String): String {
        var n = Normalizer.normalize(name, Normalizer.Form.NFC).trim().trim('.')
        n = ILLEGAL.replace(n, "_")
        if (n.isEmpty() || n == "." || n == "..") return "_"
        val stem = n.substringBefore('.').uppercase()
        if (stem in RESERVED) n = "_$n"
        if (n.length > 200) {
            val dot = n.lastIndexOf('.')
            n = if (dot > 0) n.substring(0, 180) + n.substring(dot) else n.substring(0, 200)
        }
        return n.ifEmpty { "_" }
    }

    /** 返回清理后的 POSIX 风格相对路径；拒绝 .. 穿越。 */
    fun sanitizeRelPath(rel: String): String {
        val parts = rel.replace('\\', '/').split('/')
            .filter { it.isNotEmpty() && it != "." && it != ".." }
            .map { sanitizeComponent(it) }
        return if (parts.isEmpty()) "unnamed" else parts.joinToString("/")
    }

    fun kindOf(name: String): String {
        val dot = name.lastIndexOf('.')
        if (dot < 0) return "file"
        return if (name.substring(dot).lowercase() in IMAGE_EXT) "image" else "file"
    }
}
