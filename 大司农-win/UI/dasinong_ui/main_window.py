"""大司农 Windows 主窗口：附近 / 会话 / 传输 / 设置 四页 + 托盘 + 拖拽发送。"""
from __future__ import annotations

import html
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import QCheckBox, QSpinBox
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from dasinong_core.config import Config, load_prefs, save_prefs
from dasinong_core.models import ChatMessage, MsgKind, Peer, Transfer, TransferState
from dasinong_core.node import NodeEngine
from dasinong_core.pairing import PAIR_TIMEOUT
from dasinong_core.protocol.constants import PROTO_VERSION

from . import theme
from dasinong_core.utils import human_size

from .bridge import Bridge

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_STATE_TEXT = {
    TransferState.PENDING: "等待",
    TransferState.ACTIVE: "传输中",
    TransferState.PAUSED: "已暂停(可续传)",
    TransferState.COMPLETED: "完成",
    TransferState.FAILED: "失败",
    TransferState.REJECTED: "被拒绝",
    TransferState.CANCELLED: "已取消",
}

_MSG_STATUS_TEXT = {
    "pending": "待发送",
    "sent": "已发送",
    "delivered": "已送达",
    "received": "已接收",
    "failed": "发送失败",
}

#: 开机自启在 HKCU\...\Run 里的值名
_AUTOSTART_VALUE = "Dasinong"

# 用 -EncodedCommand 执行，避免中文/引号在命令行里被破坏。
# 按程序查（服务端过滤，约 2 秒）——Windows 弹窗「允许访问」生成的正是这种规则，
# 名字通常是 "Python"/"大司农" 而不是我们命名的 Dasinong UDP/TCP，按名字查会误报。
# 注意：不要改成逐条查询「端口过滤器」，那样要遍历全部规则，会慢到几十秒。
_PS_FW_CHECK = r'''
$ErrorActionPreference = "SilentlyContinue"
$rules = Get-NetFirewallApplicationFilter -Program "{exe}" |
    Get-NetFirewallRule |
    Where-Object {{ $_.Direction -eq "Inbound" -and $_.Action -eq "Allow" -and $_.Enabled -eq "True" }}
if ($rules) {{
    "PROGRAM:" + (($rules | Select-Object -First 3 -ExpandProperty DisplayName) -join " / ")
}} else {{
    "NONE"
}}
'''


class PairingDialog(QDialog):
    """配对确认：两端显示**同一个 6 位数字**，核对一致才点「配对」。

    刻意**不显示任何指纹**（用户硬要求）—— 6 位码是本次会话的临时值，不是身份指纹。
    """

    def __init__(self, peer_name: str, sas: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("配对确认")
        self.setModal(True)
        self._left = max(1, int(PAIR_TIMEOUT))          # 与后端等待超时对齐
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"设备「{peer_name}」请求与你配对。"))
        layout.addWidget(QLabel("请核对两台设备屏幕上显示的数字是否**完全相同**："))
        code = QLabel(" ".join(sas) if sas else "——")
        code.setAlignment(Qt.AlignCenter)
        code.setStyleSheet("font-size:34px; font-weight:bold; letter-spacing:6px;")
        layout.addWidget(code)
        self._tip = QLabel("")
        self._tip.setWordWrap(True)
        layout.addWidget(self._tip)
        self._render_tip()
        row = QHBoxLayout()
        ok = QPushButton("配对")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        layout.addLayout(row)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _render_tip(self) -> None:
        self._tip.setText(
            "一致 → 点「配对」；**不一致**说明有人在中间冒充，请点「取消」。\n"
            f"（{self._left} 秒内未确认将自动取消）"
        )

    def _tick(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self.reject()
            return
        self._render_tip()


class _PillSwitch(QCheckBox):
    """胶囊滑块开关（自绘药丸轨道 + 圆钮），观感与手机端一致。"""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(24)
        self.setMinimumWidth(124)

    def paintEvent(self, _event) -> None:  # noqa: N802
        from PySide6.QtGui import QBrush, QColor, QPainter, QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h, w = 18, 36
        top = (self.height() - h) // 2
        on = self.isChecked()
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor("#2e7d32") if on else QColor("#bdbdbd")))
        p.drawRoundedRect(0, top, w, h, h / 2, h / 2)
        d = h - 4
        x = (w - d - 2) if on else 2
        p.setBrush(QBrush(QColor("#ffffff")))
        p.setPen(QPen(QColor("#9e9e9e"), 1))
        p.drawEllipse(x, top + 2, d, d)
        p.setPen(QPen(QColor("#333333")))
        p.drawText(w + 8, 0, self.width() - w - 8, self.height(),
                   Qt.AlignVCenter | Qt.AlignLeft, self.text())
        p.end()


