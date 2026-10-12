"""pytest 引导：
1. 把中文目录「后端」加入 sys.path，使 dasinong_core 可导入；
2. **测试隔离**：把 prefs（记住的设备地址等）指到临时文件，别碰用户真实的
   `~/Dasinong/config.json` —— 否则跑一次测试就会往真实配置里写脏数据，
   甚至让用户下次启动去直连 127.0.0.1 这种测试地址。
   必须在导入 dasinong_core.config 之前设置（PREFS_PATH 是导入期算好的）。
"""
import os
import sys
import tempfile
from pathlib import Path

if not os.environ.get("DASINONG_PREFS"):
    os.environ["DASINONG_PREFS"] = str(
        Path(tempfile.mkdtemp(prefix="dasinong-test-prefs-")) / "config.json"
    )

# v4 起首次连接要**人工确认配对**（两端显示同一个 6 位短码）。测试里没人点按钮，
# 所以让它自动接受 —— 这是配对模块专门留给无头环境的开关，真实运行绝不设置。
os.environ.setdefault("DASINONG_PAIR_AUTO", "1")

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "后端"
UI = ROOT / "UI"
for p in (BACKEND, UI):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
