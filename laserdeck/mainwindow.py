"""Main window: wires the GRBL controller, the viewer and the panels together (Laser Sender + SVG Converter)."""
from __future__ import annotations

import os
import time

import numpy as np
from PySide6.QtCore import QEvent, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
                               QStackedWidget, QTabWidget, QToolBar, QToolButton, QVBoxLayout, QWidget)
from serial.tools import list_ports

from . import __version__, grbl, theme
from .canvas import LaserView, add_view_buttons
from .convertpage import ConverterPage
from .gcode import Program, format_duration, parse_program, preamble_for
from .grbl import SIM_PORT, GrblConnection, Status
from .settings_dialog import GrblSettingsDialog
from .theme import icon
from .widgets import AxisRow, Card, CodeEditor, Console, JogPad, OverrideBox, RapidBox, StateBadge

STEPS = [("0.1 mm", 0.1), ("1 mm", 1.0), ("5 mm", 5.0), ("10 mm", 10.0), ("50 mm", 50.0), ("Continuous", None)]
BAUDS = [115200, 57600, 38400, 19200, 9600, 250000]
APP_NAME = "Laser Deck"
TITLE = f"Laser Deck {__version__}"


class ParseThread(QThread):
    parsed = Signal(object, int)

    def __init__(self, text: str, rates, serial_no: int, parent=None):
        super().__init__(parent)
        self.text, self.rates, self.serial_no = text, rates, serial_no

    def run(self) -> None:
        self.parsed.emit(parse_program(self.text, self.rates), self.serial_no)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(TITLE)
        self.setAcceptDrops(True)
        self.qs = QSettings("laserdeck", "laserdeck")
        self.conn: GrblConnection | None = None
        self.status = Status()
        self.program: Program | None = None
        self.file_path: str | None = None
        self._display_name = ""
        self._run_cmds: list[tuple[int, str]] = []
        self._acked_idx = -1
        self._running = False
        self._paused = False
        self._t0 = 0.0
        self._paused_total = 0.0
        self._paused_at = 0.0
        self._continuous = False
        self._firing = False
        self._door_reset_done = False
        self._alarm_code = -1
        self._autohome_pending = False
        self._parse_serial = 0
        self._parse_thread: ParseThread | None = None
        self._loading_text = False
        self._settings_dlg: GrblSettingsDialog | None = None
        self._alarm_text = ""
        self._last_error = ""
        self._first_error = ""
        self._error_count = 0
        self._ports_sig: list[str] = []

        self.mode = 0
        self._build_toolbar()
        self._build_ui()
        self._build_statusbar()
        self._sender_actions += [self.act_open, self.act_reload, self.act_save, self.act_run, self.act_pause,
                                 self.act_stop, self.act_frame, self.act_home, self.act_unlock, self.act_reset,
                                 self.act_check]
        self.set_mode(0)
        self._restore()
        self._refresh_ports()
        self._update_enabled()

        self._ui_timer = QTimer(self, interval=80)
        self._ui_timer.timeout.connect(self._ui_tick)
        self._ui_timer.start()
        self._port_timer = QTimer(self, interval=3000)
        self._port_timer.timeout.connect(self._auto_refresh_ports)
        self._port_timer.start()
        self._reparse_timer = QTimer(self, singleShot=True, interval=700)
        self._reparse_timer.timeout.connect(self._reparse_editor)
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------------ UI construction
    @staticmethod
    def _spin(lo: float, hi: float, val: float, decimals: int, suffix: str) -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setRange(lo, hi)
        w.setDecimals(decimals)
        w.setValue(val)
        w.setSuffix(suffix)
        return w

    def _act(self, name: str, text: str, tip: str, slot, shortcut: str | None = None, checkable=False) -> QAction:
        a = QAction(icon(name), text, self)
        a.setToolTip(f"{tip}" + (f"  ({shortcut})" if shortcut else ""))
        a.setCheckable(checkable)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(slot)
        return a

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        seg = QWidget()
        seg.setStyleSheet("background: transparent;")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(0, 0, 8, 0)
        sl.setSpacing(0)
        self.btn_mode = []
        for i, text in enumerate(("Laser Sender", "SVG Converter")):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setToolTip("Switch to " + text)
            b.setStyleSheet("padding: 7px 16px; font-weight: 600; border-radius: 0;"
                            + ("border-top-left-radius:8px; border-bottom-left-radius:8px;" if i == 0 else
                               "border-top-right-radius:8px; border-bottom-right-radius:8px; border-left: none;"))
            b.clicked.connect(lambda _=False, n=i: self.set_mode(n))
            sl.addWidget(b)
            self.btn_mode.append(b)
        tb.addWidget(seg)
        self._sender_actions: list[QAction] = []
        self.act_open = self._act("open", "Open", "Open a G-code file", self.open_file, "Ctrl+O")
        self.act_reload = self._act("reload", "Reload", "Reload the file from disk", self.reload_file, "Ctrl+Shift+R")
        self.act_save = self._act("save", "Save", "Save the changes to the file", self.save_file, "Ctrl+S")
        self.act_run = self._act("play", "Run", "Run the program", self.run_program, "Ctrl+R")
        self.act_pause = self._act("pause", "Pause", "Feed hold / resume (the laser stops)", self.toggle_pause, "Ctrl+P")
        self.act_stop = self._act("stop", "Stop", "Abort (hold + reset): the laser turns off", self.stop_program, "Ctrl+.")
        self.act_frame = self._act("frame", "Frame", "Trace the bounding box of the job (laser off or pointer power)",
                                   self.frame_job, "Ctrl+F")
        self.act_home = self._act("home", "Home", "Homing cycle ($H)", lambda: self.send("$H"))
        self.act_unlock = self._act("unlock", "Unlock", "Clear the alarm ($X)", lambda: self.send("$X"))
        self.act_reset = self._act("reset", "Reset", "Soft reset (Ctrl-X)", self.soft_reset)
        self.act_check = self._act("check", "Check", "Check mode: simulate without moving ($C)", lambda: self.send("$C"),
                                   checkable=True)
        self.act_settings = self._act("gear", "GRBL", "Board settings ($$)", self.show_settings)
        for a in (self.act_open, self.act_reload, self.act_save):
            tb.addAction(a)
        self._sender_actions += [tb.addSeparator()]
        for a in (self.act_run, self.act_pause, self.act_stop):
            tb.addAction(a)
        for a, name in ((self.act_run, "run"), (self.act_stop, "stopb")):
            tb.widgetForAction(a).setObjectName(name)
        self._sender_actions += [tb.addSeparator()]
        for a in (self.act_frame, self.act_home, self.act_unlock, self.act_reset, self.act_check):
            tb.addAction(a)
        spacer = QWidget()
        spacer.setStyleSheet(f"background: {theme.PANEL};")
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.act_settings)

    def _build_ui(self) -> None:
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.stack = QStackedWidget()
        self.stack.addWidget(split)                    # page 0: Laser Sender
        self.converter = ConverterPage()               # page 1: SVG converter
        self.converter.sendRequested.connect(self._send_from_converter)
        self.stack.addWidget(self.converter)
        self.setCentralWidget(self.stack)

        # ---- left column
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 6, 10)
        lv.setSpacing(10)

        c = Card("Connection")
        row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.port_combo.setMinimumContentsLength(12)
        self.baud_combo = QComboBox()
        for b in BAUDS:
            self.baud_combo.addItem(str(b), b)
        self.baud_combo.setFixedWidth(96)
        row.addWidget(self.port_combo, 1)
        row.addWidget(self.baud_combo)
        c.body.addLayout(row)
        row = QHBoxLayout()
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setObjectName("primary")
        self.btn_connect.clicked.connect(self.toggle_connection)
        self.badge = StateBadge()
        row.addWidget(self.btn_connect, 1)
        row.addWidget(self.badge)
        c.body.addLayout(row)
        self.chk_autohome = QCheckBox("Home automatically after connecting")
        self.chk_autohome.setToolTip("Runs the homing cycle ($H) as soon as the board is ready: the head goes to the limit "
                                     "switches and the machine zero is set. Needs homing enabled ($22=1).")
        c.body.addWidget(self.chk_autohome)
        lv.addWidget(c)

        c = Card("Position")
        self.axes = {a: AxisRow(a) for a in "XY"}
        for a, r in self.axes.items():
            r.zeroClicked.connect(lambda ax: self.send(f"G10L20P0{ax}0"))
            r.gotoClicked.connect(lambda ax: self.goto_zero(ax))
            c.body.addWidget(r)
        row = QHBoxLayout()
        b = QPushButton("Zero XY here")
        b.setToolTip("Set the work origin (X0 Y0) at the current head position")
        b.clicked.connect(lambda: self.send("G10L20P0X0Y0"))
        b2 = QPushButton("Go to XY 0")
        b2.clicked.connect(lambda: self.goto_zero("XY"))
        for x in (b, b2):
            row.addWidget(x)
        c.body.addLayout(row)
        self.live = QLabel("Feed 0   Power 0")
        self.live.setObjectName("muted")
        c.body.addWidget(self.live)
        lv.addWidget(c)

        c = Card("Jog")
        pad_row = QHBoxLayout()
        self.pad = JogPad()
        self.pad.pressed.connect(self.on_jog_press)
        self.pad.released.connect(self.on_jog_release)
        pad_row.addStretch()
        pad_row.addWidget(self.pad)
        pad_row.addStretch()
        c.body.addLayout(pad_row)
        g = QHBoxLayout()
        g.addWidget(QLabel("Step"))
        self.step_combo = QComboBox()
        for label, v in STEPS:
            self.step_combo.addItem(label, v)
        self.step_combo.setCurrentIndex(1)
        g.addWidget(self.step_combo, 1)
        g.addWidget(QLabel("Speed"))
        self.jog_feed = QSpinBox()
        self.jog_feed.setRange(1, 20000)
        self.jog_feed.setValue(3000)
        self.jog_feed.setSuffix(" mm/min")
        self.jog_feed.setSingleStep(100)
        g.addWidget(self.jog_feed, 1)
        c.body.addLayout(g)
        g = QHBoxLayout()
        self.chk_keys = QCheckBox("Keyboard: arrow keys")
        g.addWidget(self.chk_keys)
        g.addStretch()
        c.body.addLayout(g)
        lv.addWidget(c)

        c = Card("Frame and laser test")
        self.frame_power = self._spin(0, 100, 0, 1, " %")
        self.frame_power.setToolTip("Power while tracing the frame. 0 % = laser off: the head only moves. A low value "
                                    "(1–3 %) draws a faint visible line: needs laser mode ($32=1)")
        self.frame_feed = QSpinBox()
        self.frame_feed.setRange(10, 20000)
        self.frame_feed.setValue(3000)
        self.frame_feed.setSuffix(" mm/min")
        grid = QGridLayout()
        grid.setSpacing(6)
        grid.addWidget(QLabel("Frame power"), 0, 0)
        grid.addWidget(self.frame_power, 0, 1)
        grid.addWidget(QLabel("Frame speed"), 1, 0)
        grid.addWidget(self.frame_feed, 1, 1)
        c.body.addLayout(grid)
        self.btn_frame = QPushButton("  Frame the job")
        self.btn_frame.setIcon(icon("frame"))
        self.btn_frame.clicked.connect(self.frame_job)
        c.body.addWidget(self.btn_frame)
        self.chk_fire = QCheckBox("Enable test fire (wear laser safety glasses!)")
        self.chk_fire.toggled.connect(lambda _on: self._update_enabled())
        c.body.addWidget(self.chk_fire)
        row = QHBoxLayout()
        self.btn_fire = QPushButton("  Hold to fire")
        self.btn_fire.setIcon(icon("laser"))
        self.btn_fire.setObjectName("danger")
        self.btn_fire.setToolTip("The laser is on only while the button is held down")
        self.btn_fire.pressed.connect(self._fire_on)
        self.btn_fire.released.connect(self._fire_off)
        self.fire_power = self._spin(0, 100, 1, 1, " %")
        row.addWidget(self.btn_fire, 1)
        row.addWidget(self.fire_power)
        c.body.addLayout(row)
        lv.addWidget(c)

        c = Card("Macros")
        self.macro_buttons: list[QPushButton] = []
        mh = QLabel("Right-click a button to edit it")
        mh.setObjectName("muted")
        c.body.addWidget(mh)
        grid = QGridLayout()
        grid.setSpacing(6)
        for i in range(6):
            b = QPushButton()
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.clicked.connect(lambda _=False, n=i: self.run_macro(n))
            b.customContextMenuRequested.connect(lambda _pos, n=i: self.edit_macro(n))
            grid.addWidget(b, i // 3, i % 3)
            self.macro_buttons.append(b)
        c.body.addLayout(grid)
        lv.addWidget(c)
        self._refresh_macros()

        c = Card("Override")
        self.ov_feed = OverrideBox("Speed")
        self.ov_spin = OverrideBox("Power")
        self.ov_rapid = RapidBox()
        self.ov_feed.action.connect(self._ov_feed_action)
        self.ov_spin.action.connect(self._ov_spin_action)
        self.ov_rapid.action.connect(self._ov_rapid_action)
        for w in (self.ov_feed, self.ov_spin, self.ov_rapid):
            c.body.addWidget(w)
        lv.addWidget(c)
        lv.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(410)
        scroll.setMaximumWidth(440)
        split.addWidget(scroll)

        # ---- right column
        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 10, 10, 0)
        tv.setSpacing(8)
        self.alarm_bar = self._bar(theme.DANGER, "#3b1519", "#fecaca")
        self.alarm_label = QLabel()
        self.alarm_label.setWordWrap(True)
        bu = QPushButton("Unlock ($X)")
        bu.clicked.connect(lambda: self.send("$X"))
        bh = QPushButton("Homing ($H)")
        bh.clicked.connect(lambda: self.send("$H"))
        for w in (self.alarm_label, bu, bh):
            self.alarm_bar.layout().addWidget(w, 1 if w is self.alarm_label else 0)
        self.alarm_bar.hide()
        tv.addWidget(self.alarm_bar)
        self.door_bar = self._bar(theme.WARN, "#3a2d0c", "#fde68a")
        self.door_label = QLabel()
        self.door_label.setWordWrap(True)
        bd = QPushButton("Resume (~)")
        bd.setToolTip("Cycle start: leaves the Door state if GRBL no longer sees the door input active")
        bd.clicked.connect(lambda: self._rt(grbl.RT_RESUME))
        br = QPushButton("Soft reset")
        br.clicked.connect(self.soft_reset)
        for w in (self.door_label, bd, br):
            self.door_bar.layout().addWidget(w, 1 if w is self.door_label else 0)
        self.door_bar.hide()
        tv.addWidget(self.door_bar)
        self.error_bar = self._bar(theme.WARN, "#3a2d0c", "#fde68a")
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.btn_err_resume = QPushButton("Resume")
        self.btn_err_resume.clicked.connect(self._error_resume)
        self.btn_err_stop = QPushButton("Abort")
        self.btn_err_stop.setObjectName("danger")
        self.btn_err_stop.clicked.connect(self.stop_program)
        for w in (self.error_label, self.btn_err_resume, self.btn_err_stop):
            self.error_bar.layout().addWidget(w, 1 if w is self.error_label else 0)
        self.error_bar.hide()
        tv.addWidget(self.error_bar)
        self.laser_bar = self._bar(theme.WARN, "#3a2d0c", "#fde68a")
        self.laser_label = QLabel("Laser mode is OFF ($32=0): GRBL keeps the laser ON during rapid moves and burns the "
                                  "material along them. Enable laser mode before running a job.")
        self.laser_label.setWordWrap(True)
        bl = QPushButton("Enable laser mode ($32=1)")
        bl.clicked.connect(lambda: self.send("$32=1"))
        for w in (self.laser_label, bl):
            self.laser_bar.layout().addWidget(w, 1 if w is self.laser_label else 0)
        self.laser_bar.hide()
        tv.addWidget(self.laser_bar)
        self.limits_bar = self._bar(theme.WARN, "#3a2d0c", "#fde68a")
        self.limits_label = QLabel("Soft limits are ON ($20=1) but homing is off ($22=0). Without homing the machine zero is "
                                   "wherever the head was at power-up, so GRBL refuses every move beyond that point (jog "
                                   "error:15). Turn the soft limits off to move freely in both directions.")
        self.limits_label.setWordWrap(True)
        bs = QPushButton("Disable soft limits ($20=0)")
        bs.clicked.connect(lambda: self.send("$20=0"))
        for w in (self.limits_label, bs):
            self.limits_bar.layout().addWidget(w, 1 if w is self.limits_label else 0)
        self.limits_bar.hide()
        tv.addWidget(self.limits_bar)
        self.view = LaserView()
        self.view.setStyleSheet("border: 1px solid %s; border-radius: 10px;" % theme.BORDER)
        tv.addWidget(self.view, 1)
        add_view_buttons(self.view, [("Travel", "Show the moves with the laser off", self._toggle_rapids, True, False),
                                     ("Machine area", "Show the machine travel ($130/$131); needs homing ($22=1)",
                                      self._toggle_envelope, True, False)])
        right.addWidget(top)

        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 10, 10)
        tabs = QTabWidget()
        self.editor = CodeEditor()
        self.editor.textChanged.connect(self._editor_changed)
        self.editor.startFromLine.connect(self.run_from_line)
        self.console = Console()
        self.console.commandEntered.connect(self.send)
        prog_page = QWidget()
        pv = QVBoxLayout(prog_page)
        pv.setContentsMargins(8, 8, 8, 8)
        self.job_label = QLabel("No file loaded")
        self.job_label.setObjectName("muted")
        pv.addWidget(self.job_label)
        pv.addWidget(self.editor, 1)
        tabs.addTab(prog_page, "Program")
        tabs.addTab(self.console, "Console")
        self.tabs = tabs
        bv.addWidget(tabs)
        right.addWidget(bottom)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)
        split.addWidget(right)
        split.setStretchFactor(1, 1)

    @staticmethod
    def _bar(border: str, bg: str, fg: str) -> QFrame:
        f = QFrame()
        f.setStyleSheet(f"QFrame{{background:{bg}; border:1px solid {border}; border-radius:8px;}}"
                        f"QLabel{{background:transparent; color:{fg}; font-weight:600;}}")
        lay = QHBoxLayout(f)
        lay.setContentsMargins(12, 8, 12, 8)
        return f

    # ------------------------------------------------------------------ modes
    def set_mode(self, n: int) -> None:
        """0 = Laser Sender, 1 = SVG converter. The sender keeps running (streaming, status) in either mode."""
        self.mode = n
        self.stack.setCurrentIndex(n)
        for i, b in enumerate(self.btn_mode):
            b.setChecked(i == n)
        for a in self._sender_actions:
            a.setVisible(n == 0)
        if n == 0:
            self._update_enabled()

    def _send_from_converter(self, text: str, name: str) -> None:
        """'Send to Laser': hand the generated G-code to the Sender in memory (no file round-trip)."""
        if self.load_gcode_text(text, name):
            self.set_mode(0)
            self.sb_state.setText("G-code from the converter loaded into the Sender: set the work zero, frame, then press Run")

    def load_gcode_text(self, text: str, name: str) -> bool:
        if self._running:
            QMessageBox.information(self, APP_NAME, "You cannot load a program while one is running.")
            return False
        self.file_path = None
        self._display_name = name
        self.setWindowTitle(f"{name} — {TITLE}")
        self._loading_text = True
        self.editor.setPlainText(text)
        self._loading_text = False
        self._parse(text)
        self.tabs.setCurrentIndex(0)
        self._update_enabled()
        return True

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.sb_state = QLabel("Not connected")
        self.sb_buf = QLabel("")
        self.sb_buf.setObjectName("muted")
        self.sb_time = QLabel("")
        self.sb_time.setStyleSheet(f"font-family:{theme.MONO};")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        sb.addWidget(self.sb_state, 1)
        sb.addPermanentWidget(self.sb_buf)
        sb.addPermanentWidget(self.sb_time)
        sb.addPermanentWidget(self.progress)

    # ------------------------------------------------------------------ persistence
    def _restore(self) -> None:
        g = self.qs.value("geometry")
        if g:
            self.restoreGeometry(g)
        else:
            self.resize(1480, 940)
        self.jog_feed.setValue(int(self.qs.value("jog_feed", 3000)))
        self.step_combo.setCurrentIndex(int(self.qs.value("jog_step", 1)))
        self.chk_keys.setChecked(self.qs.value("keys", "false") == "true")
        self.chk_autohome.setChecked(self.qs.value("autohome", "false") == "true")
        self.frame_power.setValue(float(self.qs.value("frame_power", 0.0)))
        self.frame_feed.setValue(int(self.qs.value("frame_feed", 3000)))
        self.fire_power.setValue(float(self.qs.value("fire_power", 1.0)))
        i = self.baud_combo.findData(int(self.qs.value("baud", 115200)))
        self.baud_combo.setCurrentIndex(max(0, i))

    def closeEvent(self, e) -> None:
        if self._running:
            if QMessageBox.question(self, APP_NAME, "A job is running. Close anyway?") != QMessageBox.Yes:
                e.ignore()
                return
        self._fire_off()
        QApplication.instance().removeEventFilter(self)
        self.qs.setValue("geometry", self.saveGeometry())
        self.qs.setValue("jog_feed", self.jog_feed.value())
        self.qs.setValue("jog_step", self.step_combo.currentIndex())
        self.qs.setValue("keys", "true" if self.chk_keys.isChecked() else "false")
        self.qs.setValue("autohome", "true" if self.chk_autohome.isChecked() else "false")
        self.qs.setValue("frame_power", self.frame_power.value())
        self.qs.setValue("frame_feed", self.frame_feed.value())
        self.qs.setValue("fire_power", self.fire_power.value())
        self.qs.setValue("baud", self.baud_combo.currentData())
        self.qs.setValue("port", self.port_combo.currentData())
        if self.conn and self.conn.isRunning():
            self.conn.disconnect_port()
            if not self.conn.wait(3000):    # never let a stuck serial thread keep the app alive
                self.conn.terminate()
                self.conn.wait(500)
        super().closeEvent(e)

    # ------------------------------------------------------------------ ports / connection
    def _list_ports(self):
        items = []
        for p in list_ports.comports():
            desc = p.description if p.description and p.description != "n/a" else "Serial port"
            items.append((p.device, f"{p.device}  —  {desc}"))
        items.sort(key=lambda t: (0 if ("ttyUSB" in t[0] or "ttyACM" in t[0]) else 1, t[0]))
        items.append((SIM_PORT, "GRBL simulator (demo)"))
        return items

    def _refresh_ports(self) -> None:
        cur = self.port_combo.currentData() or self.qs.value("port")
        items = self._list_ports()
        self._ports_sig = [d for d, _ in items]
        self.port_combo.clear()
        for dev, label in items:
            self.port_combo.addItem(label, dev)
        i = self.port_combo.findData(cur)
        self.port_combo.setCurrentIndex(i if i >= 0 else 0)

    def _auto_refresh_ports(self) -> None:
        if self.conn and self.conn.isRunning():
            return
        if [d for d, _ in self._list_ports()] != self._ports_sig:
            self._refresh_ports()

    @property
    def connected(self) -> bool:
        return (self.conn is not None and self.conn.isRunning()
                and self.status.state not in ("Disconnected", "Connecting"))

    def toggle_connection(self) -> None:
        if self.conn and self.conn.isRunning():
            self._fire_off()
            self.conn.disconnect_port()
            return
        port = self.port_combo.currentData()
        if not port:
            return
        self.conn = GrblConnection(port, int(self.baud_combo.currentData()))
        c = self.conn
        c.connectionChanged.connect(self.on_connection)
        c.statusChanged.connect(self.on_status)
        c.console.connect(self.console.append)
        c.lineAcked.connect(self.on_line_acked)
        c.programState.connect(self.on_program_state)
        c.settingsChanged.connect(self.on_settings)
        c.alarmRaised.connect(self.on_alarm)
        self._door_reset_done = False
        self._autohome_pending = self.chk_autohome.isChecked()
        self.status = Status(state="Connecting")
        self.badge.set_state("Connecting")
        self.btn_connect.setText("Connecting…")
        self.btn_connect.setEnabled(False)
        self.port_combo.setEnabled(False)
        self.baud_combo.setEnabled(False)
        c.start()

    def on_connection(self, ok: bool, msg: str) -> None:
        self.sb_state.setText(msg)
        self.btn_connect.setEnabled(True)
        if ok:
            self.btn_connect.setText("Disconnect")
            self.console.append(msg, "info")
        else:
            self.btn_connect.setText("Connect")
            self.port_combo.setEnabled(True)
            self.baud_combo.setEnabled(True)
            self.status = Status()
            self.badge.set_state("Disconnected")
            self.alarm_bar.hide()
            self.door_bar.hide()
            self.laser_bar.hide()
            self.limits_bar.hide()
            self.console.append(msg, "error" if "Cannot open" in msg or "lost" in msg or "No response" in msg else "info")
            if "Errno 13" in msg or "ermission" in msg:
                QMessageBox.warning(self, APP_NAME,
                                    f"{msg}\n\nOn Ubuntu add your user to the 'dialout' group:\n"
                                    "  sudo usermod -aG dialout $USER\n(then log out and back in).\n\n"
                                    "If a board with a CH340 chip disappears right after plugging it in, "
                                    "remove brltty:  sudo apt remove brltty")
            elif "Cannot open" in msg or "No response" in msg:
                QMessageBox.warning(self, APP_NAME, msg)
            self._finish_run_ui()
        self._update_enabled()

    # ------------------------------------------------------------------ status handling
    def on_status(self, st: Status) -> None:
        self.status = st
        for i, a in enumerate("XY"):
            self.axes[a].set_values(st.wpos[i], st.mpos[i])
        self.view.set_tool(st.wpos)
        self.badge.set_state(st.state)
        self.act_check.setChecked(st.base_state == "Check")
        smax = self._s_max()
        self.live.setText(f"Speed {st.feed:.0f} mm/min    Power {st.spindle:.0f} S ({100 * st.spindle / smax:.0f} %)")
        self.ov_feed.set_value(st.ov_feed)
        self.ov_rapid.set_value(st.ov_rapid)
        self.ov_spin.set_value(st.ov_spindle)
        port = "Simulator" if self.port_combo.currentData() == SIM_PORT else self.port_combo.currentData()
        self.sb_state.setText(f"{port}  •  {st.state}" + (f"  •  Pin: {st.pins}" if st.pins else ""))
        self.sb_buf.setText(f"Buffer: planner {st.planner_free}  rx {st.rx_free}" if st.planner_free else "")
        if st.base_state == "Door" and self.conn and not self._door_reset_done and not self._running:
            # ESP32 boards often read the (missing) door input as open at power-up and work after one soft reset
            self._door_reset_done = True
            self.console.append("Door state right after connecting: automatic soft reset (once)", "info")
            self.conn.reset()
        self._update_door_bar(st)
        self._maybe_autohome(st)
        if st.base_state == "Alarm":
            self.alarm_label.setText(self._alarm_text or "Machine in ALARM. Check the position, then unlock ($X) or run homing.")
            self.alarm_bar.show()
        else:
            self.alarm_bar.hide()
            self._alarm_text = ""
            self._alarm_code = -1
        self._update_envelope()
        self._update_enabled()

    def _maybe_autohome(self, st: Status) -> None:
        """Once per connection, when the board is ready (settings read, not in Door/Home/Run): run $H."""
        if not self._autohome_pending or not self.conn or self._running:
            return
        if 22 not in self.conn.settings or st.base_state not in ("Idle", "Alarm"):
            return
        self._autohome_pending = False
        if self.conn.settings.get(22):
            self.console.append("Homing after connecting ($H)…", "info")
            self.conn.send_line("$H")
        else:
            self.console.append("Auto-home skipped: homing is disabled on the board ($22=0). Enable it in the GRBL "
                                "settings ($22=1) and set $23 for the direction of your limit switches.", "msg")

    def _update_door_bar(self, st: Status) -> None:
        if st.base_state != "Door":
            self.door_bar.hide()
            return
        sub = st.state.split(":")[1] if ":" in st.state else ""
        pin = "D" in st.pins
        if sub == "0" and not pin:
            msg = "GRBL is in the Door state (door closed) and waits for a cycle start. Press Resume (~)."
        else:
            msg = ("GRBL reports the safety DOOR input as active" + (" (Pn:D)" if pin else "")
                   + ". This laser has no door: the board is reading its door/feed-hold pin as triggered "
                   "(floating or inverted input). Try Resume (~) or a soft reset; if the state comes back at once, "
                   "the pin must be fixed in the firmware or wiring, GRBL offers no setting to ignore it.")
        self.door_label.setText(msg + f"   [state: {st.state}   pins: {st.pins or '-'}]")
        self.door_bar.show()

    def on_alarm(self, code: int, msg: str) -> None:
        self._alarm_code = code
        self._alarm_text = f"ALARM {code}: {msg}"
        if code in (1, 2):
            self._alarm_text += ("  →  move the head away from the limit switch with the jog pad (it unlocks by itself), "
                                 "then run Homing.")
        self.alarm_label.setText(self._alarm_text)
        self.alarm_bar.show()

    def _s_max(self) -> float:
        s = self.conn.settings if self.conn else {}
        return float(s.get(30, 1000) or 1000)

    def _homing_enabled(self) -> bool:
        s = self.conn.settings if self.conn else {}
        return bool(s.get(22, 1))

    def on_settings(self, d: dict) -> None:
        if d.get(110) and d.get(111):
            self.jog_feed.setMaximum(int(max(d.get(110, 0), d.get(111, 0))))
        self.laser_bar.setVisible(d.get(32) == 0)
        self.limits_bar.setVisible(bool(d.get(20)) and not d.get(22))
        self.view.s_max = float(d.get(30, 1000) or 1000)
        self.converter.set_machine(d.get(130), d.get(131), d.get(30))
        self._update_envelope()
        if self._settings_dlg is not None and self._settings_dlg.isVisible():
            self._settings_dlg.load(d)
        if self.program is not None:
            self._check_travel()
        self._update_enabled()

    def show_settings(self) -> None:
        if not self.connected:
            return
        dlg = GrblSettingsDialog(self.conn.settings, self)
        dlg.applyRequested.connect(self._write_settings)
        dlg.refreshRequested.connect(lambda: self.send("$$"))
        self._settings_dlg = dlg
        self.send("$$")
        dlg.show()

    def _write_settings(self, cmds: list) -> None:
        if self._running:
            return
        if QMessageBox.question(self, "Write settings", "Write to the EEPROM:\n\n" + "\n".join(cmds)) != QMessageBox.Yes:
            return
        for c in cmds:
            self.send(c)

    def _update_envelope(self) -> None:
        """Machine box in work coordinates. Only meaningful with homing ($22=1): without it the machine zero is
        wherever the head was at power-up."""
        s = self.conn.settings if self.conn else {}
        if all(k in s for k in (130, 131)) and s.get(22):
            hi = -np.array(self.status.wco[:2])
            lo = hi - np.array([s[130], s[131]])
            self.view.set_envelope(lo, hi)
        else:
            self.view.set_envelope(None, None)

    def _toggle_rapids(self, on: bool) -> None:
        self.view.show_rapids = on
        self.view._invalidate()
        self.view.update()

    def _toggle_envelope(self, on: bool) -> None:
        self.view.show_envelope = on
        self.view.update()

    def _update_enabled(self) -> None:
        st = self.status.base_state
        conn = self.connected
        idle = bool(conn and st in ("Idle", "Check") and not self._running)
        self.act_run.setEnabled(bool(idle and self.program and self.program.commands))
        self.act_frame.setEnabled(bool(idle and st == "Idle" and self.program and self._frame_rect()))
        self.btn_frame.setEnabled(self.act_frame.isEnabled())
        self.act_pause.setEnabled(bool(conn and (self._running or st in ("Run", "Hold"))))
        self.act_stop.setEnabled(bool(conn and (self._running or st in ("Run", "Hold", "Jog"))))
        self.act_pause.setIcon(icon("play" if self._paused or st == "Hold" else "pause"))
        self.act_pause.setText("Resume" if self._paused or st == "Hold" else "Pause")
        self.act_home.setEnabled(bool(conn and st in ("Idle", "Alarm") and self._homing_enabled()))
        self.act_home.setToolTip("Homing cycle ($H)" if self._homing_enabled() else "Homing is disabled on this machine ($22=0)")
        self.act_unlock.setEnabled(bool(conn and st == "Alarm"))
        self.act_reset.setEnabled(conn)
        self.act_check.setEnabled(idle)
        self.act_settings.setEnabled(conn)
        self.act_reload.setEnabled(self.file_path is not None and not self._running)
        self.pad.setEnabled(bool(conn and not self._running and (st in ("Idle", "Jog") or (st == "Alarm" and self._alarm_code in (1, 2)))))
        for r in self.axes.values():
            r.setEnabled(bool(idle))
        self.chk_fire.setEnabled(bool(conn and st == "Idle" and not self._running) or self._firing)
        self.btn_fire.setEnabled(bool(self.chk_fire.isChecked() and conn and (st == "Idle" or self._firing) and not self._running))
        for w in (self.ov_feed, self.ov_rapid, self.ov_spin):
            w.setEnabled(conn)
        self.editor.setReadOnly(self._running)

    # ------------------------------------------------------------------ sending
    def send(self, text: str) -> None:
        if not self.connected:
            self.console.append("Not connected", "error")
            return
        self.conn.send_line(text)

    # ------------------------------------------------------------------ macros
    def _macro(self, n: int) -> tuple[str, str]:
        return (str(self.qs.value(f"macro{n}_name", "")), str(self.qs.value(f"macro{n}_cmd", "")))

    def _refresh_macros(self) -> None:
        for i, b in enumerate(self.macro_buttons):
            name, cmd = self._macro(i)
            b.setText(name or "＋")
            b.setToolTip(cmd.replace(";", "\n") if cmd else "Right-click to define a macro")

    def run_macro(self, n: int) -> None:
        name, cmd = self._macro(n)
        if not cmd:
            self.edit_macro(n)
            return
        if self._running:
            return
        for line in cmd.replace(";", "\n").splitlines():
            if line.strip():
                self.send(line.strip())

    def edit_macro(self, n: int) -> None:
        name, cmd = self._macro(n)
        new_name, ok = QInputDialog.getText(self, "Macro", "Button name (empty to delete):", text=name)
        if not ok:
            return
        if not new_name.strip():
            self.qs.setValue(f"macro{n}_name", "")
            self.qs.setValue(f"macro{n}_cmd", "")
        else:
            new_cmd, ok = QInputDialog.getMultiLineText(self, "Macro", "Commands (one per line):", cmd.replace(";", "\n"))
            if not ok:
                return
            self.qs.setValue(f"macro{n}_name", new_name.strip())
            self.qs.setValue(f"macro{n}_cmd", ";".join(l.strip() for l in new_cmd.splitlines() if l.strip()))
        self._refresh_macros()

    def soft_reset(self) -> None:
        if self.conn:
            self._firing = False
            self.conn.reset()
            self._finish_run_ui()

    # ------------------------------------------------------------------ laser: test fire and frame
    def _power_s(self, pct: float) -> int:
        return 0 if pct <= 0 else max(1, int(round(pct / 100.0 * self._s_max())))

    def _fire_on(self) -> None:
        if not (self.connected and self.chk_fire.isChecked() and self.status.base_state == "Idle") or self._firing:
            return
        s = self._power_s(self.fire_power.value())
        if not s:
            return
        self._firing = True
        # laser mode only fires in a feed motion mode: select G1 (no motion), then switch the laser on
        self.conn.send_line("G1F100", silent=True)
        self.conn.send_line(f"M3S{s}", silent=True)

    def _fire_off(self) -> None:
        if self._firing:
            self._firing = False
            if self.conn and self.conn.isRunning():
                self.conn.send_line("M5", silent=True)
                self.conn.send_line("G0", silent=True)

    def _frame_rect(self):
        if not (self.program and len(self.program.path)):
            return None
        lo, hi = self.program.bounds_min, self.program.bounds_max
        if hi[0] - lo[0] < 1e-6 and hi[1] - lo[1] < 1e-6:
            return None
        return lo[0], lo[1], hi[0], hi[1]

    def frame_job(self) -> None:
        """Trace the bounding box of the burned area: with 0 % the laser stays off, otherwise a faint pointer line."""
        r = self._frame_rect()
        if not (r and self.connected and not self._running and self.status.base_state == "Idle"):
            return
        x0, y0, x1, y1 = r
        feed = self.frame_feed.value()
        s = self._power_s(self.frame_power.value())
        laser_mode = bool(self.conn.settings.get(32))
        if s and not laser_mode:
            self.console.append("Frame: laser mode ($32) is off, the pointer power is ignored (the laser stays off)", "msg")
            s = 0
        cmds = ["G21", "G90", f"G0X{x0:.3f}Y{y0:.3f}"]
        if s:
            cmds.append(f"M3S{s}")        # modal motion is still G0 here: nothing is burned until the first G1
        cmds += [f"G1X{x1:.3f}F{feed}", f"G1Y{y1:.3f}", f"G1X{x0:.3f}", f"G1Y{y0:.3f}"]
        if s:
            cmds.append("M5")
        cmds.append("G0")
        for c in cmds:
            self.conn.send_line(c, silent=False)
        self.view.set_frame((x0, y0), (x1, y1))
        self.console.append(f"Framing X {x0:.1f}…{x1:.1f}  Y {y0:.1f}…{y1:.1f}", "info")

    # ------------------------------------------------------------------ overrides
    def _ov_feed_action(self, a: str) -> None:
        m = {"minus10": grbl.RT_FEED_MINUS10, "minus1": grbl.RT_FEED_MINUS1, "reset": grbl.RT_FEED_RESET,
             "plus1": grbl.RT_FEED_PLUS1, "plus10": grbl.RT_FEED_PLUS10}
        self._rt(m[a])

    def _ov_spin_action(self, a: str) -> None:
        m = {"minus10": grbl.RT_SPINDLE_MINUS10, "minus1": grbl.RT_SPINDLE_MINUS1, "reset": grbl.RT_SPINDLE_RESET,
             "plus1": grbl.RT_SPINDLE_PLUS1, "plus10": grbl.RT_SPINDLE_PLUS10}
        self._rt(m[a])

    def _ov_rapid_action(self, a: str) -> None:
        self._rt({"r25": grbl.RT_RAPID_25, "r50": grbl.RT_RAPID_50, "r100": grbl.RT_RAPID_100}[a])

    def _rt(self, b: bytes) -> None:
        if self.connected:
            self.conn.send_realtime(b)

    # ------------------------------------------------------------------ jog
    def _max_travel(self, axis: int) -> float:
        s = self.conn.settings if self.conn else {}
        return float(s.get(130 + axis, 0) or 0) or 400.0

    def _safe_bounds(self, axis: int):
        """(lo, hi) machine coordinates the head may be sent to, or None without homing. After homing GRBL parks the head
        `$27` mm from the limit switch and reports MPos = -$27: the machine zero is the switch itself, so commands must
        stop a little before the switch end ($23 tells which end it is) or the hard limit alarms and the position is lost."""
        s = self.conn.settings if self.conn else {}
        if not s.get(22) or not s.get(130 + axis):
            return None
        lo, hi = -float(s[130 + axis]), 0.0
        margin = float(s.get(27, 1.0) or 0.0) + 0.2
        if int(s.get(23, 0) or 0) & (1 << axis):
            lo += margin            # homes toward negative: the switch is at the -travel end
        else:
            hi -= margin
        return lo, hi

    def _clamp(self, axis: int, target: float, cur: float) -> float:
        """Keep a machine-coordinate target inside the safe bounds, never reversing the requested direction."""
        b = self._safe_bounds(axis)
        if b is None:
            return target
        t = min(max(target, b[0]), b[1])
        if (target - cur) > 0 and t < cur:
            return cur
        if (target - cur) < 0 and t > cur:
            return cur
        return t

    def goto_zero(self, axes: str) -> None:
        """Rapid to the work zero of the given axes, stopping short of the limit switch when the zero lies on it."""
        if not self.connected:
            self.console.append("Not connected", "error")
            return
        words = []
        for a in axes:
            i = "XY".index(a)
            wco, cur = self.status.wco[i], self.status.mpos[i]
            tgt_m = self._clamp(i, wco, cur)
            if abs(tgt_m - wco) > 1e-3:
                self.console.append(f"{a}0 is at the limit switch: stopping {abs(tgt_m - wco):.1f} mm short of it", "msg")
            if abs(tgt_m - cur) > 1e-3:
                words.append(f"{a}{tgt_m - wco:.3f}")
        if words:
            self.send("G90G0" + "".join(words))

    def on_jog_press(self, dx: int, dy: int, dz: int) -> None:
        st = self.status.base_state
        unlock = st == "Alarm" and self._alarm_code in (1, 2)       # hard/soft limit: unlock, then move away
        if not self.connected or self._running or not (st in ("Idle", "Jog") or unlock):
            return
        step = self.step_combo.currentData()
        feed = self.jog_feed.value()
        d = (dx, dy)
        words = []
        bounded = (not unlock) and all(self._safe_bounds(i) for i in range(2))
        absolute = step is None and bounded
        for i, a in enumerate("XY"):
            if not d[i]:
                continue
            cur = self.status.mpos[i]
            if absolute:                                  # continuous: aim at the safe end, jog-cancel stops on release
                lo, hi = self._safe_bounds(i)
                target = self._clamp(i, hi if d[i] > 0 else lo, cur)
                if abs(target - cur) > 1e-3:
                    words.append(f"{a}{target:.3f}")
            else:
                dist = d[i] * (step if step is not None else self._max_travel(i))
                if bounded:
                    dist = self._clamp(i, cur + dist, cur) - cur
                if abs(dist) > 1e-3:
                    words.append(f"{a}{dist:.3f}")
        if not words:
            return
        if unlock:
            self.conn.send_line("$X", silent=True)
        if step is None:
            self._continuous = True
        mode = "G53G21G90" if absolute else "G91G21"
        self.conn.send_line(f"$J={mode}{''.join(words)}F{feed}", silent=True)

    def on_jog_release(self) -> None:
        if self._continuous:
            self._continuous = False
            self.conn.send_realtime(grbl.RT_JOG_CANCEL)

    def eventFilter(self, obj, ev) -> bool:
        if (self.mode == 0 and self.chk_keys.isChecked() and ev.type() in (QEvent.KeyPress, QEvent.KeyRelease)
                and self.isActiveWindow()):
            fw = QApplication.focusWidget()
            if not isinstance(fw, (QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox)):
                keys = (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down)
                if not ev.isAutoRepeat():
                    d = {Qt.Key_Left: (-1, 0, 0), Qt.Key_Right: (1, 0, 0), Qt.Key_Up: (0, 1, 0), Qt.Key_Down: (0, -1, 0)}.get(ev.key())
                    if d:
                        if ev.type() == QEvent.KeyPress:
                            self.on_jog_press(*d)
                        else:
                            self.on_jog_release()
                        return True
                elif ev.key() in keys:
                    return True
        return super().eventFilter(obj, ev)

    # ------------------------------------------------------------------ files
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        for u in e.mimeData().urls():
            if u.isLocalFile():
                path = u.toLocalFile()
                if path.lower().endswith(".svg"):
                    self.set_mode(1)
                    self.converter.load_svg(path)
                else:
                    self.set_mode(0)
                    self.load_file(path)
                break

    def open_file(self) -> None:
        d = self.qs.value("last_dir", os.path.expanduser("~"))
        path, _ = QFileDialog.getOpenFileName(self, "Open G-code", d,
                                              "G-code (*.nc *.gcode *.ngc *.tap *.gc *.cnc *.txt);;All files (*)")
        if path:
            self.load_file(path)

    def reload_file(self) -> None:
        if self.file_path:
            self.load_file(self.file_path)

    def load_file(self, path: str) -> None:
        if self._running:
            QMessageBox.information(self, APP_NAME, "You cannot load a file while one is running.")
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, str(e))
            return
        self.file_path = path
        self._display_name = os.path.basename(path)
        self.qs.setValue("last_dir", os.path.dirname(path))
        self.setWindowTitle(f"{os.path.basename(path)} — {TITLE}")
        self._loading_text = True
        self.editor.setPlainText(text)
        self._loading_text = False
        self._parse(text)
        self.tabs.setCurrentIndex(0)

    def save_file(self) -> None:
        path = self.file_path
        if not path or not os.access(os.path.dirname(path) or ".", os.W_OK):
            path, _ = QFileDialog.getSaveFileName(self, "Save G-code", self.qs.value("last_dir", os.path.expanduser("~")),
                                                  "G-code (*.nc *.gcode)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.editor.toPlainText())
        self.file_path = path
        self.setWindowTitle(f"{os.path.basename(path)} — {TITLE}")
        self.sb_state.setText(f"Saved {path}")

    def _rates(self):
        s = self.conn.settings if self.conn else {}
        return (s.get(110, 6000.0), s.get(111, 6000.0), s.get(112, 800.0))

    def _parse(self, text: str) -> None:
        self._parse_serial += 1
        self.job_label.setText("Analysing the file…")
        t = ParseThread(text, self._rates(), self._parse_serial, self)
        t.parsed.connect(self._on_parsed)
        t.finished.connect(t.deleteLater)
        self._parse_thread = t
        t.start()

    def _on_parsed(self, prog: Program, serial_no: int) -> None:
        if serial_no != self._parse_serial:
            return
        self.program = prog
        self.view.set_frame(None, None)
        self.view.s_max = self._s_max()
        self.view.set_program(prog)
        self._acked_idx = -1
        self.editor.set_exec_line(-1)
        self.editor.errors = {int(w.split(":")[0].split()[1]) - 1 for w in prog.warnings if w.startswith("Line ")}
        self.editor.gutter.update()
        sz = prog.size
        name = self._display_name or "(unnamed)"
        self.job_label.setText(
            f"<b>{name}</b> &nbsp;•&nbsp; {len(prog.commands)} commands &nbsp;•&nbsp; size "
            f"{sz[0]:.1f} × {sz[1]:.1f} mm &nbsp;•&nbsp; burning {prog.burn_length:.0f} mm "
            f"&nbsp;•&nbsp; max power S{prog.max_spindle:g} &nbsp;•&nbsp; estimated time ≈ {format_duration(prog.total_time)}"
            + (f" &nbsp;•&nbsp; <span style='color:{theme.WARN}'>{len(prog.warnings)} warnings</span>" if prog.warnings else ""))
        self.sb_time.setText(f"≈ {format_duration(prog.total_time)}")
        self.progress.setValue(0)
        for w in prog.warnings[:30]:
            self.console.append(w, "msg")
        self._check_travel()
        self._update_enabled()

    def _check_travel(self) -> None:
        if not self.program or not self.conn:
            return
        s = self.conn.settings
        if all(k in s for k in (130, 131)):
            sz = self.program.size
            over = [a for a, v, k in zip("XY", sz, (130, 131)) if v > s[k] + 1e-6]
            if over:
                self.console.append(f"Warning: the job is larger than the machine area on {', '.join(over)}", "msg")

    def _editor_changed(self) -> None:
        if not self._loading_text and not self._running:
            self._reparse_timer.start()

    def _reparse_editor(self) -> None:
        if not self._running:
            self._parse(self.editor.toPlainText())

    # ------------------------------------------------------------------ run control
    def _confirm_start(self) -> bool:
        """Checks before burning: laser mode, power above $30, parser warnings. False = do not start."""
        prog = self.program
        if prog is None:
            return False
        s = self.conn.settings
        if prog.uses_laser and s.get(32) == 0:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Laser mode is off")
            box.setText("Laser mode ($32) is OFF.\n\nGRBL keeps the laser ON during rapid moves (G0), so the material "
                        "would be burned along every travel move.")
            enable = box.addButton("Enable laser mode and run", QMessageBox.AcceptRole)
            anyway = box.addButton("Run anyway", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is enable:
                self.conn.send_line("$32=1", silent=True)
            elif box.clickedButton() is not anyway:
                return False
        notes = list(prog.warnings[:10])
        if prog.max_spindle > self._s_max() + 1e-6 and s.get(30):
            notes.append(f"The program uses S{prog.max_spindle:g}, above the maximum S{self._s_max():g} ($30): the power is clipped.")
        if not prog.uses_laser:
            notes.append("The program never switches the laser on (no M3/M4): nothing will be burned.")
        if notes:
            if QMessageBox.warning(self, "Program warnings", "\n".join(notes) + "\n\nRun anyway?",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return False
        return True

    def run_program(self) -> None:
        if not (self.program and self.connected) or not self._confirm_start():
            return
        self._begin_run(self.program.commands)

    def run_from_line(self, line: int) -> None:
        if not (self.program and self.connected) or self._running or self.status.base_state != "Idle":
            return
        if QMessageBox.question(
                self, "Resume from line",
                f"Restart from line {line + 1}?\n\nThe state (units, speed, laser mode and power) will be restored with the "
                "laser off, the head will move to the XY of that line and the job continues. Check that the work zero "
                "is the original one and that the material has not moved.") != QMessageBox.Yes:
            return
        pre = [(line, c) for c in preamble_for(self.program, line)]
        rest = [(i, c) for i, c in self.program.commands if i >= line]
        self._begin_run(pre + rest)

    def _begin_run(self, cmds: list[tuple[int, str]]) -> None:
        self._fire_off()
        self._run_cmds = cmds
        self._error_count = 0
        self._last_error = ""
        self._acked_idx = -1
        self._running = True
        self._paused = False
        self._t0 = time.monotonic()
        self._paused_total = 0.0
        self.view.set_progress_line(-1)
        self.conn.start_program(cmds)
        self.tabs.setCurrentIndex(0)
        self._update_enabled()

    def toggle_pause(self) -> None:
        if not self.connected:
            return
        if self._running:
            if self._paused or self.status.base_state == "Hold":
                if self._paused:
                    self._paused_total += time.monotonic() - self._paused_at
                self.conn.resume()
                self._paused = False
            else:
                self._paused_at = time.monotonic()
                self.conn.pause()
                self._paused = True
        elif self.status.base_state == "Hold":
            self.conn.send_realtime(grbl.RT_RESUME)
        else:
            self.conn.send_realtime(grbl.RT_HOLD)
        self._update_enabled()

    def stop_program(self) -> None:
        self._fire_off()
        if self.connected:
            if self._running:
                self.conn.stop()
            else:
                self.conn.send_realtime(grbl.RT_JOG_CANCEL)
                if self.status.base_state in ("Run", "Hold"):
                    self.conn.stop()

    def on_program_state(self, s: str) -> None:
        if s != "error":
            self.error_bar.hide()
        if s == "running":
            self._paused = False
        elif s == "paused":
            self._paused = True
        elif s == "error":
            # non-modal: a blocking dialog per rejected line stacks up and freezes the main window
            self._paused = True
            self._paused_at = time.monotonic()
            self.error_label.setText(self._last_error or "GRBL rejected a line. The job is paused.")
            self.error_bar.show()
        elif s == "done":
            el = time.monotonic() - self._t0 - self._paused_total
            self.console.append(f"Job completed in {format_duration(el)}", "info")
            self.sb_state.setText(f"Job completed in {format_duration(el)}")
            self._finish_run_ui(done=True)
        elif s == "aborted":
            self.console.append("Job aborted", "info")
            self._finish_run_ui()
        self._update_enabled()

    def _finish_run_ui(self, done: bool = False) -> None:
        self._running = False
        self._paused = False
        if done:
            self.progress.setValue(1000)
            self.view.set_progress_line(10 ** 9)
        self._update_enabled()

    def on_line_acked(self, idx: int, ok: bool, code: int) -> None:
        self._acked_idx = idx
        if not ok and 0 <= idx < len(self._run_cmds):
            src, cmd = self._run_cmds[idx]
            self._error_count += 1
            if self._error_count == 1:      # keep the first rejected line: it is the cause
                self._first_error = (f"GRBL rejected line {src + 1}: {cmd}  →  error:{code} "
                                     f"{grbl.ERRORS.get(code, '')}. Job paused.")
            self._last_error = self._first_error + (f"  (+{self._error_count - 1} more lines rejected, see Console)"
                                                    if self._error_count > 1 else "")
            if self.error_bar.isVisible():
                self.error_label.setText(self._last_error)

    def _error_resume(self) -> None:
        self.error_bar.hide()
        self.toggle_pause()

    def _estimate_exec_line(self, idx: int) -> int:
        """GRBL acks a line when it enters the planner (up to ~15 blocks ahead of the head), so snap the real head
        position onto the nearest path segment among the most recently sent lines."""
        src_hi = self._run_cmds[idx][0]
        if not self.program or not len(self.program.path):
            return src_hi
        src_lo = self._run_cmds[max(0, idx - 30)][0]
        path = self.program.path
        i0 = int(np.searchsorted(path.line, src_lo, "left"))
        i1 = int(np.searchsorted(path.line, src_hi, "right"))
        if i1 <= i0:
            return src_hi
        a = path.start[i0:i1].astype(float)
        ab = path.end[i0:i1].astype(float) - a
        p = np.asarray(self.status.wpos, float)
        t = np.clip(((p - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0.0, 1.0)
        d = np.linalg.norm(a + t[:, None] * ab - p, axis=1)
        if d.min() > 2.0:
            return src_hi
        k = int(np.nonzero(d <= d.min() + 0.03)[0][-1])
        return int(path.line[i0 + k])

    def _ui_tick(self) -> None:
        if not self._running or self._acked_idx < 0 or not self._run_cmds:
            return
        idx = min(self._acked_idx, len(self._run_cmds) - 1)
        src = self._estimate_exec_line(idx)
        self.editor.set_exec_line(src)
        self.view.set_progress_line(src)
        self.progress.setValue(int(1000 * (idx + 1) / len(self._run_cmds)))
        el = (self._paused_at if self._paused else time.monotonic()) - self._t0 - self._paused_total
        rem = 0.0
        if self.program:
            rem = self.program.time_remaining(src) * 100.0 / max(self.status.ov_feed, 10)
        self.sb_time.setText(f"{format_duration(el)}  /  remaining ≈ {format_duration(rem)}")