class MainWindow(QMainWindow):
    diagnosisDone = Signal(str)
    firewallStatusReady = Signal(bool, str)   # (是否已放行, 说明)
    directConnectDone = Signal(bool, str, str)  # (是否成功, ip, 说明)

    def __init__(self, engine: NodeEngine) -> None:
        super().__init__()
        self.engine = engine
        self.cfg: Config = engine.cfg
        self.bridge = Bridge(engine.bus)

        self._peers: Dict[str, Peer] = {}
        self._current_conv: Optional[str] = None
        self._transfer_rows: Dict[str, int] = {}      # file_id -> row
        # 传输速度/用时统计：file_id -> {start, bytes, at, speed}
        self._transfer_stat: Dict[str, dict] = {}
        self._transfer_row_fid: Dict[int, str] = {}   # row -> file_id（双击定位用）
        self._transfer_paths: Dict[str, str] = {}     # file_id -> 本地路径（用于打开/预览）
        self._bubble_by_msg: Dict[str, tuple] = {}    # msg_id -> (气泡控件, 不含状态的正文)
        self._status_labels: Dict[str, QLabel] = {}   # 设置页「运行状态」各行的值标签
        self._transfer_notified: set = set()          # 已提示过的 (file_id, state)
        self._tray_progress_at: Dict[str, float] = {}  # file_id -> 上次刷新 tooltip 的时间
        self._tray_pct: Dict[str, int] = {}            # file_id -> 上次的整数百分比
        self._really_quit = False

        self.setWindowTitle(f"大司农 — {self.cfg.device_name}")
        self.setWindowIcon(self._load_icon())
        self.resize(980, 660)
        self.setAcceptDrops(True)

        self._build_ui()
        self._connect_bridge()
        self._setup_tray()
        self._refresh_nearby()
        self._load_transfer_history()   # 回填历史传输记录（否则关掉重开就看不到）

        self._warned_no_conn = False
        self._logs: list = []
        self.diagnosisDone.connect(self._show_diagnosis)
        self.directConnectDone.connect(self._on_direct_connect_done)
        self.firewallStatusReady.connect(self._update_firewall_line)
        self._conn_timer = QTimer(self)
        self._conn_timer.timeout.connect(self._tick)
        self._conn_timer.start(2000)

        # 启动后自动探测一次防火墙状态；结果直接写进「设置 → 运行状态」，不再弹窗打扰
        self._fw_state: Optional[tuple] = None     # 最近一次 (是否放行, 说明)
        self._fw_probing = False
        self._refresh_status_lines()
        QTimer.singleShot(1200, self._refresh_firewall_async)

        # 开机自启：exe 换过位置时把注册表里的路径改正（静默，不打扰）
        if getattr(sys, "frozen", False) and load_prefs().get("autostart") == "1":
            self._sync_autostart_registry(True, quiet=True)

    # ------------------------------------------------------------- 构建 UI
    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_nearby_tab(), "附近")
        self.tabs.addTab(self._build_chat_tab(), "会话")
        self.tabs.addTab(self._build_transfer_tab(), "传输")
        self.tabs.addTab(self._build_settings_tab(), "设置")
        self.setCentralWidget(self.tabs)

        bar = self.statusBar()
        self._status_default = (
            f"本机：{self.cfg.device_name}  协议 v{PROTO_VERSION}  "
            f"UDP {self.cfg.udp_port} / TCP {self.cfg.tcp_port}"
        )
        bar.showMessage(self._status_default)

    def _build_nearby_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        hint = QLabel("同一局域网（含手机热点）下自动发现设备。双击设备打开会话；也可以直接把文件/文件夹**拖进窗口**发送。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.device_list = QListWidget()
        # 让拖拽事件冒泡到主窗口（否则列表会把 drop 吃掉）
        self.device_list.setAcceptDrops(False)
        self.device_list.itemDoubleClicked.connect(lambda _i: self._open_current_conv())
        layout.addWidget(self.device_list, 1)

        row = QHBoxLayout()
        for text, slot in (
            ("打开会话", self._open_current_conv),
            ("发送文件", lambda: self._send_dialog(folder=False)),
            ("发送文件夹", lambda: self._send_dialog(folder=True)),
            ("手动添加 IP", self._manual_add),
            ("配对选中设备", self._pair_selected),
            ("刷新", self._refresh_all),
            ("连接选中设备", self._connect_selected),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        # 胶囊滑块开关：「保持长连接」（观感与手机端一致）
        self.keepalive_btn = _PillSwitch("保持长连接")
        self.keepalive_btn.setChecked(bool(self.engine.keep_alive()))
        self.keepalive_btn.setToolTip("开启后：心跳更密、空闲不断开，长时间保持连接")
        self.keepalive_btn.toggled.connect(self._toggle_keep_alive)
        row.addWidget(self.keepalive_btn)
        layout.addLayout(row)
        return w

    def _build_chat_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.conv_list = QListWidget()
        self.conv_list.setAcceptDrops(False)   # 让 drop 冒泡到主窗口（拖文件即发送）
        self.conv_list.currentItemChanged.connect(self._on_conv_selected)
        self.conv_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.conv_list.customContextMenuRequested.connect(self._conv_context_menu)
        ll.addWidget(self.conv_list, 1)
        conv_btns = QHBoxLayout()
        new_conv = QPushButton("新建会话")
        new_conv.clicked.connect(self._new_conversation)
        conv_btns.addWidget(new_conv)
        del_conv = QPushButton("删除会话")
        del_conv.clicked.connect(self._delete_conversation)
        conv_btns.addWidget(del_conv)
        ll.addLayout(conv_btns)
        splitter.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        title_row = QHBoxLayout()
        self.chat_title = QLabel("未选择会话")
        title_row.addWidget(self.chat_title, 1)
        clear_btn = QPushButton("清空聊天")
        clear_btn.setToolTip("清空当前会话的全部聊天记录（只删本机记录，不影响对方）")
        clear_btn.clicked.connect(self._clear_chat_history)
        title_row.addWidget(clear_btn)
        rl.addLayout(title_row)
        # 消息区：用「滚动区 + 气泡控件」而不是 QTextBrowser ——
        # QTextBrowser 不支持 display:inline-block / max-width，做不出手机那种气泡。
        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_container = QWidget()
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setAlignment(Qt.AlignTop)
        self.chat_layout.setContentsMargins(6, 6, 6, 6)
        self.chat_layout.setSpacing(6)
        self.chat_scroll.setWidget(self.chat_container)
        rl.addWidget(self.chat_scroll, 1)

        input_row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("输入消息，回车发送（也可以把文件拖进窗口）")
        self.input.setAcceptDrops(False)   # 否则拖进来的文件会被当成路径粘进输入框
        self.input.returnPressed.connect(self._send_text)
        input_row.addWidget(self.input, 1)
        for text, slot in (
            ("发送", self._send_text),
            ("图片", lambda: self._send_dialog(folder=False, images_only=True)),
            ("文件", lambda: self._send_dialog(folder=False)),
            ("文件夹", lambda: self._send_dialog(folder=True)),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            input_row.addWidget(btn)
        rl.addLayout(input_row)

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)
        return w

    def _build_transfer_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        self.transfer_table = QTableWidget(0, 7)
        self.transfer_table.setHorizontalHeaderLabels(
            ["名称", "设备", "方向", "大小", "进度", "状态", "操作"]
        )
        self.transfer_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.transfer_table.verticalHeader().setVisible(False)
        self.transfer_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.transfer_table.setAcceptDrops(False)   # 让 drop 冒泡到主窗口
        self.transfer_table.itemDoubleClicked.connect(self._on_transfer_double_clicked)
        self.transfer_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.transfer_table.customContextMenuRequested.connect(self._transfer_context_menu)
        layout.addWidget(self.transfer_table)

        row = QHBoxLayout()
        open_btn = QPushButton("打开选中的文件")
        open_btn.clicked.connect(self._open_selected_transfer)
        row.addWidget(open_btn)
        exp_btn = QPushButton("导出到…")
        exp_btn.setToolTip("把选中的文件另存到你自选的路径")
        exp_btn.clicked.connect(self._export_selected_transfer)
        row.addWidget(exp_btn)
        del_btn = QPushButton("删除文件")
        del_btn.clicked.connect(self._delete_selected_transfer)
        row.addWidget(del_btn)
        clear_btn = QPushButton("一键清空")
        clear_btn.clicked.connect(self._clear_transfers)
        row.addWidget(clear_btn)
        hint = QLabel("双击可直接打开；「删除文件」删单条，「一键清空」清空列表 —— 都会把已下载到本机的文件一并删掉（不可恢复），进行中的任务不受影响")
        hint.setWordWrap(True)
        row.addWidget(hint, 1)
        layout.addLayout(row)
        return w

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # 运行状态：几行信息一览（不再弹窗打扰）
        layout.addWidget(QLabel("— 运行状态 —"))
        self._add_status_row(layout, "device", "设备名称")
        self._add_status_row(layout, "proto", "协议版本")
        self._add_status_row(layout, "listen", "本机监听")
        self._add_status_row(layout, "firewall", "防火墙")
        self._add_status_row(layout, "tunnel", "加密隧道")
        self._add_status_row(layout, "peers", "当前设备")

        # 文件目录（可自定义）
        layout.addWidget(QLabel("— 文件目录 —"))
        self._add_status_row(layout, "receive", "接收目录", (
            ("选择…", self._pick_receive_dir),
            ("打开", lambda: self._open_dir(self.cfg.receive_dir)),
        ))
        self._add_status_row(layout, "data", "数据目录", (
            ("选择…", self._pick_data_dir),
            ("打开", lambda: self._open_dir(self.cfg.data_dir)),
        ))

        # 已配对设备（v4：每设备独立身份 + 配对确认；**没有口令这个概念了**）
        layout.addWidget(QLabel("— 已配对设备 —"))
        self.paired_list = QListWidget()
        self.paired_list.setMaximumHeight(120)
        layout.addWidget(self.paired_list)
        paired_row = QHBoxLayout()
        unpair_btn = QPushButton("解绑选中设备")
        unpair_btn.setToolTip("解绑后该设备下次连接需要重新配对")
        unpair_btn.clicked.connect(self._unpair_selected)
        paired_row.addWidget(unpair_btn)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh_paired)
        paired_row.addWidget(refresh_btn)
        paired_row.addStretch(1)
        layout.addLayout(paired_row)
        paired_hint = QLabel(
            "首次与某台设备连接时，两端会显示**同一个 6 位数字**；\n"
            "核对一致后两边各点一次「配对」，才建立长期信任，直到你在这里解绑。\n"
            "加密始终开启，不需要设置口令。"
        )
        paired_hint.setWordWrap(True)
        layout.addWidget(paired_hint)

        # 后台驻留：关窗口不退出（托盘常驻），这里可选开机自启
        layout.addWidget(QLabel("— 连接时长 —"))
        ct_row = QHBoxLayout()
        ct_row.addWidget(QLabel("连接闲置多少分钟后自动关闭："))
        self.conn_timeout_spin = QSpinBox()
        self.conn_timeout_spin.setRange(1, 24 * 60)
        self.conn_timeout_spin.setSuffix(" 分钟")
        self.conn_timeout_spin.setValue(int(self.engine.conn_timeout_minutes()))
        self.conn_timeout_spin.setToolTip("「保持长连接」打开时忽略此项（无限期保持）")
        self.conn_timeout_spin.valueChanged.connect(
            lambda v: self.engine.set_conn_timeout_minutes(int(v)))
        ct_row.addWidget(self.conn_timeout_spin)
        ct_row.addStretch(1)
        layout.addLayout(ct_row)
        layout.addWidget(QLabel("说明：连接空闲超过这个时间会被关闭（省资源）；"
                                "打开「保持长连接」则**无限期保持**，不会被关。"))

        cc_row = QHBoxLayout()
        cc_row.addWidget(QLabel("TCP 并发（一次传输并发几条数据连接）："))
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 8)
        self.concurrency_spin.setSuffix(" 条")
        self.concurrency_spin.setValue(int(self.engine.concurrency()))
        self.concurrency_spin.setToolTip("配合接口池：多设备/大文件时更快；填 1 = 单连接")
        self.concurrency_spin.valueChanged.connect(
            lambda v: self.engine.set_concurrency(int(v)))
        cc_row.addWidget(self.concurrency_spin)
        cc_row.addStretch(1)
        layout.addLayout(cc_row)

        layout.addWidget(QLabel("— 后台驻留 —"))
        self.autostart_check = QCheckBox("开机自启动（登录后自动运行）")
        self.autostart_check.setChecked(load_prefs().get("autostart") == "1")
        self.autostart_check.toggled.connect(self._toggle_autostart)
        if not getattr(sys, "frozen", False):
            self.autostart_check.setEnabled(False)
            self.autostart_check.setToolTip("仅打包后的 exe 生效（开发模式不可用）")
        layout.addWidget(self.autostart_check)
        tray_hint = QLabel("关闭窗口不会退出程序，会缩到托盘继续收发消息/文件；退出请用托盘菜单「退出」。")
        tray_hint.setWordWrap(True)
        layout.addWidget(tray_hint)

        # 工具
        row = QHBoxLayout()
        for text, slot in (
            ("重新检测防火墙", self._refresh_firewall_async),
            ("一键放行防火墙", self._firewall_allow),
            ("网络诊断", self._diagnose),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return w

    def _add_status_row(self, layout, key: str, title: str, buttons=()) -> None:
        """一行：标题 + 值 + 可选按钮；值标签存进 self._status_labels 供刷新。"""
        row = QHBoxLayout()
        t = QLabel(title)
        t.setMinimumWidth(88)
        row.addWidget(t)
        val = QLabel("—")
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(val, 1)
        for text, slot in buttons:
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        layout.addLayout(row)
        self._status_labels[key] = val

    @staticmethod
    def _open_dir(path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ------------------------------------------------------------- 运行状态
    def _refresh_status_lines(self) -> None:
        self._set_status("device", self.cfg.device_name)
        self._set_status("proto", f"v{PROTO_VERSION}")
        self._refresh_listen_line()
        self._refresh_tunnel_line()
        self._set_status("receive", str(self.cfg.receive_dir))
        self._set_status("data", str(self.cfg.data_dir))
        self._refresh_peers_line()
        self._refresh_firewall_line()
        self._refresh_paired()

    def _refresh_listen_line(self) -> None:
        transport = getattr(self.engine, "transport", None)
        listening = bool(transport is not None and getattr(transport, "is_listening", False))
        self._set_status(
            "listen",
            f"UDP {self.cfg.udp_port} / TCP {self.cfg.tcp_port} · "
            + ("运行中" if listening else "未运行（端口可能被占用）"),
        )

    def _set_status(self, key: str, text: str) -> None:
        label = self._status_labels.get(key)
        if label is not None:
            label.setText(text)

    def _refresh_peers_line(self) -> None:
        peers = self.engine.peers()
        connected = sum(1 for p in peers if self.engine.is_connected(p.device_id))
        self._set_status("peers", f"发现 {len(peers)} 台 · 已连接 {connected} 台")

    def _refresh_tunnel_line(self) -> None:
        """只显示加密状态 —— 不显示指纹等任何敏感信息。

        v4 起加密**始终开启**（每设备独立身份），所以这一行恒为「已加密」。
        """
        label = self._status_labels.get("tunnel")
        if label is None:
            return
        label.setText("已加密（强制 · 每设备独立身份）")
        label.setStyleSheet(f"color:{theme.GREEN};")

    def _refresh_firewall_line(self) -> None:
        label = self._status_labels.get("firewall")
        if label is None:
            return
        if sys.platform != "win32":
            label.setText("非 Windows，无需放行")
            label.setStyleSheet(f"color:{theme.MUTED};")
            return
        if self._fw_state is None:
            label.setText("检测中…")
            label.setStyleSheet(f"color:{theme.MUTED};")
            return
        allowed, _detail = self._fw_state
        if allowed:
            label.setText("已放行")
            label.setStyleSheet(f"color:{theme.GREEN};")
        else:
            label.setText("没放行（点下面「一键放行防火墙」）")
            label.setStyleSheet(f"color:{theme.GOLD};")

    def _load_icon(self) -> QIcon:
        """按 打包环境 → 可执行文件旁 → 开发目录 的顺序找图标。"""
        candidates: list[Path] = []
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
            candidates += [base / "大司农.jpg", base / "大司农.ico"]
            exe_dir = Path(sys.executable).parent
            candidates += [exe_dir / "大司农.ico", exe_dir / "大司农.jpg"]
        root = Path(__file__).resolve().parents[3]
        candidates += [
            root / "软件图标" / "大司农.jpg",
            root / "软件图标" / "大司农.png",
            root / "软件图标" / "大司农.ico",
        ]
        for cand in candidates:
            if cand.exists():
                return QIcon(str(cand))
        return QIcon()

    # ------------------------------------------------------------- 托盘
    def _setup_tray(self) -> None:
        icon = self.windowIcon()
        if icon.isNull():
            icon = QIcon.fromTheme("applications-internet")
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self._show_window)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._show_window() if r == QSystemTrayIcon.DoubleClick else None)
        self.tray.messageClicked.connect(self._show_window)   # 点通知气泡也把窗口唤出来
        self.tray.setToolTip(self._idle_tooltip())
        self.tray.show()

    def _idle_tooltip(self) -> str:
        return f"大司农 — {self.cfg.device_name} · 运行中"

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _notify(self, title: str, body: str) -> None:
        try:
            self.tray.showMessage(title, body, self.windowIcon(), 4000)
        except Exception:
            pass

    def _update_tray_progress(self, fid: str, t: Transfer) -> None:
        """传输进度只写在托盘 tooltip 上（不弹气泡）；≥1 秒且整数百分比变了才更新。"""
        now = time.time()
        total = t.file.size if t.file else 0
        done = t.received if t.direction == "recv" else t.sent
        pct = int(100 * done / total) if total else 0
        if self._tray_pct.get(fid) == pct and now - self._tray_progress_at.get(fid, 0.0) < 1.0:
            return
        if now - self._tray_progress_at.get(fid, 0.0) < 1.0:
            return
        self._tray_progress_at[fid] = now
        self._tray_pct[fid] = pct
        verb = "接收" if t.direction == "recv" else "发送"
        name = (t.file.name if t.file else fid)
        try:
            self.tray.setToolTip(f"大司农 — {verb}中 {pct}% · {name[:24]}")
        except Exception:
            pass

    # ------------------------------------------------------------- 信号连接
    def _connect_bridge(self) -> None:
        self.bridge.peerAdded.connect(self._on_peer_changed)
        self.bridge.peerUpdated.connect(self._on_peer_changed)
        self.bridge.peerRemoved.connect(self._on_peer_removed)
        self.bridge.messageEvent.connect(self._on_message)
        self.bridge.messageStatus.connect(self._on_message_status)
        self.bridge.transferEvent.connect(self._on_transfer)
        self.bridge.conversationRebound.connect(self._on_conv_rebound)
        self.bridge.pairRequest.connect(self._on_pair_request)
        self.bridge.pairedChanged.connect(self._on_paired_changed)
        self.bridge.log.connect(self._on_log)

    def _on_conv_rebound(self, old_id: str, new_id: str) -> None:
        """手动 IP 会话已升级成真实设备：切到新会话并刷新列表。"""
        if self._current_conv == old_id:
            self._current_conv = new_id
            self._select_conv(new_id)
        else:
            self._refresh_convs()

    def _on_log(self, text: str) -> None:
        """后端日志（含隧道握手失败、口令变更等）→ 状态栏 + 留档给诊断。"""
        self._logs.append(text)
        del self._logs[:-50]
        self.statusBar().showMessage(text, 10000)

    # ------------------------------------------------------------- 附近
    def _refresh_nearby(self) -> None:
        self.device_list.clear()
        for peer in self.engine.peers():
            self._peers[peer.device_id] = peer
            item = QListWidgetItem(self._peer_label(peer, self.engine.is_connected(peer.device_id)))
            item.setData(Qt.UserRole, peer.device_id)
            self.device_list.addItem(item)
        self._refresh_convs()

    def _peer_label(self, peer: Peer, connected: bool = True) -> str:
        """设备列表一行文字。

        ⚠ **不要加 `@staticmethod`**：这里用了 `self.engine`，而调用方是 `self._peer_label(...)`。
        标成 staticmethod 后 `self` 会被绑到第一个实参（Peer）上，`peer` 变成 bool →
        一取属性就抛异常；而 `_refresh_nearby` 是「先 clear 再循环」，异常一抛附近页就永远是空的。
        """
        kind = "手机" if peer.device_type == "android" else "电脑"
        state = "已连接" if connected else "未连接"
        dot = "●" if connected else "○"
        lock = "🔒 " if getattr(peer, "encrypted", False) else ""
        tag = ""
        if getattr(peer, "incompatible", False):
            tag = "  ⚠ 版本过旧，需升级"
        elif not self.engine.is_paired(peer.device_id):
            tag = "  ·  未配对"
        return f"{dot} {lock}{peer.name}  [{kind}]  {peer.ip}  ·  {state}{tag}"

    def _require_paired(self, peer_id: str) -> bool:
        """未配对 / 版本过旧时**不允许**发业务数据，并给出明确提示。"""
        peer = self.engine.get_peer(peer_id) or self._peers.get(peer_id)
        if peer is not None and getattr(peer, "incompatible", False):
            QMessageBox.warning(
                self, "双方版本不一致",
                "协议版本不一致，无法通信。\n\n请把两端都升级到最新版本再试。",
            )
            return False
        if not self.engine.is_paired(peer_id):
            QMessageBox.information(
                self, "尚未配对",
                f"与「{self._conv_display_name(peer_id)}」还没配对完成。\n\n"
                "首次通信需要两端各自核对一次 **6 位数字**：\n"
                "等配对窗口弹出后，确认两边数字相同再点「配对」。",
            )
            return False
        return True

    def _tick(self) -> None:
        """定时刷新设备列表上的连接状态（连接建立/断开不产生事件）。"""
        for i in range(self.device_list.count()):
            item = self.device_list.item(i)
            peer = self._peers.get(item.data(Qt.UserRole))
            if peer is not None:
                item.setText(self._peer_label(peer, self.engine.is_connected(peer.device_id)))

        self._refresh_listen_line()
        self._refresh_peers_line()
        self._refresh_firewall_line()

    def _on_peer_changed(self, peer: Peer) -> None:
        self._peers[peer.device_id] = peer
        self._refresh_nearby()

    def _on_peer_removed(self, device_id: str) -> None:
        self._peers.pop(device_id, None)
        self._refresh_nearby()

    def _selected_peer(self) -> Optional[Peer]:
        item = self.device_list.currentItem()
        if item is None:
            return None
        return self._peers.get(item.data(Qt.UserRole))

    def _manual_add(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        ip, ok = QInputDialog.getText(self, "手动添加设备", "输入对方 IP（例如 192.168.43.1）：")
        if ok and ip.strip():
            self.engine.add_manual_ip(ip.strip())
            self.statusBar().showMessage(f"已向 {ip.strip()} 发送探测", 5000)

    def _connect_selected(self) -> None:
        """**手动连接**选中的设备（退出再进软件后可手动连上；不自动连）。"""
        peer = self._selected_peer()
        if peer is None:
            QMessageBox.information(self, "连接", "请先在设备列表里选中一台设备。")
            return
        if self.engine.is_connected(peer.device_id):
            QMessageBox.information(self, "连接", f"「{peer.name}」已经连着了。")
            return
        self.statusBar().showMessage(f"正在连接「{peer.name}」…", 8000)

        def work() -> None:
            try:
                ok, msg = self.engine.connect_by_ip(peer.ip, peer.tcp_port)
            except Exception as exc:
                ok, msg = False, f"连接异常：{exc}"
            self.engine.bus.publish(EventType.LOG, msg or ("已连接" if ok else "连接失败"))

        import threading

        threading.Thread(target=work, name="manual-connect", daemon=True).start()

    def _toggle_keep_alive(self, on: bool) -> None:
        """开关「保持长连接」。"""
        self.engine.set_keep_alive(on)
        self.statusBar().showMessage(
            "已开启「保持长连接」（心跳更密、空闲不断开）" if on else "已关闭「保持长连接」", 5000)

    def _refresh_all(self) -> None:
        """刷新：设备列表 + 设备状态（连接/未连接）+ 会话列表与预览 + 已配对设备。"""
        self._refresh_nearby()     # 重建设备列表（含连接状态）
        self._refresh_convs()      # 重建会话列表与预览
        try:
            self._refresh_paired() # 已配对设备
        except Exception:
            pass
        self._tick()               # 立刻刷新连接状态文字

    def _pair_selected(self) -> None:
        """手动发起配对。

        配对**失败后不会再自动重试**（否则会变成"疯狂发连接请求"）：
        要让某台设备重新配对，就在设备列表里选中它、点这里手动发起一次。
        """
        peer = self._selected_peer()
        if peer is None:
            QMessageBox.information(self, "配对", "请先在设备列表里选中一台设备。")
            return
        if self.engine.is_paired(peer.device_id):
            QMessageBox.information(self, "配对", f"「{peer.name}」已经配对过了。")
            return
        if self.engine.request_pairing(peer.device_id):
            self.statusBar().showMessage(
                f"已向「{peer.name}」发起配对：核对两边显示的 6 位数字一致后点「配对」。", 8000)
        else:
            QMessageBox.warning(
                self, "配对",
                f"暂时连不上「{peer.name}」。\n请确认在同一局域网/热点下，稍后再试。",
            )

    # ------------------------------------------------------------- 会话
    def _refresh_convs(self) -> None:
        current = self._current_conv
        self.conv_list.blockSignals(True)
        self.conv_list.clear()
        seen = set()
        try:
            hidden = self.engine.hidden_conversation_ids()
        except Exception:
            hidden = set()
        # 1) 已登记（新建过 / 收发过消息）的会话
        for conv in self.engine.recent_conversations():
            pid = conv["conv_id"]
            seen.add(pid)
            peer = self._peers.get(pid)
            name = peer.name if peer else (conv.get("title") or pid[:8])
            text = (conv.get("text") or "")[:30] or "（暂无消息）"
            item = QListWidgetItem(f"{name}\n{text}")
            item.setData(Qt.UserRole, pid)
            self.conv_list.addItem(item)
        # 2) 附近发现、还没聊过的设备（被删除过的除外）
        for pid, peer in self._peers.items():
            if pid in seen or pid in hidden:
                continue
            item = QListWidgetItem(f"{peer.name}\n（暂无消息）")
            item.setData(Qt.UserRole, pid)
            self.conv_list.addItem(item)
        for i in range(self.conv_list.count()):
            if self.conv_list.item(i).data(Qt.UserRole) == current:
                self.conv_list.setCurrentRow(i)
                break
        self.conv_list.blockSignals(False)

    def _open_current_conv(self) -> None:
        peer = self._selected_peer()
        if peer is None:
            QMessageBox.information(self, "提示", "请先在「附近」里选择一个设备。")
            return
        self._select_conv(peer.device_id)
        self.tabs.setCurrentIndex(1)

    def _select_conv(self, peer_id: str) -> None:
        self._current_conv = peer_id
        peer = self._peers.get(peer_id)
        # 打开即登记会话，保证对端离线后仍留在列表里
        self.engine.touch_conversation(peer_id, peer.name if peer else None)
        self.chat_title.setText(f"与 {self._conv_display_name(peer_id)} 的会话")
        self._clear_chat()
        self._chat_seen_ids = set()          # 这一段会话已渲染过的消息（用于幂等，防重复气泡）
        for msg in self.engine.history(peer_id):
            self._chat_seen_ids.add(msg.msg_id)
            self._render_message(msg)
        self._refresh_convs()

    def _new_conversation(self) -> None:
        """新建会话：从已发现的设备里挑一个，或手动输入 IP。"""
        from PySide6.QtWidgets import QInputDialog

        peers = self.engine.peers()
        options = [
            f"{p.name}  [{'手机' if p.device_type == 'android' else '电脑'}]  {p.ip}"
            for p in peers
        ]
        MANUAL = "手动输入 IP…"
        options.append(MANUAL)
        if not peers:
            QMessageBox.information(
                self, "新建会话",
                "当前没发现设备，将打开手动添加 IP。\n（同一局域网/热点下通常几秒内会自动发现）",
            )
        choice, ok = QInputDialog.getItem(self, "新建会话", "选择要聊天的设备：", options, 0, False)
        if not ok or not choice:
            return
        if choice == MANUAL:
            ip, ok2 = QInputDialog.getText(self, "新建会话", "输入对方 IP（例如 192.168.43.1）：")
            if ok2 and ip.strip():
                self._new_conversation_by_ip(ip.strip())
            return
        peer = peers[options.index(choice)]
        self._select_conv(peer.device_id)
        self.tabs.setCurrentIndex(1)

    def _new_conversation_by_ip(self, ip: str) -> None:
        """按 IP 新建会话：立刻出现占位会话，同时**后台主动直连**。

        直连（TCP + 身份应答）不依赖 UDP 发现 —— 手机开热点时 Android 不暴露热点网卡，
        广播 hello 发不出去，只有这条路能连上。成功后对端会被登记进发现表，
        `ip:<addr>` 会话自动升级成真实设备。
        """
        conv_id = self.engine.create_conversation_by_ip(ip)
        if not conv_id:
            return
        self._select_conv(conv_id)
        self.tabs.setCurrentIndex(1)
        self.statusBar().showMessage(f"正在直连 {ip} …")
        threading.Thread(target=self._direct_connect_worker, args=(ip,), daemon=True).start()

    def _direct_connect_worker(self, ip: str) -> None:
        try:
            ok, msg = self.engine.connect_by_ip(ip)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)
        self.directConnectDone.emit(ok, ip, msg)

    def _on_direct_connect_done(self, ok: bool, ip: str, msg: str) -> None:
        if ok:
            self.statusBar().showMessage(msg, 5000)
            self._refresh_nearby()
            self._refresh_convs()
            return
        self.statusBar().showMessage(f"直连 {ip} 失败：{msg}", 10000)
        QMessageBox.warning(
            self, "直连失败",
            f"{ip}\n\n{msg}\n\n"
            "排查：\n"
            "· 两端「隧道口令」是否一致（不一致时提示会写「身份对不上」）\n"
            "· 手机开热点时，请在电脑这边填**手机的**热点地址（一般是 192.168.43.1）\n"
            "· 电脑开热点时，请在手机那边填电脑的热点地址，并在电脑上放行防火墙\n\n"
            "仍会按原来的 UDP 广播方式继续尝试发现。",
        )

    def _conv_display_name(self, peer_id: str) -> str:
        peer = self._peers.get(peer_id)
        if peer is not None:
            return peer.name
        if self.engine.is_manual_conv(peer_id):
            return peer_id[3:]
        return peer_id[:8]

    def _delete_conversation(self) -> None:
        pid = self._current_conv
        if pid is None:
            item = self.conv_list.currentItem()
            pid = item.data(Qt.UserRole) if item is not None else None
        if pid is None:
            QMessageBox.information(self, "删除会话", "请先在列表里选择一个会话。")
            return
        name = self._conv_display_name(pid)
        ret = QMessageBox.question(
            self, "删除会话",
            f"删除与「{name}」的会话？\n\n聊天记录会一并清空，且该设备不再自动出现在会话列表里"
            f"（已收到的文件仍保留在传输列表/接收目录）。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        self.engine.delete_conversation(pid)
        if self._current_conv == pid:
            self._current_conv = None
            self.chat_title.setText("未选择会话")
            self._clear_chat()
        self._refresh_convs()

    def _conv_context_menu(self, pos) -> None:
        item = self.conv_list.itemAt(pos)
        if item is None:
            return
        self.conv_list.setCurrentItem(item)
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        act_del = menu.addAction("删除会话")
        chosen = menu.exec(self.conv_list.mapToGlobal(pos))
        if chosen == act_del:
            self._delete_conversation()

    def _on_conv_selected(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None:
            return
        self._current_conv = current.data(Qt.UserRole)
        peer = self._peers.get(self._current_conv)
        self.engine.touch_conversation(self._current_conv, peer.name if peer else None)
        self.chat_title.setText(f"与 {self._conv_display_name(self._current_conv)} 的会话")
        self._clear_chat()
        for msg in self.engine.history(self._current_conv):
            self._render_message(msg)

    def _clear_chat_history(self) -> None:
        """一键清空当前会话的聊天记录（只删本机）。"""
        conv = self._current_conv
        if not conv:
            QMessageBox.information(self, "清空聊天", "请先在左侧选择一个会话。")
            return
        name = self._conv_display_name(conv)
        if QMessageBox.question(
            self, "清空聊天",
            f"清空与「{name}」的全部聊天记录？\n\n"
            "只删本机记录，不影响对方；会话本身会保留。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        n = self.engine.clear_messages(conv)
        self._select_conv(conv)          # 重新渲染（里面会清空气泡映射）
        self.statusBar().showMessage(f"已清空 {n} 条聊天记录", 4000)

    def _bubble_context_menu(self, pos) -> None:
        """右键气泡：删掉这一条。"""
        w = self.sender()
        msg_id = w.property("msg_id") if w is not None else None
        if not msg_id:
            return
        menu = QMenu(self)
        act_del = menu.addAction("删除这条消息")
        if menu.exec(w.mapToGlobal(pos)) == act_del:
            self._delete_one_message(str(msg_id))

    def _delete_one_message(self, msg_id: str) -> None:
        if QMessageBox.question(
            self, "删除消息", "删除这条消息？（只删本机记录，不影响对方）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        if self.engine.delete_message(msg_id) and self._current_conv:
            self._select_conv(self._current_conv)

    def _send_text(self) -> None:
        text = self.input.text().strip()
        if not text or not self._current_conv:
            if not self._current_conv:
                QMessageBox.information(self, "提示", "请先选择会话。")
            return
        if not self._require_paired(self._current_conv):
            return
        self.input.clear()
        msg = self.engine.send_text(self._current_conv, text)
        if msg is not None:
            self._render_message(msg)
            if msg.status == "pending":
                self._warn_not_connected()

    def _warn_not_connected(self) -> None:
        if self._warned_no_conn:
            return
        self._warned_no_conn = True
        QMessageBox.warning(
            self,
            "尚未建立连接",
            "能发现设备，但还没建立 TCP 连接，所以消息/文件发不出去。\n\n"
            "常见原因与处理：\n"
            "1) 对方（或本机）的防火墙拦了入站 37020/37021 —— 点「设置 → 一键放行防火墙」；\n"
            "2) 等 8~15 秒：双方会自动反连重试；\n"
            "3) 若长期为「未连接」，在「附近」看对方 IP，必要时用「设置」手动添加对方 IP。",
        )

    def _send_dialog(self, folder: bool, images_only: bool = False) -> None:
        peer_id = self._current_conv
        if peer_id is None:
            peer = self._selected_peer()
            if peer is None:
                QMessageBox.information(self, "提示", "请先在「附近」选择设备或打开会话。")
                return
            peer_id = peer.device_id
            self._select_conv(peer_id)
        if folder:
            path = QFileDialog.getExistingDirectory(self, "选择要发送的文件夹")
            paths = [path] if path else []
        else:
            filt = "图片 (*.png *.jpg *.jpeg *.gif *.webp *.bmp)" if images_only else "所有文件 (*.*)"
            files, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件", "", filt)
            paths = files
        if paths:
            if not self._require_paired(peer_id):
                return
            self.engine.send_files(peer_id, paths)
        # 发送后**立刻刷新聊天框**：文件/图片气泡（含进度）当场出现，不用去传输页
        if self._current_conv == peer_id:
            try:
                self._select_conv(peer_id)
            except Exception:
                pass
            # 发送后**立刻刷新聊天框**：文件/图片气泡（含进度）当场出现，不用去传输页
            if self._current_conv == peer_id:
                try:
                    self._select_conv(peer_id)
                except Exception:
                    pass

    def _on_message(self, msg: ChatMessage) -> None:
        if msg.outgoing and msg.kind == MsgKind.TEXT:
            # 出站**文本**已在 _send_text 里渲染，这里避免重复；
            # 出站的文件/图片没有别处画 → 必须走下面渲染，否则"自己发的自己看不到"
            return
        if msg.conv_id == self._current_conv:
            # ⚠ **幂等**：同一条（msg_id）只画一次。
            # 否则「传输完成 → 重画整段聊天」和随后的 MESSAGE 事件会各画一次 → 一模一样两条。
            seen = getattr(self, "_chat_seen_ids", None)
            if seen is None:
                seen = set()
                self._chat_seen_ids = seen
            if msg.msg_id not in seen:
                seen.add(msg.msg_id)
                self._render_message(msg)
        peer = self._peers.get(msg.conv_id)
        name = peer.name if peer else self._conv_display_name(msg.conv_id)
        viewing = self.isVisible() and self.isActiveWindow() and msg.conv_id == self._current_conv
        # 只对文本弹气泡：文件/图片的提示统一由「传输完成」那条负责，避免双弹；
        # 而且正在盯着这个会话看时也不用提醒（气泡里已经显示）。
        if msg.kind == MsgKind.TEXT and not viewing:
            self._notify(f"来自 {name}", msg.text)
        self._refresh_convs()

    def _on_message_status(self, data) -> None:
        """消息状态变化（已发送 / 已送达）→ 就地把对应气泡底部的状态文字换掉。"""
        try:
            msg_id = str(data.get("msg_id"))
            status = str(data.get("status"))
        except Exception:
            return
        entry = self._bubble_by_msg.get(msg_id)
        if entry is None:
            return
        bubble, base_body = entry
        label = _MSG_STATUS_TEXT.get(status, status)
        try:
            bubble.setText(base_body + f'<br><span style="color:{theme.STATUS_TEXT}; font-size:9px;">{label}</span>')
        except RuntimeError:
            # 控件已被销毁（会话被切走）
            self._bubble_by_msg.pop(msg_id, None)

    def _clear_chat(self) -> None:
        self._bubble_by_msg.clear()
        while self.chat_layout.count():
            item = self.chat_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _scroll_chat_to_bottom(self) -> None:
        bar = self.chat_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _render_message(self, msg: ChatMessage) -> None:
        """一条消息 = 一行（气泡靠左或靠右）。样式与手机端一致：自己的在右。"""
        outgoing = msg.outgoing
        bubble = QLabel()
        bubble.setWordWrap(True)
        bubble.setTextFormat(Qt.RichText)
        bubble.setTextInteractionFlags(Qt.TextBrowserInteraction)   # 让链接可点
        bubble.setOpenExternalLinks(False)
        bubble.linkActivated.connect(self._open_local_path)
        bubble.setStyleSheet(
            "background-color:%s; border-radius:10px; padding:6px 10px;"
            % (theme.TEAL_SOFT if outgoing else theme.SURFACE_VAR)
        )
        bubble.setMaximumWidth(min(340, max(240, int(self.width() * 0.7))))
        # 右键气泡 → 删除这条
        bubble.setProperty("msg_id", msg.msg_id)
        bubble.setContextMenuPolicy(Qt.CustomContextMenu)
        bubble.customContextMenuRequested.connect(self._bubble_context_menu)

        if msg.kind == MsgKind.TEXT:
            body = html.escape(msg.text).replace("\n", "<br>")
        else:
            body = self._file_body_html(msg)
        if outgoing:
            label = _MSG_STATUS_TEXT.get(msg.status, msg.status)
            # 记住“不含状态”的正文，状态变化时只换底部那一行
            self._bubble_by_msg[msg.msg_id] = (bubble, body)
            body += f'<br><span style="color:{theme.STATUS_TEXT}; font-size:9px;">{label}</span>'
        bubble.setText(body)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        if outgoing:                      # 自己发的靠右
            row_layout.addStretch(1)
            row_layout.addWidget(bubble)
        else:                             # 对方发来的靠左
            row_layout.addWidget(bubble)
            row_layout.addStretch(1)
        self.chat_layout.addWidget(row)
        QTimer.singleShot(0, self._scroll_chat_to_bottom)

    def _file_body_html(self, msg: ChatMessage) -> str:
        if msg.file is None:
            return "(文件)"
        f = msg.file
        path = f.local_path or self._transfer_paths.get(f.file_id)
        size = human_size(f.size)
        small = f'style="font-size:10px; color:{theme.MUTED};"'
        if f.kind == "image" and path and Path(path).exists():
            return (
                f'<a href="open:{html.escape(path)}">'
                f'<img src="{html.escape(Path(path).as_uri())}" width="220"></a>'
                f'<br><span {small}>{html.escape(f.name)} · {size}</span>'
            )
        if path and Path(path).exists():
            return (
                f'<a href="open:{html.escape(path)}">📄 {html.escape(f.name)}</a>'
                f'<br><span {small}>{size}</span>'
            )
        return f'📄 {html.escape(f.name)}<br><span {small}>{size}</span>'

    def _open_local_path(self, link: str) -> None:
        """气泡里的文件/图片点一下 → 用系统默认程序打开。"""
        if link.startswith("open:"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(link[5:]))

    # ------------------------------------------------------------- 传输
    def _open_transfer_path(self, fid: str) -> None:
        path = self._transfer_paths.get(fid)
        if not path:
            QMessageBox.information(self, "打开", "这个任务还没有本地文件（可能还在传输中）。")
            return
        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, "打开", f"文件不在了：\n{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _on_transfer_double_clicked(self, item) -> None:
        fid = self._transfer_row_fid.get(item.row())
        if fid:
            self._open_transfer_path(fid)

    def _open_selected_transfer(self) -> None:
        row = self.transfer_table.currentRow()
        fid = self._transfer_row_fid.get(row) if row >= 0 else None
        if not fid:
            QMessageBox.information(self, "打开", "请先在列表里选中一行。")
            return
        self._open_transfer_path(fid)

    def _export_selected_transfer(self) -> None:
        """把选中的传输文件**导出到你自选的路径**。"""
        import shutil
        from pathlib import Path as _P

        row = self.transfer_table.currentRow()
        fid = self._transfer_row_fid.get(row) if row >= 0 else None
        src = self._transfer_paths.get(fid) if fid else None
        if not src or not _P(src).is_file():
            QMessageBox.information(self, "导出", "请先选中一条**有本地文件**的记录。")
            return
        dst, _ = QFileDialog.getSaveFileName(self, "导出到…", _P(src).name)
        if not dst:
            return
        try:
            shutil.copy2(src, dst)
            self.statusBar().showMessage(f"已导出到 {dst}", 8000)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def _delete_selected_transfer(self) -> None:
        row = self.transfer_table.currentRow()
        fid = self._transfer_row_fid.get(row) if row >= 0 else None
        if not fid:
            QMessageBox.information(self, "删除文件", "请先在列表里选中一行。")
            return
        item = self.transfer_table.item(row, 0)
        name = item.text() if item is not None else fid
        path = self._transfer_paths.get(fid)
        target = f"\n\n本机文件：{path}" if path else "\n\n（这一条还没有本机文件，只从列表移除）"
        ret = QMessageBox.question(
            self, "删除文件",
            f"删除「{name}」？{target}\n\n"
            "确认后会把该文件从磁盘删掉（不可恢复），并从传输列表移除。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        ok, msg = self.engine.delete_transfer(fid)
        if not ok:
            QMessageBox.warning(self, "删除失败", msg)
            return
        self._remove_transfer_row(fid)
        self.statusBar().showMessage(msg, 5000)

    def _remove_transfer_row(self, fid: str) -> None:
        self._transfer_stat.pop(fid, None)
        row = self._transfer_rows.pop(fid, None)
        self._transfer_paths.pop(fid, None)
        if row is None or row >= self.transfer_table.rowCount():
            return
        self.transfer_table.removeRow(row)
        # 删行后下面各行的行号整体上移，重建「行号 ↔ file_id」映射
        self._transfer_rows.clear()
        self._transfer_row_fid.clear()
        for r in range(self.transfer_table.rowCount()):
            cell = self.transfer_table.item(r, 0)
            f = cell.data(Qt.UserRole) if cell is not None else None
            if f:
                self._transfer_rows[f] = r
                self._transfer_row_fid[r] = f

    def _transfer_context_menu(self, pos) -> None:
        item = self.transfer_table.itemAt(pos)
        if item is None:
            return
        self.transfer_table.setCurrentCell(item.row(), 0)
        menu = QMenu(self)
        act_open = menu.addAction("打开")
        act_del = menu.addAction("删除文件")
        menu.addSeparator()
        act_clear = menu.addAction("清空列表")
        chosen = menu.exec(self.transfer_table.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._open_selected_transfer()
        elif chosen == act_del:
            self._delete_selected_transfer()
        elif chosen == act_clear:
            self._clear_transfers()

    def _load_transfer_history(self) -> None:
        """启动时把数据库里的历史传输记录回填到列表（关掉重开也能看到）。"""
        try:
            rows = self.engine.persisted_transfers()
        except Exception:
            return
        # 数据库按 updated_at 倒序返回；反过来插，让最早的在最上面、顺序自然
        for t in reversed(rows):
            self._on_transfer(t, notify=False)

    def _clear_transfers(self) -> None:
        if self.transfer_table.rowCount() == 0:
            QMessageBox.information(self, "清空传输列表", "列表已经是空的。")
            return
        if QMessageBox.question(
            self,
            "清空传输列表",
            "清空所有传输记录？\n\n"
            "已下载到本机的文件会一并删除（不可恢复）；\n"
            "正在进行中的任务会被保留。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        ok, msg = self.engine.clear_transfers()
        if not ok:
            QMessageBox.warning(self, "清空失败", msg)
            return
        # 重建列表：进行中的任务会从数据库里重新读回来
        self.transfer_table.setRowCount(0)
        self._transfer_rows.clear()
        self._transfer_row_fid.clear()
        self._transfer_paths.clear()
        self._load_transfer_history()
        self.statusBar().showMessage(msg, 5000)

    def _on_transfer(self, t: Transfer, notify: bool = True) -> None:
        fid = t.file.file_id if t.file else t.transfer_id
        row = self._transfer_rows.get(fid)
        if row is None:
            row = self.transfer_table.rowCount()
            self.transfer_table.insertRow(row)
            self._transfer_rows[fid] = row
            self._transfer_row_fid[row] = fid
            name_item = QTableWidgetItem(t.file.name if t.file else fid)
            name_item.setData(Qt.UserRole, fid)      # 删除/重排时靠它认行
            self.transfer_table.setItem(row, 0, name_item)
            # 多设备同时传时，光看文件名分不清是哪台设备发来的
            peer = self._peers.get(t.peer_id)
            self.transfer_table.setItem(
                row, 1, QTableWidgetItem(peer.name if peer else self._conv_display_name(t.peer_id))
            )
            self.transfer_table.setItem(row, 2, QTableWidgetItem("发送" if t.direction == "send" else "接收"))
            self.transfer_table.setItem(row, 3, QTableWidgetItem(human_size(t.file.size if t.file else 0)))
        done = t.received if t.direction == "recv" else t.sent
        total = t.file.size if t.file else 0
        pct = int(100 * done / total) if total else 0
        self.transfer_table.setItem(row, 4, QTableWidgetItem(f"{pct}%"))
        # 状态列带上失败原因，省得用户只看到「失败」不知道为啥
        state_text = _STATE_TEXT.get(t.state, str(t.state))
        if t.error:
            state_text = f"{state_text} · {t.error}"
        # 速度 + 用时：首次见到该任务记开始时间；用**增量**算瞬时速度（每 ~0.4s 刷新一次）
        import time as _time

        _now = _time.time()
        _st = self._transfer_stat.setdefault(
            fid, {"start": _now, "bytes": 0, "at": _now, "speed": 0.0}
        )
        if t.state in (TransferState.COMPLETED, TransferState.FAILED):
            _st["speed"] = 0.0
        else:
            _dt = _now - _st["at"]
            if _dt >= 0.4:
                _st["speed"] = max(0.0, (done - _st["bytes"]) / _dt)
                _st["bytes"], _st["at"] = done, _now
        _elapsed = max(0.0, _now - _st["start"])
        _mm, _ss = divmod(int(_elapsed), 60)
        _speed_txt = f"{human_size(int(_st['speed']))}/s" if _st["speed"] > 0 else "—"
        state_text = f"{state_text} · {_speed_txt} · {_mm:02d}:{_ss:02d}"
        self.transfer_table.setItem(row, 5, QTableWidgetItem(state_text))
        # 发出的图片用本地原文件预览，收到的用落盘文件预览
        path = t.final_path or t.local_path
        if path:
            self._transfer_paths[fid] = path
            self.transfer_table.setItem(row, 6, QTableWidgetItem("双击打开"))
        # 传输**完成** → 后端已在会话里补了一条文件/图片消息；刷新聊天框，
        # 让它像普通消息一样立刻出现（收发两端都会补，所以两边都能看到）。
        if t.state == TransferState.COMPLETED and self._current_conv == t.peer_id:
            try:
                self._select_conv(t.peer_id)
            except Exception:
                pass
        if t.state in (TransferState.COMPLETED, TransferState.FAILED):
            key = f"{fid}:{t.state}"
            if notify and key not in self._transfer_notified:
                self._transfer_notified.add(key)
                name = t.file.name if t.file else fid
                if t.state == TransferState.COMPLETED:
                    verb = "已接收" if t.direction == "recv" else "已发送"
                    self._notify(verb, name)
                else:
                    self._notify("传输失败", f"{t.error or ''} · {name}".strip(" ·"))
            try:
                self.tray.setToolTip(self._idle_tooltip())   # 回到空闲文案
            except Exception:
                pass
        elif t.state == TransferState.ACTIVE:
            self._update_tray_progress(fid, t)

    # ------------------------------------------------------------- 目录配置
    def _pick_receive_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择接收目录", str(self.cfg.receive_dir))
        if not path:
            return
        try:
            self.engine.set_receive_dir(path)
        except Exception as exc:
            QMessageBox.critical(self, "设置失败", str(exc))
            return
        self.cfg = self.engine.cfg
        self._refresh_status_lines()
        QMessageBox.information(self, "已修改", f"接收目录已改为：\n{self.cfg.receive_dir}\n\n（立即生效）")

    def _pick_data_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择数据目录", str(self.cfg.data_dir))
        if not path:
            return
        if QMessageBox.question(
            self,
            "确认修改数据目录",
            f"数据目录将改为：\n{path}\n\n"
            "数据目录里放着数据库（口令、会话、传输记录）和身份密钥。\n"
            "改完后**需要重启程序**才生效；旧目录的内容**不会自动搬过去**。\n\n要继续吗？",
        ) != QMessageBox.Yes:
            return
        try:
            self.engine.set_data_dir(path)
        except Exception as exc:
            QMessageBox.critical(self, "设置失败", str(exc))
            return
        self._refresh_status_lines()
        self._set_status("data", f"{self.cfg.data_dir}（已改为 {path}，重启后生效）")
        QMessageBox.information(
            self, "已记录",
            f"数据目录已设为：\n{path}\n\n请**退出并重启**程序后生效（退出走托盘菜单里的「退出」）。",
        )

    # ------------------------------------------------------------- 设置
    # ------------------------------------------------------------- 配对（v4）
    def _on_pair_request(self, data) -> None:
        """后端问「要不要跟这台设备配对」→ 弹窗让用户核对 6 位码。"""
        peer_id = str(data.get("peer_id") or "")
        if not peer_id:
            return
        name = str(data.get("name") or peer_id[:8])
        sas = str(data.get("sas") or "")
        accepted = PairingDialog(name, sas, self).exec() == QDialog.DialogCode.Accepted
        # 回答后端（它正在等这个答案；超时/取消都算拒绝）
        self.engine.answer_pairing(peer_id, accepted)
        self.statusBar().showMessage(
            f"已确认与「{name}」配对" if accepted else f"已取消与「{name}」的配对", 5000
        )
        self._refresh_paired()

    def _on_paired_changed(self, _data) -> None:
        self._refresh_paired()
        self._refresh_nearby()

    def _refresh_paired(self) -> None:
        """刷新设置页的「已配对设备」列表。"""
        if not hasattr(self, "paired_list"):
            return
        self.paired_list.clear()
        for row in self.engine.paired_devices():
            did = str(row.get("device_id") or "")
            item = QListWidgetItem(row.get("name") or did[:8])
            item.setData(Qt.UserRole, did)
            item.setToolTip("已配对：对方身份已被记住。解绑后需重新配对。")
            self.paired_list.addItem(item)

    def _unpair_selected(self) -> None:
        item = self.paired_list.currentItem()
        if item is None:
            QMessageBox.information(self, "解绑", "请先在列表里选中一台设备。")
            return
        peer_id = item.data(Qt.UserRole)
        if not peer_id:
            return
        if QMessageBox.question(
            self,
            "解绑设备",
            f"解绑「{item.text()}」？\n\n解绑后它下次连接要重新配对（重新核对 6 位数字）。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self.engine.unpair(peer_id)
        self._refresh_paired()
        self._refresh_nearby()

    # ------------------------------------------------------------- 开机自启
    def _toggle_autostart(self, enabled: bool) -> None:
        save_prefs(autostart="1" if enabled else "0")
        self._sync_autostart_registry(enabled)
        self.statusBar().showMessage(
            "已开启开机自启动" if enabled else "已关闭开机自启动", 4000
        )

    def _sync_autostart_registry(self, enabled: bool, quiet: bool = False) -> None:
        """把「大司农」写进 / 移出 HKCU 的 Run 项。开发模式（非打包 exe）不动注册表。"""
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            return
        try:
            import winreg

            run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE
            ) as key:
                if enabled:
                    winreg.SetValueEx(
                        key, _AUTOSTART_VALUE, 0, winreg.REG_SZ, f'"{sys.executable}"'
                    )
                else:
                    try:
                        winreg.DeleteValue(key, _AUTOSTART_VALUE)
                    except FileNotFoundError:
                        pass
        except Exception as exc:
            if not quiet:
                QMessageBox.warning(self, "开机自启", f"写入注册表失败：{exc}")

    @staticmethod
    def _ps_run(script: str, timeout: int = 25) -> str:
        """以 -EncodedCommand 执行 PowerShell 片段（避免中文/引号转义问题）。"""
        import base64

        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, timeout=timeout, creationflags=_NO_WINDOW,
        )
        return (proc.stdout or b"").decode("utf-8", "replace")

    def _firewall_status(self) -> tuple:
        """返回 (是否已放行, 说明)。

        不只看本程序命名的规则：Windows 在程序首次监听时弹的「允许访问」生成的是
        **按程序**的入站规则，按名字是查不到的——只按名字查会误报。
        """
        # 1) 我们自己加的命名规则（最快）
        try:
            out = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 f"name=Dasinong UDP {self.cfg.udp_port}"],
                capture_output=True, timeout=10, creationflags=_NO_WINDOW,
            )
            if out.returncode == 0:
                return True, f"已存在命名规则「Dasinong UDP {self.cfg.udp_port}」"
        except Exception:
            pass

        # 2) 按程序放行（Windows 弹窗「允许访问」生成的就是这种）
        try:
            script = _PS_FW_CHECK.format(exe=str(Path(sys.executable)))
            lines = [ln.strip() for ln in self._ps_run(script, timeout=20).splitlines() if ln.strip()]
            tag = lines[-1] if lines else "NONE"
            if tag.startswith("PROGRAM:"):
                names = tag[len("PROGRAM:"):]
                return True, f"已按程序放行（规则：{names}）"
        except Exception:
            # 探测失败时按「已放行」处理，避免反复打扰
            return True, "防火墙探测失败（按已放行处理）"
        return False, "未检测到任何入站放行"

    # ------------------------------------------------------------- 防火墙（只写进设置页，不弹窗）
    def _refresh_firewall_async(self) -> None:
        """后台探测一次入站放行情况；结果写进「设置 → 运行状态 → 防火墙」。"""
        if sys.platform != "win32":
            self._fw_state = (True, "非 Windows，无需放行")
            self._refresh_firewall_line()
            return
        if self._fw_probing:
            return
        self._fw_probing = True
        if self._fw_state is None:
            label = self._status_labels.get("firewall")
            if label is not None:
                label.setText("检测中…")
                label.setStyleSheet(f"color:{theme.MUTED};")

        def probe() -> None:
            allowed, detail = self._firewall_status()
            self.firewallStatusReady.emit(allowed, detail)

        threading.Thread(target=probe, name="fw-probe", daemon=True).start()

    def _update_firewall_line(self, allowed: bool, detail: str) -> None:
        self._fw_probing = False
        self._fw_state = (allowed, detail)
        self._refresh_firewall_line()

    def _firewall_allow(self) -> None:
        """生成临时 bat 并以管理员身份运行，放行 UDP/TCP 入站。"""
        if sys.platform != "win32":
            QMessageBox.information(self, "放行防火墙", "仅在 Windows 上可用。")
            return
        udp, tcp = self.cfg.udp_port, self.cfg.tcp_port
        lines = [
            "@echo off",
            f'netsh advfirewall firewall delete rule name="Dasinong UDP {udp}" >nul 2>&1',
            f'netsh advfirewall firewall delete rule name="Dasinong TCP {tcp}" >nul 2>&1',
            f'netsh advfirewall firewall add rule name="Dasinong UDP {udp}" dir=in action=allow '
            f"protocol=UDP localport={udp} profile=any",
            f'netsh advfirewall firewall add rule name="Dasinong TCP {tcp}" dir=in action=allow '
            f"protocol=TCP localport={tcp} profile=any",
            "echo OK",
        ]
        bat = Path(tempfile.gettempdir()) / "dasinong_allow_firewall.bat"
        try:
            bat.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Start-Process -FilePath '{bat}' -Verb RunAs -Wait"],
                creationflags=_NO_WINDOW, timeout=60,
            )
        except Exception as exc:
            QMessageBox.critical(self, "放行失败", f"未能执行：{exc}")
            return
        self.statusBar().showMessage("已请求放行，正在重新检测…")
        self._fw_state = None
        self._refresh_firewall_async()

    # ------------------------------------------------------------- 诊断
    def _diagnose(self) -> None:
        self.statusBar().showMessage("正在诊断…（约需几秒）")

        def worker() -> None:
            try:
                text = self.engine.diagnose()
            except Exception as exc:  # 诊断自身出错也要给出信息
                text = f"诊断失败：{exc}"
            if self._logs:
                text += "\n\n最近日志：\n" + "\n".join(f"  {line}" for line in self._logs[-15:])
            self.diagnosisDone.emit(text)

        threading.Thread(target=worker, name="diagnose", daemon=True).start()

    def _show_diagnosis(self, text: str) -> None:
        self.statusBar().clearMessage()
        box = QMessageBox(self)
        box.setWindowTitle("网络诊断")
        box.setText("把下面内容发我，就能定位卡在哪一步：")
        box.setDetailedText(text)
        box.setIcon(QMessageBox.Information)
        box.exec()

    # ------------------------------------------------------------- 拖拽发送
    # 把文件/文件夹拖进窗口任意位置即可发送。目标解析顺序：
    #   拖到某台设备上 → 那台设备
    #   否则 → 当前打开的会话 → 「附近」里选中的设备 → 唯一一台设备（直接发）→ 弹出选择框
    @staticmethod
    def _drop_paths(event) -> Optional[List[str]]:
        """取本次拖拽里的本地路径（文件或文件夹）；不是本地文件则返回 None。"""
        md = event.mimeData()
        if not md.hasUrls():
            return None
        paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        return paths or None

    def _peer_at_pos(self, pos) -> Optional[str]:
        """拖拽落点在「附近」某台设备上时，直接发给那台设备。"""
        if not self.device_list.isVisible():
            return None
        item = self.device_list.itemAt(self.device_list.mapFrom(self, pos))
        return item.data(Qt.UserRole) if item is not None else None

    def _selected_peer_id(self) -> Optional[str]:
        peer = self._selected_peer()
        return peer.device_id if peer else None

    def _choose_peer_dialog(self, peers) -> Optional[str]:
        from PySide6.QtWidgets import QInputDialog

        labels = [f"{p.name}（{p.ip}）" for p in peers]
        choice, ok = QInputDialog.getItem(
            self, "发送到哪台设备", "选择目标设备：", labels, 0, False
        )
        if not ok or choice not in labels:
            return None
        return peers[labels.index(choice)].device_id

    def _drop_hint_peer(self, pos) -> Optional[str]:
        return self._peer_at_pos(pos) or self._current_conv or self._selected_peer_id()

    def _update_drop_hint(self, pos, count: int) -> None:
        peer_id = self._drop_hint_peer(pos)
        n = f"{count} 项" if count > 1 else "文件"
        if peer_id is not None:
            self.statusBar().showMessage(
                f"松开即可把{n}发送到「{self._conv_display_name(peer_id)}」"
            )
        else:
            self.statusBar().showMessage(f"松开后选择要发送的设备（{n}）")

    def _clear_drop_hint(self) -> None:
        self.statusBar().showMessage(self._status_default)

    def dragEnterEvent(self, event) -> None:
        if self._drop_paths(event) is not None:
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        paths = self._drop_paths(event)
        if paths is not None:
            event.acceptProposedAction()
            self._update_drop_hint(event.position().toPoint(), len(paths))

    def dragLeaveEvent(self, event) -> None:
        self._clear_drop_hint()

    def dropEvent(self, event) -> None:
        paths = self._drop_paths(event)
        self._clear_drop_hint()
        if paths is None:
            return
        event.acceptProposedAction()

        peer_id = self._drop_hint_peer(event.position().toPoint())
        if peer_id is None:
            peers = list(self.engine.peers())   # 用引擎的实时列表（_peers 可能残留已下线的设备）
            if len(peers) == 1:
                peer_id = peers[0].device_id
            elif peers:
                peer_id = self._choose_peer_dialog(peers)
        if peer_id is None:
            QMessageBox.information(
                self,
                "没有可发送的设备",
                "还没发现任何设备。请确认两台设备在同一局域网/热点下，\n"
                "或在「设置」里手动添加对方 IP。",
            )
            return
        if not self._require_paired(peer_id):
            return

        self.engine.send_files(peer_id, paths)
        name = self._conv_display_name(peer_id)
        self.statusBar().showMessage(
            f"已发送 {len(paths)} 项到「{name}」，进度见「传输」页", 6000
        )
        # 切到「传输」页给个明确反馈（transfer_table 的父控件就是那一页）
        self.tabs.setCurrentWidget(self.transfer_table.parentWidget())

    # ------------------------------------------------------------- 关闭
    def _quit(self) -> None:
        from PySide6.QtWidgets import QApplication

        self._really_quit = True
        QApplication.quit()

    def closeEvent(self, event) -> None:
        if not self._really_quit:
            event.ignore()
            self.hide()
            self._notify("大司农仍在运行", "已最小化到托盘，仍在后台接收消息与文件。")
            return
        try:
            self.engine.stop()
        finally:
            super().closeEvent(event)
