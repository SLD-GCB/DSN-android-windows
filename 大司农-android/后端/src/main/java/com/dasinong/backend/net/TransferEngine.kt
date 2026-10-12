package com.dasinong.backend.net

import com.dasinong.backend.Event
import com.dasinong.backend.EventBus
import com.dasinong.backend.NodeConfig
import com.dasinong.backend.model.FileEntry
import com.dasinong.backend.model.Peer
import com.dasinong.backend.model.Transfer
import com.dasinong.backend.model.TransferState
import com.dasinong.backend.protocol.FrameCodec
import com.dasinong.backend.protocol.FrameType
import com.dasinong.backend.protocol.Messages
import com.dasinong.backend.protocol.Proto
import com.dasinong.backend.util.Paths
import com.dasinong.backend.util.Ranges
import org.json.JSONArray
import org.json.JSONObject
import java.io.Closeable
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.RandomAccessFile
import java.net.Socket
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList

/** 待发送的本地文件（由 UI 层从 content Uri / 路径构造）。 */
data class LocalFile(
    val name: String,
    val relPath: String,
    val rootName: String?,
    val size: Long,
    val mtime: Double,
    val kind: String,
    val open: () -> InputStream,
    /** 本机来源（content:// 或文件路径），仅用于 UI 预览缩略图。 */
    val source: String = "",
)

/** 支持随机起点读取的流封装（内部维持位置，必要时重新打开并跳过）。 */
private class RangeReader(private val source: LocalFile) : Closeable {
    private var ins: InputStream? = null
    private var pos: Long = 0

    private fun ensure() {
        if (ins == null) {
            ins = source.open(); pos = 0
        }
    }

    fun readAt(offset: Long, length: Int): ByteArray? {
        if (ins == null || offset < pos) {
            ins?.close()
            ins = source.open()
            pos = 0
        }
        ensure()
        var toSkip = offset - pos
        val skipBuf = ByteArray(64 * 1024)
        while (toSkip > 0) {
            val s = ins!!.skip(toSkip)
            if (s > 0) {
                toSkip -= s
            } else {
                val want = minOf(toSkip, skipBuf.size.toLong()).toInt()
                val r = ins!!.read(skipBuf, 0, want)
                if (r < 0) return null
                toSkip -= r
            }
        }
        pos = offset
        val buf = ByteArray(length)
        var read = 0
        while (read < length) {
            val r = ins!!.read(buf, read, length - read)
            if (r < 0) break
            read += r
        }
        pos += read
        return when {
            read == 0 -> null
            read == length -> buf
            else -> buf.copyOf(read)
        }
    }

    override fun close() {
        ins?.close()
        ins = null
    }
}

/** 发 offer 时等控制连接回来的时间（文字有待发队列，文件要有同等耐心）。 */
private const val OFFER_WAIT_MS = 20_000L

/**
 * 传输引擎：并行分块发送 + 分块接收 + 断点续传（与 Windows 端 transfer.py 对齐）。
 */
