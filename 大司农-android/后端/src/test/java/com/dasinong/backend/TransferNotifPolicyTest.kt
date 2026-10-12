package com.dasinong.backend

import com.dasinong.backend.notify.TransferNotifPolicy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TransferNotifPolicyTest {

    @Test
    fun terminal_notified_once_per_state() {
        val p = TransferNotifPolicy()
        assertTrue(p.shouldNotifyTerminal("f1", "COMPLETED"))
        assertFalse(p.shouldNotifyTerminal("f1", "COMPLETED"))   // 重复发布不再提示
        assertTrue(p.shouldNotifyTerminal("f1", "FAILED"))       // 换状态算新的
        assertTrue(p.shouldNotifyTerminal("f2", "COMPLETED"))
    }

    @Test
    fun progress_first_always_then_throttled_by_interval_and_percent() {
        val p = TransferNotifPolicy(minIntervalMs = 1_000)
        assertTrue(p.shouldUpdateProgress("f1", 0, 0))            // 首次必刷
        assertFalse(p.shouldUpdateProgress("f1", 0, 5_000))       // 百分比没变 → 跳过
        assertFalse(p.shouldUpdateProgress("f1", 30, 500))        // 变化了但没到间隔 → 跳过
        assertTrue(p.shouldUpdateProgress("f1", 30, 1_000))       // 到间隔 → 刷
        assertFalse(p.shouldUpdateProgress("f1", 30, 9_000))      // 同百分比 → 跳过
        assertTrue(p.shouldUpdateProgress("f1", 31, 9_000))       // 百分比变了 → 刷
    }

    @Test
    fun receive_side_chunk_storm_is_collapsed() {
        // 模拟接收侧每 64KiB 一次事件：1 秒内 100 次，只应该刷 1 次（首次）
        val p = TransferNotifPolicy(minIntervalMs = 1_000)
        var updates = 0
        for (i in 0..99) {
            val pct = i            // 百分比一直在变
            if (p.shouldUpdateProgress("big", pct, i * 10L)) updates++
        }
        assertEquals(1, updates)
    }

    @Test
    fun hasActive_tracks_start_and_terminal() {
        val p = TransferNotifPolicy()
        assertFalse(p.hasActive)
        p.shouldUpdateProgress("f1", 10, 0)
        assertTrue(p.hasActive)
        p.shouldNotifyTerminal("f1", "COMPLETED")   // 终态顺带移出活动
        assertFalse(p.hasActive)
    }

    @Test
    fun onFinished_removes_active_without_notifying() {
        val p = TransferNotifPolicy()
        p.shouldUpdateProgress("f1", 10, 0)
        p.onFinished("f1")
        assertFalse(p.hasActive)
        // 取消后再完成，仍然可以提示
        assertTrue(p.shouldNotifyTerminal("f1", "CANCELLED"))
    }
}
