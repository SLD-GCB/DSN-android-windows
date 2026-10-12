"""运行时配置：端口、数据目录、接收目录、设备标识。

路径优先级（高 → 低）：
  1. 环境变量（`DASINONG_DATA_DIR` / `DASINONG_RECEIVE_DIR` / …）—— 便于单机跑双实例
  2. **指针文件** `~/Dasinong/config.json`（固定位置，可由设置页写入）
  3. 内置默认值

⚠ 数据目录里放着 SQLite（口令、会话、传输记录），所以**自定义路径不能存进数据库**——
否则改目录后就再也读不到配置了。因此用这个**固定位置的 config.json** 记住自定义路径。
改数据目录需要**重启生效**；改接收目录立即生效。
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

#: 默认数据目录
DEFAULT_DATA_DIR = Path.home() / "Dasinong"
#: 固定位置的"指针"文件（默认永远在这里，不随 data_dir 变化）。
#: 可用 `DASINONG_PREFS` 覆盖（单机双实例/测试时用，避免互相干扰）。
PREFS_PATH = Path(
    os.environ.get("DASINONG_PREFS") or (DEFAULT_DATA_DIR / "config.json")
).expanduser()
#: 默认接收目录名（放在数据目录下）
DEFAULT_RECEIVE_DIRNAME = "Downloads"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_prefs() -> dict:
    """读指针文件；不存在/损坏都回空 dict（不抛）。"""
    try:
        if PREFS_PATH.exists():
            obj = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def save_prefs(**kwargs: object) -> dict:
    """合并写入指针文件（原子写）；返回合并后的内容。"""
    prefs = load_prefs()
    for key, value in kwargs.items():
        if value is None:
            continue
        prefs[key] = str(value)
    try:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PREFS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, PREFS_PATH)
    except OSError:
        pass
    return prefs


def load_or_create_device_id(data_dir: Path) -> str:
    """从数据目录读取持久化 device_id，不存在则生成 UUIDv4 并落盘。"""
    f = data_dir / ".device_id"
    if f.exists():
        value = f.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = str(uuid.uuid4())
    f.write_text(value, encoding="utf-8")
    return value


@dataclass(frozen=True)
class Config:
    udp_port: int
    tcp_port: int
    data_dir: Path
    device_name: str
    device_id: str
    device_type: str = "windows"
    #: 接收目录；不传则默认 `data_dir/Downloads`（见 __post_init__）
    receive_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        # Config 是 frozen 的，只能用 object.__setattr__ 补默认值
        resolved = self.receive_dir if self.receive_dir is not None else self.data_dir / DEFAULT_RECEIVE_DIRNAME
        object.__setattr__(self, "receive_dir", Path(resolved))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dasinong.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.receive_dir.mkdir(parents=True, exist_ok=True)

    def with_receive_dir(self, path: Path) -> "Config":
        """返回换了接收目录的新配置（Config 是 frozen 的）。"""
        new = replace(self, receive_dir=Path(path))
        new.ensure_dirs()
        return new


def _resolve_dir(env_name: str, prefs: dict, pref_key: str, default: Path) -> Path:
    raw = os.environ.get(env_name)
    if raw:
        return Path(raw).expanduser()
    stored = prefs.get(pref_key)
    if stored:
        return Path(str(stored)).expanduser()
    return default


def load_config() -> Config:
    prefs = load_prefs()
    data_dir = _resolve_dir("DASINONG_DATA_DIR", prefs, "data_dir", DEFAULT_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    receive_dir = _resolve_dir(
        "DASINONG_RECEIVE_DIR", prefs, "receive_dir", data_dir / DEFAULT_RECEIVE_DIRNAME
    )
    udp_port = _env_int("DASINONG_UDP_PORT", 37020)
    tcp_port = _env_int("DASINONG_TCP_PORT", 37021)
    device_name = (
        os.environ.get("DASINONG_DEVICE_NAME")
        or prefs.get("device_name")
        or socket.gethostname()
        or "Windows-PC"
    )
    cfg = Config(
        udp_port=udp_port,
        tcp_port=tcp_port,
        data_dir=data_dir,
        device_name=str(device_name),
        device_id=load_or_create_device_id(data_dir),
        receive_dir=receive_dir,
    )
    cfg.ensure_dirs()
    return cfg
