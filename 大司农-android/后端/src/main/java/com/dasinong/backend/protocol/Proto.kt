package com.dasinong.backend.protocol

import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.charset.StandardCharsets

/**
 * 协议常量与帧编解码（与 protocol/PROTOCOL.md、Windows 端 dasinong_core.protocol 严格对齐）。
 *
 * 帧格式：u32 len(BE) + u8 type + body，len = body.size + 1
 */
object Proto {
    // v4：每设备独立身份 + 配对确认（破坏性改动 —— 旧版客户端连不上）
    const val VERSION = 5
    const val MIN_VERSION = 4

    const val UDP_PORT = 37020
    const val TCP_PORT = 37021
    const val MULTICAST_GROUP = "224.0.0.167"

    const val HELLO_INTERVAL_MS = 3_000L
    const val HELLO_INTERVAL_BG_MS = 5_000L
    const val PEER_TIMEOUT_MS = 10_000L
    const val PING_INTERVAL_MS = 3_000L
    const val PING_TIMEOUT_MS = 6_000L
    const val CONNECT_TIMEOUT_MS = 5_000
    const val GATEWAY_PROBE_INTERVAL_MS = 5_000L

    const val CHUNK_SIZE = 65_536
    const val MAX_FRAME = 1_048_576
    const val DEFAULT_PARALLEL = 3
    const val MAX_PARALLEL = 8
    const val WINDOW_BYTES = 4 * 1024 * 1024

    val CANDIDATE_GATEWAYS = listOf(
        "192.168.43.1", "192.168.42.1", "192.168.137.1", "172.20.10.1",
    )
    val HOTSPOT_BROADCASTS = listOf(
        "192.168.43.255", "192.168.42.255", "192.168.137.255", "172.20.10.255",
    )
}

object FrameType {
    const val JSON = 0x01
    // ---- 隧道认证（TLS 之上、业务首帧之前；见 net/Tunnel.kt）----
    const val AUTH_HELLO = 0x03
    const val AUTH_REQ = 0x04
    const val AUTH_RES = 0x05
    const val AUTH_ERR = 0x06
    const val DATA_HELLO = 0x02
    const val CHUNK = 0x10
    const val CHUNK_ACK = 0x11
    const val DATA_DONE = 0x12
    const val DATA_ERROR = 0x7F
}

class ProtocolException(message: String) : Exception(message)

object FrameCodec {

    class Frame(val type: Int, val body: ByteArray)

    fun packFrame(type: Int, body: ByteArray): ByteArray {
        if (body.size + 1 > Proto.MAX_FRAME) {
            throw ProtocolException("frame too large: ${body.size + 1}")
        }
        val buf = ByteBuffer.allocate(5 + body.size).order(ByteOrder.BIG_ENDIAN)
        buf.putInt(body.size + 1)
        buf.put(type.toByte())
        buf.put(body)
        return buf.array()
    }

    fun readExact(input: InputStream, n: Int): ByteArray? {
        val out = ByteArray(n)
        var read = 0
        while (read < n) {
            val r = input.read(out, read, n - read)
            if (r < 0) {
                if (read == 0) return null
                throw ProtocolException("connection closed mid-frame")
            }
            read += r
        }
        return out
    }

    /** 读取一帧；连接正常结束返回 null，长度非法抛 ProtocolException。 */
    fun readFrame(input: InputStream): Frame? {
        val head = readExact(input, 5) ?: return null
        val bb = ByteBuffer.wrap(head).order(ByteOrder.BIG_ENDIAN)
        val len = bb.int
        val type = bb.get().toInt() and 0xFF
        if (len < 1 || len > Proto.MAX_FRAME) throw ProtocolException("bad frame length: $len")
        val bodyLen = len - 1
        if (bodyLen == 0) return Frame(type, ByteArray(0))
        val body = readExact(input, bodyLen) ?: throw ProtocolException("eof mid-frame body")
        return Frame(type, body)
    }

