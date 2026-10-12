package com.dasinong.backend.protocol

import org.json.JSONObject
import java.util.UUID

/** JSON 信封构造/解析 + 版本协商。信封：{proto,type,id,from,to,ts,data}。 */
object Messages {

    fun newId(): String = UUID.randomUUID().toString()

    fun envelope(
        type: String,
        fromId: String,
        toId: String? = null,
        data: JSONObject? = null,
        proto: Int = Proto.VERSION,
        id: String = newId(),
        ts: Long = System.currentTimeMillis(),
    ): JSONObject {
        val o = JSONObject()
        o.put("proto", proto)
        o.put("type", type)
        o.put("id", id)
        o.put("from", fromId)
        o.put("to", toId ?: JSONObject.NULL)
        o.put("ts", ts)
        o.put("data", data ?: JSONObject())
        return o
    }

    fun encode(obj: JSONObject): ByteArray = obj.toString().toByteArray(Charsets.UTF_8)

    /** 解析 JSON 帧体；非法返回 null（调用方应忽略该帧）。 */
    fun decode(body: ByteArray): JSONObject? = try {
        JSONObject(String(body, Charsets.UTF_8))
    } catch (e: Exception) {
        null
    }

    /** 协商生效协议版本；返回 -1 表示不兼容。 */
    fun negotiate(
        remoteProto: Int,
        remoteMin: Int,
        remoteMax: Int,
        local: Int = Proto.VERSION,
        localMin: Int = Proto.MIN_VERSION,
    ): Int {
        val eff = minOf(local, remoteMax)
        if (eff < remoteMin || eff < localMin) return -1
        return eff
    }
}
