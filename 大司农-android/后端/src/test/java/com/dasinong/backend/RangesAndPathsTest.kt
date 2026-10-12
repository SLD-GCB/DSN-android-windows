package com.dasinong.backend

import com.dasinong.backend.util.Paths
import com.dasinong.backend.util.Ranges
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** 字节区间与路径清洗单测（与 Windows 端 utils.py 行为一致）。 */
class RangesAndPathsTest {

    @Test
    fun mergeRanges() {
        assertEquals(listOf(0L..19L), Ranges.merge(listOf(0L..9L, 10L..19L)))
        assertEquals(listOf(0L..9L), Ranges.merge(listOf(5L..9L, 0L..4L)))
        assertTrue(Ranges.merge(emptyList()).isEmpty())
    }

    @Test
    fun totalLen() {
        assertEquals(20L, Ranges.totalLen(listOf(0L..9L, 20L..29L)))
        assertEquals(0L, Ranges.totalLen(emptyList()))
    }

    @Test
    fun complement() {
        assertEquals(listOf(0L..99L), Ranges.complement(100L, emptyList()))
        assertEquals(listOf(50L..99L), Ranges.complement(100L, listOf(0L..49L)))
        assertTrue(Ranges.complement(100L, listOf(0L..99L)).isEmpty())
        assertEquals(listOf(10L..89L), Ranges.complement(100L, listOf(0L..9L, 90L..99L)))
    }

    @Test
    fun sanitizeComponent() {
        assertEquals("a_b", Paths.sanitizeComponent("a/b"))
        assertEquals("_CON", Paths.sanitizeComponent("CON"))
        assertEquals("trailing", Paths.sanitizeComponent("trailing. "))
        assertEquals("_", Paths.sanitizeComponent(""))
    }

    @Test
    fun sanitizeRelPathBlocksTraversal() {
        val p = Paths.sanitizeRelPath("../../etc/passwd")
        assertTrue(!p.split("/").contains(".."))
        assertEquals("passwd", p.split("/").last())
        assertEquals("项目/2026/报告.pdf", Paths.sanitizeRelPath("项目/2026/报告.pdf"))
    }

    @Test
    fun kindOf() {
        assertEquals("image", Paths.kindOf("a.PNG"))
        assertEquals("image", Paths.kindOf("a.jpeg"))
        assertEquals("file", Paths.kindOf("a.zip"))
    }
}
