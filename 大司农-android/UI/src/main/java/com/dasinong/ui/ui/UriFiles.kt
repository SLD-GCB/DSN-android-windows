package com.dasinong.ui.ui

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import com.dasinong.backend.net.LocalFile
import com.dasinong.backend.util.Paths
import java.io.IOException

/**
 * 把 SAF / Photo Picker 返回的 Uri 转成后端可用的 LocalFile。
 * 不使用 androidx.documentfile，直接走 DocumentsContract（少一个依赖）。
 */
object UriFiles {

    /** 让选中的 Uri 读权限**跨重启保留**（Photo Picker 的可能不支持，失败就忽略）。 */
    private fun persistPermission(context: Context, uri: Uri) {
        runCatching {
            context.contentResolver.takePersistableUriPermission(
                uri,
                android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION,
            )
        }
    }

    fun fromUri(context: Context, uri: Uri): LocalFile? {
        persistPermission(context, uri)
        val resolver = context.contentResolver
        var name = "unnamed"
        var size = 0L
        try {
            resolver.query(uri, null, null, null, null)?.use { c ->
                if (c.moveToFirst()) {
                    val ni = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    val si = c.getColumnIndex(OpenableColumns.SIZE)
                    if (ni >= 0) name = c.getString(ni) ?: name
                    if (si >= 0 && !c.isNull(si)) size = c.getLong(si)
                }
            }
        } catch (_: Exception) {
        }
        return LocalFile(
            name = name,
            relPath = Paths.sanitizeRelPath(name),
            rootName = null,
            size = size,
            mtime = 0.0,
            kind = Paths.kindOf(name),
            source = uri.toString(),
            open = { resolver.openInputStream(uri) ?: throw IOException("无法打开 $uri") },
        )
    }

    fun fromTree(context: Context, treeUri: Uri): List<LocalFile> {
        persistPermission(context, treeUri)
        val rootDocId = try {
            DocumentsContract.getTreeDocumentId(treeUri)
        } catch (_: Exception) {
            return emptyList()
        }
        val rootName = queryName(context, DocumentsContract.buildDocumentUriUsingTree(treeUri, rootDocId))
            ?: "folder"
        val out = ArrayList<LocalFile>()
        walk(context, treeUri, rootDocId, rootName, "", out)
        return out
    }

    private fun walk(
        context: Context,
        treeUri: Uri,
        docId: String,
        rootName: String,
        prefix: String,
        out: MutableList<LocalFile>,
    ) {
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, docId)
        val cursor = try {
            context.contentResolver.query(
                childrenUri,
                arrayOf(
                    DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                    DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                    DocumentsContract.Document.COLUMN_MIME_TYPE,
                    DocumentsContract.Document.COLUMN_SIZE,
                ),
                null, null, null,
            )
        } catch (_: Exception) {
            null
        } ?: return

        cursor.use { c ->
            while (c.moveToNext()) {
                val childId = c.getString(0) ?: continue
                val name = c.getString(1) ?: continue
                val mime = c.getString(2)
                val size = if (c.isNull(3)) 0L else c.getLong(3)
                if (mime == DocumentsContract.Document.MIME_TYPE_DIR) {
                    walk(context, treeUri, childId, rootName, "$prefix$name/", out)
                } else {
                    val fileUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, childId)
                    out.add(
                        LocalFile(
                            name = name,
                            relPath = "$rootName/$prefix$name",
                            rootName = rootName,
                            size = size,
                            mtime = 0.0,
                            kind = Paths.kindOf(name),
                            source = fileUri.toString(),
                            open = {
                                context.contentResolver.openInputStream(fileUri)
                                    ?: throw IOException("无法打开 $name")
                            },
                        ),
                    )
                }
            }
        }
    }

    private fun queryName(context: Context, uri: Uri): String? = try {
        context.contentResolver.query(
            uri, arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME), null, null, null,
        )?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
    } catch (_: Exception) {
        null
    }
}