class TransferEngine(
    private val cfg: NodeConfig,
    private val binder: LanNetworkBinder,
    private val bus: EventBus,
    private val transport: TransportManager,
    private val resolvePeer: (String) -> Peer?,
    @Suppress("unused") private val autoAccept: Boolean = true,
    private val tunnelProvider: (() -> TunnelContext?)? = null,
    /** 需要控制连接时回调（让引擎立刻重连，而不是等下一次退避）。 */
    private val needSession: ((String) -> Unit)? = null,
) {
    /** 「TCP 并发」：一次传输并发几条数据连接（1~8）。由 NodeEngine 按设置写入，立刻生效。 */
    @Volatile var maxParallel: Int = Proto.DEFAULT_PARALLEL

    private class SendFile(val entry: FileEntry, val source: LocalFile) {
        val remaining = CopyOnWriteArrayList<LongRange>()
        @Volatile var sent: Long = 0
        @Volatile var completed: Boolean = false
    }

    private class SendTask(val transferId: String, val peerId: String, val files: MutableList<SendFile>) {
        val takeLock = Any()
        @Volatile var accepted = false
    }

    private class RecvFile(val entry: FileEntry, val partPath: File, finalPath: File) {
        @Volatile var finalPath: File = finalPath
        val ranges = CopyOnWriteArrayList<LongRange>()
        @Volatile var written: Long = 0
        @Volatile var completed: Boolean = false
        @Volatile var lastMeta: Long = 0
        val lock = Any()

        /**
         * 还开着的写句柄数。落盘前必须等它归零：句柄没释放就去校验/改名，
         * 轻则读到没写完的内容、重则（Windows 端）重命名直接报「另一个程序正在使用此文件」。
         */
        val writers = java.util.concurrent.atomic.AtomicInteger(0)
    }

    private class RecvTask(val transferId: String, val peerId: String) {
        val files = ConcurrentHashMap<String, RecvFile>()
    }

    private val sends = ConcurrentHashMap<String, SendTask>()
    private val recvs = ConcurrentHashMap<String, RecvTask>()
    private val shaCache = ConcurrentHashMap<String, String>()
    private val progressAt = ConcurrentHashMap<String, Long>()
    private val progressPctRecv = ConcurrentHashMap<String, Int>()

    // 接收目标分配：多台设备并发发同名文件时，必须给每个任务**独占**的 final/part，
    // 否则两个任务各拿一把 rf.lock 去写同一个 .part → 内容直接损坏。
    private val reserveLock = Any()
    private val claimedParts = HashSet<File>()
    private val claimedFinals = HashSet<File>()
    @Volatile private var stopped = false

    fun stop() {
        stopped = true
    }

    // ------------------------------------------------------------ 发送
    fun sendFiles(peerId: String, sources: List<LocalFile>): String? {
        if (sources.isEmpty()) return null
        val transferId = UUID.randomUUID().toString()
        val files = sources.map { src ->
            val entry = FileEntry(
                fileId = UUID.randomUUID().toString(),
                name = src.name,
                relPath = src.relPath,
                rootName = src.rootName,
                size = src.size,
                kind = src.kind,
                contentId = sha256Of(src),
                chunkSize = Proto.CHUNK_SIZE,
                totalChunks = maxOf(1, ((src.size + Proto.CHUNK_SIZE - 1) / Proto.CHUNK_SIZE).toInt()),
                mtime = src.mtime,
            )
            SendFile(entry, src)
        }
        val task = SendTask(transferId, peerId, files.toMutableList())
        sends[transferId] = task
        for (sf in files) publish(sendTransfer(task, sf, TransferState.PENDING, null))
        val arr = JSONArray()
        files.forEach { arr.put(it.entry.toJson()) }
        val env = Messages.envelope(
            "offer", cfg.deviceId, peerId,
            JSONObject().put("transfer_id", transferId).put("files", arr),
        )
        if (!transport.sendTo(peerId, env)) {
            // 控制连接一时不可用（热点下会话会短暂断开再自动连回来）：**等它回来再发**，
            // 别当场判死。文字消息有待发队列兜着，所以以前会出现「文字能过、文件过不去」。
            publishPending(task)
            Thread({ offerWithRetry(task, env) }, "offer-${transferId.take(8)}")
                .apply { isDaemon = true }.start()
        }
        return transferId
    }

    private fun publishPending(task: SendTask) {
        for (sf in task.files) {
            publish(sendTransfer(task, sf, TransferState.PENDING, "等待控制连接…"))
        }
    }

    private fun offerWithRetry(task: SendTask, env: JSONObject) {
        needSession?.invoke(task.peerId)
        val deadline = System.currentTimeMillis() + OFFER_WAIT_MS
        while (!stopped) {
            if (transport.sendTo(task.peerId, env)) return
            if (System.currentTimeMillis() >= deadline) break
            needSession?.invoke(task.peerId)      // 每轮催一下重连，恢复更快
            Thread.sleep(500)
        }
        for (sf in task.files) {
            publish(sendTransfer(task, sf, TransferState.FAILED,
                "控制连接不可用（已等 ${OFFER_WAIT_MS / 1000} 秒）"))
        }
        sends.remove(task.transferId)
    }

    private fun sha256Of(src: LocalFile): String {
        val key = "${src.relPath}|${src.size}|${src.mtime}"
        shaCache[key]?.let { return it }
        val md = MessageDigest.getInstance("SHA-256")
        src.open().use { ins ->
            val buf = ByteArray(1 shl 20)
            while (true) {
                val r = ins.read(buf)
                if (r < 0) break
                md.update(buf, 0, r)
            }
        }
        val hex = md.digest().joinToString("") { "%02x".format(it) }
        shaCache[key] = hex
        return hex
    }

    private fun onAccept(env: JSONObject) {
        val data = env.optJSONObject("data") ?: return
        val tid = data.optString("transfer_id")
        val task = sends[tid] ?: return
        val receivedMap = HashMap<String, List<LongRange>>()
        val acceptFiles = data.optJSONArray("files")
        if (acceptFiles != null) {
            for (i in 0 until acceptFiles.length()) {
                val item = acceptFiles.optJSONObject(i) ?: continue
                if (item.optBoolean("accepted", true)) {
                    receivedMap[item.optString("file_id")] =
                        Ranges.fromJsonArray(item.optJSONArray("received_ranges"))
                }
            }
        }
        task.accepted = true
        for (sf in task.files) {
            val ranges = Ranges.merge(receivedMap[sf.entry.fileId] ?: emptyList())
            sf.remaining.clear()
            sf.remaining.addAll(Ranges.complement(sf.entry.size, ranges))
            sf.sent = sf.entry.size - Ranges.totalLen(sf.remaining)
            publish(sendTransfer(task, sf, TransferState.ACTIVE, null))
        }
        Thread({ runSend(task) }, "send-${tid.take(8)}").apply { isDaemon = true }.start()
    }

    private fun runSend(task: SendTask) {
        var peer = resolvePeer(task.peerId)
        if (peer == null) {
            // 刚连上/正在替换连接时可能短暂查不到地址：给一小段宽限再判死，
            // 否则多设备同时连时一次抖动就让整次发送失败。
            val deadline = System.currentTimeMillis() + 5_000
            while (peer == null && System.currentTimeMillis() < deadline && !stopped) {
                Thread.sleep(250)
                peer = resolvePeer(task.peerId)
            }
        }
        if (peer == null) {
            for (sf in task.files) publish(sendTransfer(task, sf, TransferState.FAILED, "对端不可达"))
            return
        }
        val threads = ArrayList<Thread>()
        for (sf in task.files) {
            if (sf.remaining.isEmpty()) {
                sf.completed = true
                continue
            }
            // 「TCP 并发」（设置页）：1~8；填 1 = 单连接（与旧行为一致）
            val slots = maxOf(1, minOf(maxParallel.coerceIn(1, Proto.MAX_PARALLEL), countPieces(sf.remaining, sf.entry.chunkSize)))
            repeat(slots) {
                val t = Thread({ sendWorker(task, sf, peer) }, "sendw-${sf.entry.fileId.take(6)}")
                t.isDaemon = true
                t.start()
                threads.add(t)
            }
        }
        threads.forEach { it.join() }
        for (sf in task.files) {
            if (sf.remaining.isEmpty() && !sf.completed) {
                sf.completed = true
                transport.sendTo(
                    task.peerId,
                    Messages.envelope(
                        "complete", cfg.deviceId, task.peerId,
                        JSONObject().put("transfer_id", task.transferId).put("file_id", sf.entry.fileId),
                    ),
                )
            }
        }
    }

    /**
     * 取当前隧道上下文；`null` = 本机未接线/未配置隧道。
     * ⚠ 刻意不吞异常：加密状态未知时必须 fail-closed，不能退化成明文。
     */
    private fun currentTunnel(): TunnelContext? = tunnelProvider?.invoke()

    /**
     * 开一条数据连接；本机启用加密时包 TLS + 握手。
     *
     * 失败一律**抛异常**（调用方会把它记进传输状态），绝不回退成明文 ——
     * 数据连接与控制连接必须同等 fail-closed。
     */
    private fun openDataSocket(ip: String, port: Int): Socket {
        val sock = binder.connectTcpAny(ip, port, Proto.CONNECT_TIMEOUT_MS)
        val tunnel = try {
            currentTunnel()
        } catch (e: Exception) {
            runCatching { sock.close() }
            throw IOException("本机身份状态异常，已取消数据连接：${e.message}", e)
        } ?: run {
            runCatching { sock.close() }
            throw IOException("本机身份不可用，已取消数据连接")
        }
        return try {
            tunnel.connect(sock, ip).sock
        } catch (e: Exception) {
            runCatching { sock.close() }
            throw IOException("数据连接隧道失败：${e.message}", e)
        }
    }

    private fun sendWorker(task: SendTask, sf: SendFile, peer: Peer) {
        var sock: Socket? = null
        val reader = RangeReader(sf.source)
        try {
            sock = openDataSocket(peer.ip, peer.tcpPort)
            val out = sock.getOutputStream()
            FrameCodec.writeFrame(
                out, FrameType.DATA_HELLO,
                FrameCodec.packDataHello(cfg.deviceId, task.transferId, sf.entry.fileId),
            )
            while (!stopped) {
                val piece = takePiece(task, sf) ?: break
                val chunk = reader.readAt(piece.first, piece.second) ?: break
                FrameCodec.writeFrame(out, FrameType.CHUNK, FrameCodec.packChunk(piece.first, chunk))
                sf.sent += chunk.size
                maybeProgress(task, sf)
            }
            FrameCodec.writeFrame(
                out, FrameType.DATA_DONE,
                FrameCodec.packDataDone(sf.entry.size, hexToBytes(sf.entry.contentId)),
            )
        } catch (e: Exception) {
            publish(sendTransfer(task, sf, TransferState.PAUSED, "连接中断"))
        } finally {
            reader.close()
            try {
                sock?.close()
            } catch (_: Exception) {
            }
        }
    }

    private fun takePiece(task: SendTask, sf: SendFile): Pair<Long, Int>? = synchronized(task.takeLock) {
        if (sf.remaining.isEmpty()) return null
        val first = sf.remaining[0]
        val length = minOf(sf.entry.chunkSize.toLong(), first.last - first.first + 1).toInt()
        if (first.first + length > first.last) {
            sf.remaining.removeAt(0)
        } else {
            sf.remaining[0] = (first.first + length)..first.last
        }
        first.first to length
    }

    private fun countPieces(ranges: List<LongRange>, chunkSize: Int): Int {
        val total = Ranges.totalLen(ranges)
        return maxOf(1, ((total + chunkSize - 1) / chunkSize).toInt())
    }

    private fun maybeProgress(task: SendTask, sf: SendFile) {
        val key = "${task.transferId}:${sf.entry.fileId}:s"
        val now = System.currentTimeMillis()
        if (now - (progressAt[key] ?: 0L) < 300) return
        progressAt[key] = now
        publish(sendTransfer(task, sf, TransferState.ACTIVE, null))
    }

    // ------------------------------------------------------------ 接收
    private fun onOffer(conn: ControlConnection, env: JSONObject) {
        val data = env.optJSONObject("data") ?: return
        val tid = data.optString("transfer_id")
        val files = data.optJSONArray("files")
        if (tid.isEmpty() || files == null || files.length() == 0) return

        val task = RecvTask(tid, conn.peerId)
        val acceptArr = JSONArray()
        for (i in 0 until files.length()) {
            val fd = files.optJSONObject(i) ?: continue
            val entry = FileEntry.fromJson(fd)
            val rel = Paths.sanitizeRelPath(entry.relPath.ifEmpty { entry.name })
            val (final, part, resumed) = reserveTarget(rel, entry)
            final.parentFile?.mkdirs()

            var ranges: List<LongRange> = emptyList()
            if (resumed) {
                val meta = ResumeStore.load(part)
                if (meta != null && meta.sha256 == entry.contentId && meta.size == entry.size) {
                    ranges = Ranges.merge(meta.received)
                }
            }
            if (!part.exists() || part.length() != entry.size) {
                try {
                    RandomAccessFile(part, "rw").use { it.setLength(entry.size) }
                } catch (_: Exception) {
                }
                ranges = emptyList()
            }
            val rf = RecvFile(entry, part, final)
            rf.ranges.addAll(ranges)
            rf.written = Ranges.totalLen(ranges)
            task.files[entry.fileId] = rf
            acceptArr.put(
                JSONObject()
                    .put("file_id", entry.fileId)
                    .put("accepted", true)
                    .put("resume", ranges.isNotEmpty())
                    .put("received_ranges", Ranges.toJsonArray(ranges)),
            )
            publish(recvTransfer(task, rf, if (ranges.isEmpty()) TransferState.PENDING else TransferState.ACTIVE, null))
        }
        recvs[tid] = task
        conn.send(
            Messages.envelope(
                "accept", cfg.deviceId, conn.peerId,
                JSONObject()
                    .put("transfer_id", tid)
                    .put("files", acceptArr)
                    .put("max_parallel", maxParallel.coerceIn(1, Proto.MAX_PARALLEL))
                    .put("window_bytes", Proto.WINDOW_BYTES),
            ),
        )
    }

    /** 第 i 个候选名：0 = 原名，之后 `名字 (1).jpg`、`名字 (2).jpg`… */
    private fun candidate(parent: File, stem: String, ext: String, i: Int): File =
        File(parent, if (i == 0) "$stem$ext" else "$stem ($i)$ext")

    private fun splitName(name: String): Pair<String, String> {
        val dot = name.lastIndexOf('.')
        return if (dot > 0) name.substring(0, dot) to name.substring(dot) else name to ""
    }

    /**
     * 原子地给这次接收分配 (final, part, 是否续传)。
     *
     * 为什么不能只查 final：final 要到传完才出现，传输期间只有 .part。
     * 两台设备同时发 `IMG.jpg` 时两个任务都会看到 final 不存在 → 选中同一个
     * final/part，而它们各有独立的 rf.lock，于是两把锁写同一个文件。
     * 这里在锁内选名，并用 createNewFile()（等价 O_EXCL）独占占位：
     * - 占位失败（别人先抢到）→ 换下一个候选名；
     * - .part 已存在但 .partmeta 的 contentId/size 与本次吻合 → 认领它（续传）；
     * - .part 已存在且不吻合 → 换名（别糟蹋别人的在途文件）。
     */
    private fun reserveTarget(rel: String, entry: FileEntry): Triple<File, File, Boolean> {
        val p = File(cfg.receiveDir, rel)
        val parent = p.parentFile ?: cfg.receiveDir
        val (stem, ext) = splitName(p.name)
        synchronized(reserveLock) {
            var i = 0
            while (true) {
                val final = candidate(parent, stem, ext, i)
                val part = File(final.parentFile, final.name + ".part")
                if (!final.exists() && final !in claimedFinals && part !in claimedParts) {
                    if (!part.exists()) {
                        parent.mkdirs()
                        if (runCatching { part.createNewFile() }.getOrDefault(false)) {
                            claimedParts.add(part)
                            claimedFinals.add(final)
                            return Triple(final, part, false)
                        }
                    } else {
                        val meta = ResumeStore.load(part)
                        if (meta != null && meta.sha256 == entry.contentId && meta.size == entry.size) {
                            claimedParts.add(part)
                            claimedFinals.add(final)
                            return Triple(final, part, true)
                        }
                    }
                }
                i++
                if (i > 9999) throw IllegalStateException("无法为 ${entry.name} 分配唯一文件名")
            }
        }
    }

    /** 落盘时再取一次唯一名（调用方需持有 reserveLock）。 */
    private fun uniqueFinalLocked(f: File): File {
        val parent = f.parentFile ?: return f      // 没有父目录（不该发生）：原样返回
        val (stem, ext) = splitName(f.name)
        var i = 0
        while (true) {
            val cand = candidate(parent, stem, ext, i)
            if (!cand.exists() && cand !in claimedFinals) return cand
            i++
            if (i > 9999) throw IllegalStateException("无法为 ${f.name} 分配唯一文件名")
        }
    }

    /** 释放这批接收占用的名字（失败/取消后调用），否则重试时无法续传。 */
    private fun releaseRecv(task: RecvTask) {
        synchronized(reserveLock) {
            for (rf in task.files.values) {
                claimedParts.remove(rf.partPath)
                claimedFinals.remove(rf.finalPath)
            }
        }
    }

    fun handleDataConnection(sock: Socket, type: Int, body: ByteArray, ip: String) {
        if (type != FrameType.DATA_HELLO) {
            sock.close(); return
        }
        val triple = try {
            FrameCodec.parseDataHello(body)
        } catch (e: Exception) {
            sock.close(); return
        }
        val dev = triple.first
        val tid = triple.second
        val fid = triple.third
        val task = recvs[tid]
        val rf = task?.files?.get(fid)
        if (task == null || rf == null) {
            sock.close(); return
        }
        // 数据连接必须来自这笔传输的对端：多设备并发时，光靠 (transferId, fileId) 不够，
        // 再加一道 deviceId 校验，防止串流/注入。
        if (dev.isNotEmpty() && task.peerId.isNotEmpty() && dev != task.peerId) {
            sock.close(); return
        }
        Thread({ recvWorker(sock, task, rf) }, "recv-${fid.take(6)}").apply { isDaemon = true }.start()
    }

    private fun recvWorker(sock: Socket, task: RecvTask, rf: RecvFile) {
        var raf: RandomAccessFile? = null
        rf.writers.incrementAndGet()
        try {
            raf = RandomAccessFile(rf.partPath, "rw")
            val input = sock.getInputStream()
            while (!stopped) {
                val frame = FrameCodec.readFrame(input) ?: break
                when (frame.type) {
                    FrameType.CHUNK -> {
                        val parsed = FrameCodec.parseChunk(frame.body)
                        val offset = parsed.first
                        val payload = parsed.second
                        if (payload.isEmpty()) continue
                        synchronized(rf.lock) {
                            raf.seek(offset)
                            raf.write(payload)
                            // 注意：LongRange 本身也是 Iterable<Long>，直接对 List<LongRange> 用 `+` 会命中
                            // Iterable 重载导致类型推断成 Any，故显式构造列表。
                            val pending = ArrayList<LongRange>(rf.ranges.size + 1)
                            pending.addAll(rf.ranges)
                            pending.add(offset..(offset + payload.size - 1))
                            val merged = Ranges.merge(pending)
                            rf.ranges.clear()
                            rf.ranges.addAll(merged)
                            rf.written = Ranges.totalLen(rf.ranges)
                            saveMeta(rf)
                        }
                        maybeProgressRecv(task, rf)
                    }
                    FrameType.DATA_DONE, FrameType.DATA_ERROR -> break
                }
            }
            raf.fd.sync()
        } catch (_: Exception) {
        } finally {
            try {
                raf?.close()
            } catch (_: Exception) {
            }
            rf.writers.decrementAndGet()      // 句柄已释放，落盘那边可以继续了
            try {
                sock.close()
            } catch (_: Exception) {
            }
        }
    }

    /** 接收侧进度节流：≥300ms 且整数百分比变化才发事件。 */
    private fun maybeProgressRecv(task: RecvTask, rf: RecvFile) {
        // 接收侧是「每 64 KiB 一块」，不节流会变成每秒上千次事件 + 上千次 SQLite 写入，
        // 多设备并发时把 IO 和线程都打满。
        val key = "${task.transferId}:${rf.entry.fileId}:r"
        val now = System.currentTimeMillis()
        val total = rf.entry.size
        val pct = if (total > 0) ((rf.written * 100) / total).toInt() else 0
        if (progressPctRecv[key] == pct && now - (progressAt[key] ?: 0L) < 300) return
        if (now - (progressAt[key] ?: 0L) < 300) return
        progressAt[key] = now
        progressPctRecv[key] = pct
        publish(recvTransfer(task, rf, TransferState.ACTIVE, null))
    }

    private fun saveMeta(rf: RecvFile) {
        val now = System.currentTimeMillis()
        if (now - rf.lastMeta < 500) return
        rf.lastMeta = now
        ResumeStore.save(
            rf.partPath,
            PartMeta(
                transferId = "", fileId = rf.entry.fileId, name = rf.entry.name,
                size = rf.entry.size, sha256 = rf.entry.contentId,
                chunkSize = rf.entry.chunkSize, totalChunks = rf.entry.totalChunks,
                received = rf.ranges.toList(),
            ),
        )
    }

    private fun onComplete(conn: ControlConnection, env: JSONObject) {
        val data = env.optJSONObject("data") ?: return
        val tid = data.optString("transfer_id")
        val fid = data.optString("file_id")
        val task = recvs[tid] ?: return
        val rf = task.files[fid] ?: return
        // 收尾要等字节、等句柄，可能耗上百毫秒甚至更久；**不能占着控制读线程**
        // （拖住 ping/pong 会被对端判定超时、把会话掐掉）。Windows 端同样是另起线程。
        Thread({ finalizeRecv(conn, task, rf, tid, fid) }, "finalize-${fid.take(6)}")
            .apply { isDaemon = true }.start()
    }

    private fun finalizeRecv(
        conn: ControlConnection,
        task: RecvTask,
        rf: RecvFile,
        tid: String,
        fid: String,
    ) {
        // 1) 等字节收齐：`complete` 只说明**发方写完 socket**，接收侧可能还有数据在缓冲区，
        //    这时直接校验会读到不完整的内容 → 误报校验失败（就表现为「文件传不过来」）。
        val waitDeadline = System.currentTimeMillis() + 120_000
        while (rf.written < rf.entry.size && !stopped && System.currentTimeMillis() < waitDeadline) {
            Thread.sleep(50)
        }
        if (rf.completed) return
        if (rf.written < rf.entry.size) {
            publish(recvTransfer(task, rf, TransferState.PAUSED, "未收齐（${rf.written}/${rf.entry.size}）"))
            return
        }
        // 2) 再等这个文件的所有数据连接把手柄关掉：多路并行时最后收尾的 worker 可能还没
        //    close，此时校验/改名会读到没写完的内容（Windows 端更是直接报「另一个程序正在使用此文件」）。
        val drainDeadline = System.currentTimeMillis() + 10_000
        while (rf.writers.get() > 0 && System.currentTimeMillis() < drainDeadline) {
            Thread.sleep(20)
        }
        if (sha256File(rf.partPath) != rf.entry.contentId) {
            publish(recvTransfer(task, rf, TransferState.FAILED, "sha256 校验失败"))
            conn.send(
                Messages.envelope(
                    "error", cfg.deviceId, task.peerId,
                    JSONObject().put("transfer_id", tid).put("file_id", fid)
                        .put("code", 1).put("message", "checksum mismatch"),
                ),
            )
            return
        }
        // 选最终名 + 改名放在 reserve 锁里：两个任务同时传完也不会抢同一个名字。
        // 先把「自己的预定」放开，否则 uniqueFinalLocked 会把我们本来就想要的名字
        // 当成别人的占用，结果每个文件都被改名成 `名字 (1).ext`。
        var renameErr = false
        val final: File
        synchronized(reserveLock) {
            val old = rf.finalPath
            claimedFinals.remove(old)
            val f = uniqueFinalLocked(old)
            final = f
            claimedFinals.add(f)          // 占住，避免同时传完的两个任务互抢
            renameErr = !rf.partPath.renameTo(f)
            claimedParts.remove(rf.partPath)
            claimedFinals.remove(f)       // 已落盘，后续靠文件存在性判断即可
        }
        if (renameErr) {
            publish(recvTransfer(task, rf, TransferState.FAILED, "重命名失败"))
            return
        }
        ResumeStore.clear(rf.partPath)
        rf.completed = true
        rf.finalPath = final
        publish(recvTransfer(task, rf, TransferState.COMPLETED, null))
        conn.send(
            Messages.envelope(
                "done", cfg.deviceId, task.peerId,
                JSONObject().put("transfer_id", tid).put("file_id", fid),
            ),
        )
        if (task.files.values.all { it.completed }) recvs.remove(tid)
    }

    private fun onDone(env: JSONObject) {
        val data = env.optJSONObject("data") ?: return
        val tid = data.optString("transfer_id")
        val fid = data.optString("file_id")
        val task = sends[tid] ?: return
        task.files.firstOrNull { it.entry.fileId == fid }?.let {
            publish(sendTransfer(task, it, TransferState.COMPLETED, null))
        }
        if (task.files.all { it.completed }) sends.remove(tid)
    }

    private fun onReject(env: JSONObject) {
        val data = env.optJSONObject("data") ?: return
        val tid = data.optString("transfer_id")
        val reason = data.optString("reason")
        val task = sends.remove(tid) ?: return
        for (sf in task.files) publish(sendTransfer(task, sf, TransferState.REJECTED, reason))
    }

    private fun onCancel(env: JSONObject) {
        val data = env.optJSONObject("data") ?: return
        val tid = data.optString("transfer_id")
        recvs.remove(tid)?.let { task ->
            for (rf in task.files.values) publish(recvTransfer(task, rf, TransferState.CANCELLED, null))
            releaseRecv(task)
        }
        sends.remove(tid)?.let { task ->
            for (sf in task.files) publish(sendTransfer(task, sf, TransferState.CANCELLED, null))
        }
    }

    fun cancel(transferId: String) {
        val peerId = sends[transferId]?.peerId ?: recvs[transferId]?.peerId
        if (peerId != null) {
            transport.sendTo(
                peerId,
                Messages.envelope("cancel", cfg.deviceId, peerId, JSONObject().put("transfer_id", transferId)),
            )
        }
        recvs.remove(transferId)?.let { task ->
            for (rf in task.files.values) publish(recvTransfer(task, rf, TransferState.CANCELLED, null))
            releaseRecv(task)
        }
        sends.remove(transferId)?.let { task ->
            for (sf in task.files) publish(sendTransfer(task, sf, TransferState.CANCELLED, null))
        }
    }

    fun handleControl(conn: ControlConnection, env: JSONObject) {
        when (env.optString("type")) {
            "offer" -> onOffer(conn, env)
            "accept" -> onAccept(env)
            "reject" -> onReject(env)
            "complete" -> onComplete(conn, env)
            "done" -> onDone(env)
            "cancel" -> onCancel(env)
            "error" -> onError(env)
            else -> Unit
        }
    }

    private fun onError(env: JSONObject) {
        val data = env.optJSONObject("data") ?: return
        val tid = data.optString("transfer_id")
        val msg = data.optString("message", "error")
        sends[tid]?.let { task -> for (sf in task.files) publish(sendTransfer(task, sf, TransferState.FAILED, msg)) }
        recvs[tid]?.let { task ->
            for (rf in task.files.values) publish(recvTransfer(task, rf, TransferState.FAILED, msg))
            releaseRecv(task)
        }
    }

    // ------------------------------------------------------------ 辅助
    private fun publish(t: Transfer) = bus.publish(Event.TransferEvent(t))

    private fun sendTransfer(task: SendTask, sf: SendFile, state: TransferState, error: String?): Transfer =
        Transfer(
            transferId = task.transferId, peerId = task.peerId, file = sf.entry,
            direction = "send", state = state, sent = sf.sent, received = sf.sent, error = error,
            // 本地原文件位置：UI 上"打开"用（content:// 或文件路径）
            finalPath = sf.source.source.ifEmpty { null },
        )

    private fun recvTransfer(task: RecvTask, rf: RecvFile, state: TransferState, error: String?): Transfer =
        Transfer(
            transferId = task.transferId, peerId = task.peerId, file = rf.entry,
            direction = "recv", state = state,
            received = if (state == TransferState.COMPLETED) rf.entry.size else rf.written,
            receivedRanges = rf.ranges.toList(),
            finalPath = rf.finalPath.absolutePath, error = error,
        )

    private fun sha256File(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        f.inputStream().use { ins ->
            val buf = ByteArray(1 shl 20)
            while (true) {
                val r = ins.read(buf)
                if (r < 0) break
                md.update(buf, 0, r)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    private fun hexToBytes(hex: String): ByteArray {
        if (hex.length < 64) return ByteArray(32)
        return ByteArray(32) {
            ((Character.digit(hex[it * 2], 16) shl 4) + Character.digit(hex[it * 2 + 1], 16)).toByte()
        }
    }
}
