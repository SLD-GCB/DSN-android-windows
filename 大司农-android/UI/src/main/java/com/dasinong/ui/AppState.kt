package com.dasinong.ui

/**
 * 极简的「应用是否在前台」标记：MainActivity 置位，Notifier 用它决定
 * 「正在看着 App 时就不弹通知」（气泡里已经能看到）。
 */
object AppState {
    @Volatile
    var foreground: Boolean = false
}
