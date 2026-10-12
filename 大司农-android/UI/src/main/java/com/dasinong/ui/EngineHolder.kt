package com.dasinong.ui

import android.content.Context
import com.dasinong.backend.NodeEngine

/** 全局单例持有 NodeEngine，供前台服务与 UI 共享。 */
object EngineHolder {
    @Volatile var instance: NodeEngine? = null
        private set
    @Volatile var started: Boolean = false

    fun getOrCreate(context: Context): NodeEngine {
        instance?.let { return it }
        synchronized(this) {
            instance?.let { return it }
            val engine = NodeEngine(context.applicationContext)
            instance = engine
            return engine
        }
    }

    fun require(): NodeEngine = instance ?: error("NodeEngine 尚未初始化")

    fun stopIfStarted() {
        if (started) {
            runCatching { instance?.stop() }
            started = false
        }
    }
}
