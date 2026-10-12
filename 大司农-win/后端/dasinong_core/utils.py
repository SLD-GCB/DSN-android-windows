"""通用工具：文件类型判断 / 路径清洗 / 字节区间运算 / 格式化。"""
from __future__ import annotations

import mimetypes
import re
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Tuple

ByteRange = Tuple[int, int]

_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"}


# ------------------------------------------------------------------ 文件类型
def guess_kind(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    if mime and mime.startswith("image/"):
        return "image"
    ext = Path(name).suffix.lower()
    if ext in _IMAGE_EXT:
        return "image"
    return "file"


def guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


def is_image(name: str) -> bool:
    return guess_kind(name) == "image"


# ------------------------------------------------------------------ 路径清洗
def sanitize_component(name: str) -> str:
    """清洗单个路径分量：归一化、替换非法字符、去保留名与结尾点/空格。"""
    name = unicodedata.normalize("NFC", name or "")
    name = name.strip().strip(".")
    name = _ILLEGAL_CHARS.sub("_", name)
    if not name or name in (".", ".."):
        return "_"
    stem = name.split(".")[0].upper()
    if stem in _WIN_RESERVED:
        name = "_" + name
    # 限制长度，保留扩展名
    if len(name) > 200:
        p = Path(name)
        keep = 180 - len(p.suffix)
        name = p.stem[:max(1, keep)] + p.suffix
    return name or "_"


def sanitize_rel_path(rel_path: str) -> Path:
    """把协议里的 rel_path 转成本地安全相对路径；拒绝路径穿越。"""
    raw = (rel_path or "").replace("\\", "/")
    parts: List[str] = []
    for piece in PurePosixPath(raw).parts:
        if piece in ("", ".", "..", "/"):
            continue
        cleaned = sanitize_component(piece)
        if cleaned:
            parts.append(cleaned)
    if not parts:
        parts = ["unnamed"]
    return Path(*parts)


def unique_path(path: Path) -> Path:
    """若目标已存在则追加 (1) (2) ...。"""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    i = 1
    while True:
        cand = parent / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


# ------------------------------------------------------------------ 区间运算
def merge_ranges(ranges: Iterable[ByteRange]) -> List[ByteRange]:
    cleaned = sorted((s, e) for s, e in ranges if e >= s)
    merged: List[ByteRange] = []
    for s, e in cleaned:
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def total_range_len(ranges: Iterable[ByteRange]) -> int:
    return sum(e - s + 1 for s, e in ranges)


def complement_ranges(size: int, ranges: Iterable[ByteRange]) -> List[ByteRange]:
    """[0, size) 中未被 ranges 覆盖的区间。"""
    if size <= 0:
        return []
    gaps: List[ByteRange] = []
    cursor = 0
    for s, e in merge_ranges(ranges):
        if s > cursor:
            gaps.append((cursor, min(s, size) - 1))
        cursor = max(cursor, e + 1)
        if cursor >= size:
            break
    if cursor < size:
        gaps.append((cursor, size - 1))
    return [(s, e) for s, e in gaps if e >= s]


# ------------------------------------------------------------------ 格式化
def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(n)
    for u in units:
        if value < 1024 or u == units[-1]:
            return f"{value:.0f} {u}" if u == "B" else f"{value:.1f} {u}"
        value /= 1024
    return f"{n} B"
