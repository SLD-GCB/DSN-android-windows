"""大司农品牌主题（Windows 端）——与安卓端 Theme.kt 同一套色。

色值直接取自应用图标（软件图标/大司农.jpg）：
  · 墨绿（房身 / 屋脊）：#1E4346 → 主色
  · 金（中央旋纹）      ：#A88347 → 强调色
  · 奶白（图标底色）    ：#FBF6E6 / #FFFDF4 → 背景与表面
"""
from __future__ import annotations

# ---------------------------------------------------------------- 调色板
TEAL = "#1E4346"          # 主色（墨绿）
TEAL_DARK = "#0C2A2B"     # 主色上的深字
TEAL_SOFT = "#CFE0DE"     # 主色浅调（选中/出站气泡）
TEAL_HOVER = "#245354"    # 主色 hover
GOLD = "#A88347"          # 强调色（金）
GOLD_SOFT = "#F2E4C4"
GREEN = "#2F6E63"         # 状态好（与主色同族）
GREEN_SOFT = "#CBE6E0"

CREAM = "#FBF6E6"         # 背景
CREAM_DEEP = "#F0EAD8"    # 状态栏 / 表头
SURFACE = "#FFFDF4"       # 卡片 / 输入框
SURFACE_VAR = "#E7E1CE"   # 入站气泡
HOVER = "#F1EAD6"
BORDER = "#CFC9B6"
OUTLINE = "#7E7B6C"
MUTED = "#6E6B60"         # 次要说明文字
INK = "#1B1C19"           # 正文
ERROR = "#B3261E"

#: 气泡里那行状态小字（已发送 / 已送达…）
STATUS_TEXT = MUTED

FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif'


STYLESHEET = f"""
* {{
    font-family: {FONT_FAMILY};
}}

QWidget {{
    background-color: {CREAM};
    color: {INK};
    font-size: 13px;
}}

QMainWindow, QDialog {{ background-color: {CREAM}; }}
QLabel {{ background: transparent; }}

/* ---------------- 页签 ---------------- */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    background: {SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    padding: 7px 16px;
    margin-right: 4px;
    border: 1px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}}
QTabBar::tab:selected {{
    background: {SURFACE};
    color: {TEAL};
    border-color: {BORDER};
    border-bottom-color: {SURFACE};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{ color: {TEAL}; }}

/* ---------------- 按钮 ---------------- */
QPushButton {{
    background: {SURFACE};
    color: {TEAL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 14px;
    min-height: 18px;
}}
QPushButton:hover {{ background: {HOVER}; border-color: {OUTLINE}; }}
QPushButton:pressed {{ background: {TEAL_SOFT}; }}
QPushButton:default {{
    background: {TEAL};
    color: #FFFFFF;
    border-color: {TEAL};
    font-weight: 600;
}}
QPushButton:default:hover {{ background: {TEAL_HOVER}; }}
QPushButton:disabled {{ color: #A9A498; border-color: #DEDAC9; }}

/* ---------------- 输入 ---------------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: {TEAL_SOFT};
    selection-color: {TEAL_DARK};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QComboBox:focus {{ border-color: {TEAL}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}

/* ---------------- 列表 / 表格 ---------------- */
QListWidget, QTableWidget, QTreeWidget {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    outline: none;
    alternate-background-color: #FCF8EC;
}}
QListWidget::item {{
    padding: 6px 8px;
    border-radius: 6px;
    margin: 2px 4px;
}}
QListWidget::item:hover:!selected {{ background: {HOVER}; }}
QListWidget::item:selected {{ background: {TEAL_SOFT}; color: {TEAL_DARK}; }}
QTableWidget {{ gridline-color: #EDE7D4; }}
QTableWidget::item {{ padding: 4px 6px; }}
QTableWidget::item:selected {{ background: {TEAL_SOFT}; color: {TEAL_DARK}; }}
QHeaderView::section {{
    background: {CREAM_DEEP};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    font-weight: 600;
}}
QTableCornerButton::section {{ background: {CREAM_DEEP}; border: none; }}

/* ---------------- 滚动条 ---------------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px; }}
QScrollBar::handle:vertical {{ background: #D8D2BE; border-radius: 5px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: #C4BCA4; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 4px; }}
QScrollBar::handle:horizontal {{ background: #D8D2BE; border-radius: 5px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: #C4BCA4; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------------- 其他 ---------------- */
QScrollArea {{ border: none; background: {SURFACE}; }}
QSplitter::handle {{ background: #EDE7D4; }}
QStatusBar {{ background: {CREAM_DEEP}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {TEAL_SOFT}; color: {TEAL_DARK}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QToolTip {{
    background: {TEAL};
    color: #FFFFFF;
    border: none;
    padding: 5px 9px;
    border-radius: 6px;
}}
QProgressBar {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
    height: 14px;
}}
QProgressBar::chunk {{ background: {TEAL}; border-radius: 5px; }}
QMessageBox {{ background: {CREAM}; }}
QMessageBox QLabel {{ color: {INK}; }}
"""


def apply_theme(app) -> None:
    """给 QApplication 套上品牌样式（Fusion 保证 QSS 在各 Windows 版本上表现一致）。"""
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    app.setStyleSheet(STYLESHEET)
