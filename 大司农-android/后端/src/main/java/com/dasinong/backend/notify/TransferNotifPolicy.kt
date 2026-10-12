package com.dasinong.backend.notify

/**
 * 传输通知的**节流 + 去重**策略（纯 Kotlin，放 :backend 便于 JVM 单测，不依赖 Android）。
 *
 * 为什么要它：
 * - 接收侧每收 64 KiB 就发布一次进度事件，直接照着弹/刷会爆炸；
 * - 完成/失败可能被多次发布，必须只提示一次；
 * - 没有活动任务时要能回到「空闲」文案。
 *
 * 注意：事件由多个网络/接收线程并发发布，所以这里的方法是 `@Synchronized` 的。
 */
class TransferNotifPolicy(private val minIntervalMs: Long = 1_000L) {

    private val notifiedTerminal = HashSet<String>()
    private val active = HashSet<String>()
    private val lastAt = HashMap<String, Long>()
    private val lastPct = HashMap<String, Int>()

    /** 是否还有正在传输的任务（用来决定常驻通知显示进度还是空闲文案）。 */
    val hasActive: Boolean
        @Synchronized get() = active.isNotEmpty()

    /** 正在传输的任务数（多任务时常驻通知文案显示「传输中 N 个」）。 */
    val activeCount: Int
        @Synchronized get() = active.size

    /**
     * 完成/失败是否该提示。同一个 (fileId, state) 只允许 true 一次；
     * 顺带把该文件移出「活动中」，所以调用方不必再单独调 onFinished。
     */
    @Synchronized
    fun shouldNotifyTerminal(fileId: String, state: String): Boolean {
        active.remove(fileId)
        lastPct.remove(fileId)
        lastAt.remove(fileId)
        return notifiedTerminal.add("$fileId:$state")
    }

    /**
     * 进度是否该刷新常驻通知：首次必刷；之后要求**整数百分比变化**且距上次 ≥ [minIntervalMs]。
     * 返回 false 表示这次跳过（别去动通知，省电也防刷屏）。
     */
    @Synchronized
    fun shouldUpdateProgress(fileId: String, percent: Int, nowMs: Long): Boolean {
        val first = active.add(fileId)
        if (first) {
            lastAt[fileId] = nowMs
            lastPct[fileId] = percent
            return true
        }
        if (lastPct[fileId] == percent) return false
        if (nowMs - (lastAt[fileId] ?: 0L) < minIntervalMs) return false
        lastAt[fileId] = nowMs
        lastPct[fileId] = percent
        return true
    }

    /** 任务被取消等情况下手动移出「活动中」。 */
    @Synchronized
    fun onFinished(fileId: String) {
        active.remove(fileId)
        lastPct.remove(fileId)
        lastAt.remove(fileId)
    }
}
