package com.dasinong.backend

import com.dasinong.backend.protocol.FrameCodec
import com.dasinong.backend.protocol.FrameType
import com.dasinong.backend.protocol.ProtocolException
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** 帧编解码单测（纯 JVM，可在 Android Studio 里直接跑）。 */
class FrameCodecTest {

    @Test
    fun jsonFrameRoundTrip() {
        val out = ByteArrayOutputStream()
        FrameCodec.writeFrame(out, FrameType.JSON, "{\"a\":1}".toByteArray())
        val frame = FrameCodec.readFrame(ByteArrayInputStream(out.toByteArray()))
        assertEquals(FrameType.JSON, frame!!.type)
        assertEquals("{\"a\":1}", String(frame.body))
    }

    @Test
    fun unknownTypeIsReadableAndSkippable() {
        val out = ByteArrayOutputStream()
        FrameCodec.writeFrame(out, 0x55, "unknown".toByteArray())
        FrameCodec.writeFrame(out, FrameType.JSON, "{\"type\":\"hello\"}".toByteArray())
        val input = ByteArrayInputStream(out.toByteArray())

        val first = FrameCodec.readFrame(input)!!
        assertEquals(0x55, first.type)
        assertEquals("unknown", String(first.body))

        val second = FrameCodec.readFrame(input)!!
        assertEquals(FrameType.JSON, second.type)
        assertEquals("{\"type\":\"hello\"}", String(second.body))
    }

    @Test
    fun eofReturnsNull() {
        assertNull(FrameCodec.readFrame(ByteArrayInputStream(ByteArray(0))))
    }

    @Test
    fun badLengthRejected() {
        val head = ByteBuffer.allocate(5).order(ByteOrder.BIG_ENDIAN)
            .putInt(com.dasinong.backend.protocol.Proto.MAX_FRAME + 10)
            .put(1)
            .array()
        assertThrows(ProtocolException::class.java) {
            FrameCodec.readFrame(ByteArrayInputStream(head))
        }
    }

    @Test
    fun dataHelloRoundTrip() {
        val packed = FrameCodec.packDataHello("dev-1", "tr-1", "file-1")
        val (a, b, c) = FrameCodec.parseDataHello(packed)
        assertEquals("dev-1", a)
        assertEquals("tr-1", b)
        assertEquals("file-1", c)
        // 中文 id 也能正确编解码
        val cn = FrameCodec.packDataHello("设备", "传输", "文件")
        val (x, y, z) = FrameCodec.parseDataHello(cn)
        assertEquals("设备", x)
        assertEquals("传输", y)
        assertEquals("文件", z)
    }

    @Test
    fun chunkRoundTrip() {
        val packed = FrameCodec.packChunk(1_048_576L, byteArrayOf(1, 2, 3))
        val (offset, payload) = FrameCodec.parseChunk(packed)
        assertEquals(1_048_576L, offset)
        assertArrayEquals(byteArrayOf(1, 2, 3), payload)
    }

    @Test
    fun dataDoneRoundTrip() {
        val digest = ByteArray(32) { it.toByte() }
        val (total, sha) = FrameCodec.parseDataDone(FrameCodec.packDataDone(12_345L, digest))
        assertEquals(12_345L, total)
        assertArrayEquals(digest, sha)
    }

    @Test
    fun dataErrorRoundTrip() {
        val (code, msg) = FrameCodec.parseDataError(FrameCodec.packDataError(7, "校验失败"))
        assertEquals(7, code)
        assertEquals("校验失败", msg)
    }
}
