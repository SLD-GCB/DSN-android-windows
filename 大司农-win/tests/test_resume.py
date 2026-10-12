"""断点续传元数据（.partmeta）读写单测。"""
from dasinong_core.network import resume as resumemod


def test_meta_roundtrip(tmp_path):
    part = tmp_path / "file.bin.part"
    meta = resumemod.PartMeta(
        transfer_id="t1", file_id="f1", name="文件.bin", size=100,
        sha256="a" * 64, chunk_size=65536, total_chunks=1,
        received=[(0, 49), (60, 99)],
    )
    resumemod.save(part, meta)
    loaded = resumemod.load(part)
    assert loaded is not None
    assert loaded.file_id == "f1"
    assert loaded.name == "文件.bin"
    assert loaded.size == 100
    assert loaded.sha256 == "a" * 64
    assert loaded.received == [(0, 49), (60, 99)]


def test_load_missing_returns_none(tmp_path):
    assert resumemod.load(tmp_path / "nope.part") is None


def test_clear(tmp_path):
    part = tmp_path / "f.part"
    resumemod.save(part, resumemod.PartMeta(
        transfer_id="t", file_id="f", name="f", size=1, sha256="x",
    ))
    assert resumemod.meta_path(part).exists()
    resumemod.clear(part)
    assert not resumemod.meta_path(part).exists()


def test_corrupt_meta_returns_none(tmp_path):
    part = tmp_path / "f.part"
    resumemod.meta_path(part).write_text("{not json", encoding="utf-8")
    assert resumemod.load(part) is None
