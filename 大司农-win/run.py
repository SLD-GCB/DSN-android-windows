"""大司农 Windows 端入口。

把中文目录「后端」「UI」加入 sys.path，再启动 GUI。
后端包名为 dasinong_core，UI 包名为 dasinong_ui（均为 ASCII）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _sub in ("后端", "UI"):
    _p = str(ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main() -> int:
    from dasinong_ui.app import main as ui_main

    return ui_main()


if __name__ == "__main__":
    raise SystemExit(main())