    fun writeFrame(output: OutputStream, type: Int, body: ByteArray) {
        output.write(packFrame(type, body))
        output.flush()
    }

    // ------------------------------------------------------------ 长度前缀字符串

    private fun putString(buf: ByteBuffer, s: String) {
        val raw = s.toByteArray(StandardCharsets.UTF_8)
        buf.putShort(raw.size.toShort())
        buf.put(raw)
    }

    private fun getString(bb: ByteBuffer): String {
        if (bb.remaining() < 2) throw ProtocolException("truncated string")
        val n = bb.short.toInt() and 0xFFFF
        if (bb.remaining() < n) throw ProtocolException("truncated string body")
        val raw = ByteArray(n)
        bb.get(raw)
        return String(raw, StandardCharsets.UTF_8)
    }

    // ------------------------------------------------------------ 数据通道结构

    fun packDataHello(deviceId: String, transferId: String, fileId: String): ByteArray {
        val buf = ByteBuffer.allocate(2 * 3 + (deviceId.length + transferId.length + fileId.length) * 4)
            .order(ByteOrder.BIG_ENDIAN)
        putString(buf, deviceId)
        putString(buf, transferId)
        putString(buf, fileId)
        val out = ByteArray(buf.position())
        buf.flip()
        buf.get(out)
        return out
    }

    fun parseDataHello(body: ByteArray): Triple<String, String, String> {
        val bb = ByteBuffer.wrap(body).order(ByteOrder.BIG_ENDIAN)
        val device = getString(bb)
        val transfer = getString(bb)
        val file = getString(bb)
        return Triple(device, transfer, file)
    }

    fun packChunk(offset: Long, payload: ByteArray): ByteArray {
        val buf = ByteBuffer.allocate(8 + payload.size).order(ByteOrder.BIG_ENDIAN)
        buf.putLong(offset)
        buf.put(payload)
        return buf.array()
    }

    fun parseChunk(body: ByteArray): Pair<Long, ByteArray> {
        if (body.size < 8) throw ProtocolException("truncated CHUNK")
        val bb = ByteBuffer.wrap(body).order(ByteOrder.BIG_ENDIAN)
        val offset = bb.long
        val payload = ByteArray(body.size - 8)
        bb.get(payload)
        return offset to payload
    }

    fun packChunkAck(cumulativeOffset: Long): ByteArray =
        ByteBuffer.allocate(8).order(ByteOrder.BIG_ENDIAN).putLong(cumulativeOffset).array()

    fun packDataDone(totalSize: Long, sha256: ByteArray): ByteArray {
        if (sha256.size != 32) throw ProtocolException("sha256 must be 32 bytes")
        val buf = ByteBuffer.allocate(40).order(ByteOrder.BIG_ENDIAN)
        buf.putLong(totalSize)
        buf.put(sha256)
        return buf.array()
    }

    fun parseDataDone(body: ByteArray): Pair<Long, ByteArray> {
        if (body.size < 40) throw ProtocolException("truncated DATA_DONE")
        val bb = ByteBuffer.wrap(body).order(ByteOrder.BIG_ENDIAN)
        val total = bb.long
        val sha = ByteArray(32)
        bb.get(sha)
        return total to sha
    }

    fun packDataError(code: Int, message: String): ByteArray {
        val raw = message.toByteArray(StandardCharsets.UTF_8)
        val buf = ByteBuffer.allocate(2 + raw.size).order(ByteOrder.BIG_ENDIAN)
        buf.putShort(code.toShort())
        buf.put(raw)
        return buf.array()
    }

    fun parseDataError(body: ByteArray): Pair<Int, String> {
        if (body.size < 2) throw ProtocolException("truncated DATA_ERROR")
        val bb = ByteBuffer.wrap(body).order(ByteOrder.BIG_ENDIAN)
        val code = bb.short.toInt() and 0xFFFF
        val raw = ByteArray(body.size - 2)
        bb.get(raw)
        return code to String(raw, StandardCharsets.UTF_8)
    }
}
