"""大司农 Windows 端 GUI 引导。"""
from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from dasinong_core.config import load_config
    from dasinong_core.node import NodeEngine
    from dasinong_core.store import Store

    from .main_window import MainWindow
    from .theme import apply_theme

    cfg = load_config()
    store = Store(cfg.db_path)
    engine = NodeEngine(cfg)
    engine.attach_store(store)

    app = QApplication(sys.argv)
    app.setApplicationName("大司农")
    app.setApplicationDisplayName("大司农")
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)                      # 品牌配色（与安卓端同一套）

    window = MainWindow(engine)
    window.show()
    engine.start()

    try:
        return app.exec()
    finally:
        engine.stop()
        store.close()
