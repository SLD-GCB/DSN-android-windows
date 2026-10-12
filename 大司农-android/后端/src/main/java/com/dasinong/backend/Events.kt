package com.dasinong.backend

import com.dasinong.backend.model.ChatMessage
import com.dasinong.backend.model.Peer
import com.dasinong.backend.model.Transfer
import java.util.concurrent.CopyOnWriteArrayList

/** 后端事件（网络线程发布，UI 侧负责切回主线程）。 */
sealed class Event {
    data class PeerAdded(val peer: Peer) : Event()
    data class PeerUpdated(val peer: Peer) : Event()
    data class PeerRemoved(val deviceId: String) : Event()
    data class MessageEvent(val message: ChatMessage) : Event()
    data class MessageStatusEvent(val msgId: String, val status: String) : Event()
    data class TransferEvent(val transfer: Transfer) : Event()

    /** 一条传输记录被删除（含本地文件），UI 据此把它从列表移除。 */
    data class TransferRemoved(val fileId: String) : Event()
    data class LogEvent(val text: String) : Event()

    /** 手动 IP 会话已绑定到真实设备（发现后自动升级），UI 据此切换当前会话。 */
    data class ConversationRebound(val oldId: String, val newId: String) : Event()
}

class EventBus {
    private val listeners = CopyOnWriteArrayList<(Event) -> Unit>()

    fun subscribe(listener: (Event) -> Unit) {
        listeners.add(listener)
    }

    fun unsubscribe(listener: (Event) -> Unit) {
        listeners.remove(listener)
    }

    fun publish(event: Event) {
        for (l in listeners) {
            try {
                l(event)
            } catch (_: Exception) {
            }
        }
    }
}
