"""工具函数单测：字节区间运算 / 路径清洗 / 类型判断。"""
from pathlib import Path

from dasinong_core import utils


def test_merge_ranges():
    assert utils.merge_ranges([(0, 9), (10, 19)]) == [(0, 19)]
    assert utils.merge_ranges([(5, 9), (0, 4)]) == [(0, 9)]
    assert utils.merge_ranges([(0, 3), (8, 10), (4, 7)]) == [(0, 10)]
    assert utils.merge_ranges([]) == []


def test_total_range_len():
    assert utils.total_range_len([(0, 9), (20, 29)]) == 20
    assert utils.total_range_len([]) == 0


def test_complement_ranges():
    assert utils.complement_ranges(100, []) == [(0, 99)]
    assert utils.complement_ranges(100, [(0, 49)]) == [(50, 99)]
    assert utils.complement_ranges(100, [(0, 99)]) == []
    assert utils.complement_ranges(100, [(0, 9), (90, 99)]) == [(10, 89)]
    # 合并相邻后取补
    assert utils.complement_ranges(100, [(10, 19), (0, 9)]) == [(20, 99)]
    assert utils.complement_ranges(0, []) == []


def test_sanitize_component():
    assert utils.sanitize_component("a/b") == "a_b"
    assert utils.sanitize_component('bad:*?"<>|') == "bad_______"
    assert utils.sanitize_component("CON") == "_CON"
    assert utils.sanitize_component("trailing. ") == "trailing"
    assert utils.sanitize_component("") == "_"
    assert utils.sanitize_component("..") == "_"


def test_sanitize_rel_path_blocks_traversal():
    p = utils.sanitize_rel_path("../../etc/passwd")
    assert ".." not in p.parts
    assert p.parts[-1] == "passwd"
    p2 = utils.sanitize_rel_path("项目/2026/报告.pdf")
    assert p2 == Path("项目", "2026", "报告.pdf")


def test_guess_kind():
    assert utils.guess_kind("a.png") == "image"
    assert utils.guess_kind("a.JPEG") == "image"
    assert utils.guess_kind("a.txt") == "file"
    assert utils.guess_kind("a.zip") == "file"


def test_human_size():
    assert utils.human_size(0) == "0 B"
    assert utils.human_size(2048).endswith("KB")
    assert utils.human_size(5 * 1024 * 1024).endswith("MB")


def test_unique_path(tmp_path):
    f = tmp_path / "x.bin"
    assert utils.unique_path(f) == f
    f.write_bytes(b"1")
    assert utils.unique_path(f).name == "x (1).bin"
