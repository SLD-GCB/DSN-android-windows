package com.dasinong.ui.ui

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.drawable.Drawable
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import com.dasinong.backend.net.LocalFile
import com.dasinong.backend.util.Paths
import java.io.BufferedOutputStream
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/** 一台手机上「可分享」的已安装应用（含其 APK / split APK 路径）。 */
data class InstalledApp(
    val label: String,
    val packageName: String,
    val apkPath: String,
    /** split APK；从 App Bundle 安装的应用会有，单 APK 应用为空。 */
    val splitPaths: List<String>,
    val size: Long,
    val icon: ImageBitmap?,
) {
    val hasSplits: Boolean get() = splitPaths.isNotEmpty()
}

/**
 * 读取已安装应用并转成后端可发送的 LocalFile —— 就像 LocalSend 那样，把手机上装好的
 * 应用（APK）通过局域网发给另一台设备；对方收到后点一下即可安装。
 *
 * - **单 APK 应用**：直接发 `<应用名>.apk`。
 * - **分包应用**（App Bundle 安装、含 split APK）：把 base + 所有 split 打成一个
 *   `<应用名>.apks`（zip）再发，否则只发 base 装不上。接收端用支持分包安装的工具
 *   （如 SAI / adb install-multiple）即可安装。
 *
 * ⚠ 需要 Manifest 里的 `<queries>` 声明（目标 Android 11+ 才看得到其它应用）。
 */
object InstalledApps {

    /** 列出所有「有启动图标」的应用（去重），按名称排序。 */
    fun list(context: Context): List<InstalledApp> {
        val pm = context.packageManager
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val resolved = runCatching { pm.queryIntentActivities(intent, 0) }.getOrDefault(emptyList())
        val seen = HashSet<String>()
        val out = ArrayList<InstalledApp>()
        for (ri in resolved) {
            val ai = ri.activityInfo?.applicationInfo ?: continue
            val pkg = ai.packageName ?: continue
            if (!seen.add(pkg)) continue
            val base = File(ai.sourceDir ?: continue)
            if (!base.isFile) continue
            val splits = (ai.splitSourceDirs ?: emptyArray())
                .filterNotNull().filter { it.isNotEmpty() && File(it).isFile }
            val label = runCatching { ai.loadLabel(pm).toString() }.getOrDefault(pkg)
            val size = base.length() + splits.sumOf { File(it).length() }
            out.add(
                InstalledApp(
                    label = label.ifBlank { pkg },
                    packageName = pkg,
                    apkPath = base.absolutePath,
                    splitPaths = splits,
                    size = size,
                    icon = runCatching { ai.loadIcon(pm).toImageBitmap() }.getOrNull(),
                ),
            )
        }
        return out.sortedBy { it.label.lowercase() }
    }

    /**
     * 准备可发送文件（可能耗时：分包应用要打包）。**请在 IO 线程调用**。
     * 单 APK 应用返回 `<应用名>.apk`；分包应用返回缓存目录里的 `<应用名>.apks`。
     */
    fun prepare(context: Context, app: InstalledApp): LocalFile {
        if (!app.hasSplits) return plainApk(app)
        val apks = buildApks(context, app)
        return LocalFile(
            name = apks.name,
            relPath = apks.name,
            rootName = null,
            size = apks.length(),
            mtime = apks.lastModified() / 1000.0,
            kind = "file",
            open = { FileInputStream(apks) },
            source = apks.absolutePath,
        )
    }

    /** 单个 APK：文件名用「应用名.apk」。 */
    private fun plainApk(app: InstalledApp): LocalFile {
        val apk = File(app.apkPath)
        val safeName = Paths.sanitizeComponent(app.label) + ".apk"
        return LocalFile(
            name = safeName,
            relPath = safeName,
            rootName = null,
            size = apk.length(),
            mtime = apk.lastModified() / 1000.0,
            kind = "file",
            open = { FileInputStream(apk) },
            source = apk.absolutePath,
        )
    }

    /** 把 base + splits 打成一个 zip（扩展名 `.apks`），有缓存就复用。 */
    private fun buildApks(context: Context, app: InstalledApp): File {
        val dir = File(context.cacheDir, "shared-apps").apply { mkdirs() }
        val out = File(dir, Paths.sanitizeComponent(app.label) + ".apks")
        val sources = listOf(app.apkPath) + app.splitPaths
        val newest = sources.maxOfOrNull { File(it).lastModified() } ?: 0L
        if (out.isFile && out.length() > 0 && out.lastModified() >= newest) return out
        ZipOutputStream(BufferedOutputStream(FileOutputStream(out))).use { zos ->
            for (p in sources) {
                val f = File(p)
                if (!f.isFile) continue
                zos.putNextEntry(ZipEntry(f.name))
                FileInputStream(f).use { it.copyTo(zos) }
                zos.closeEntry()
            }
        }
        return out
    }
}

/** Drawable → ImageBitmap（图标展示用；自适应图标等异常情况由调用方兜底）。 */
private fun Drawable.toImageBitmap(): ImageBitmap {
    val w = intrinsicWidth.takeIf { it > 0 } ?: 96
    val h = intrinsicHeight.takeIf { it > 0 } ?: 96
    val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
    val canvas = Canvas(bmp)
    setBounds(0, 0, canvas.width, canvas.height)
    draw(canvas)
    return bmp.asImageBitmap()
}
