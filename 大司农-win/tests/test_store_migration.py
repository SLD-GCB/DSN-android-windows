"""store.py 的 `PRAGMA user_version` 迁移框架与 `paired` 表。

重点回归：**老库（从未设过 user_version）打开后要能平滑升到当前版本且老数据不丢。**
"""
import sqlite3

from dasinong_core.store import _SCHEMA_VERSION, Store


def _make_legacy_db(path) -> None:
    """造一个「老库」：有基线表、但从未设置过 `user_version`（=0）。"""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS message (
            msg_id TEXT PRIMARY KEY,
            conv_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT,
            ts REAL NOT NULL,
            outgoing INTEGER NOT NULL,
            status TEXT,
            transfer_id TEXT,
            file_json TEXT
        );
        """
    )
    conn.execute("INSERT INTO setting(key,value) VALUES('passphrase','old-value')")
    conn.commit()
    conn.close()


def test_fresh_db_lands_on_current_version(tmp_path):
    store = Store(tmp_path / "new.db")
    assert store.schema_version() == _SCHEMA_VERSION
    assert store.pair_all() == []


def test_legacy_db_migrates_without_losing_data(tmp_path):
    db = tmp_path / "legacy.db"
    _make_legacy_db(db)

    store = Store(db)

    assert store.schema_version() == _SCHEMA_VERSION
    assert store.setting_get("passphrase") == "old-value"     # 老数据还在
    store.pair_upsert("dev-1", "fp-1", "手机A")               # paired 表已可用
    assert store.pair_get("dev-1")["fp"] == "fp-1"


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "x.db"
    Store(db).close()
    again = Store(db)
    assert again.schema_version() == _SCHEMA_VERSION
    again.close()


def test_legacy_paired_table_is_dropped_and_replaced(tmp_path):
    """历史坑：早期 v4 时代建过**列不一样**的同名 `paired` 表。

    现在的策略是**换新表名** `paired_v2` + 每次打开把旧表删掉 —— 跟历史彻底脱钩。
    以前"就地自检重建"那条路在真机上没能解决问题，所以不再依赖旧表长什么样。
    """
    db = tmp_path / "v4.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE paired (
            device_id   TEXT PRIMARY KEY,
            fingerprint TEXT,          -- 旧列名（现在叫 fp）
            name        TEXT,
            paired_at   REAL,          -- 旧列名（现在叫 first_paired_at）
            trusted     INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO paired(device_id,fingerprint,name,paired_at,trusted)"
        " VALUES('old-dev','fp-old','X',1.0,1)"
    )
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()

    store = Store(db)

    assert store.pair_all() == []          # 新表是空的（旧行随旧表一起被删）
    store.pair_upsert("d1", "fp1", "A")    # 新表可正常读写
    assert store.pair_is_trusted("d1", "fp1") is True
    assert store.schema_version() == _SCHEMA_VERSION

    # 旧表确实被删掉了，新表存在
    check = sqlite3.connect(str(db))
    tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check.close()
    assert "paired" not in tables, tables
    assert "paired_v2" in tables, tables


def test_pair_crud_and_trust(tmp_path):
    store = Store(tmp_path / "p.db")

    assert store.pair_is_trusted("d1", "fpA") is False        # 还没配对
    store.pair_upsert("d1", "fpA", "A")
    assert store.pair_is_trusted("d1", "fpA") is True
    assert store.pair_is_trusted("d1", "fpB") is False        # 指纹变了 → 不再信任
    assert store.pair_is_trusted("d2", "fpA") is False

    store.pair_upsert("d1", "fpB", "A2")                      # 重新配对（覆盖）
    row = store.pair_get("d1")
    assert row["fp"] == "fpB" and row["name"] == "A2"
    assert row["trusted"] == 1
    assert len(store.pair_all()) == 1

    store.pair_remove("d1")
    assert store.pair_all() == []
