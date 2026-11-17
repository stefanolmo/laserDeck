"""SVG Converter mode: load an SVG, decide for EVERY path what the laser does (off / line / fill / fill + line) and with
which power, speed and passes, preview it, and produce the G-code (save it or send it to the Sender)."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import fields

from PySide6.QtCore import QRectF, QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QTableWidgetSelectionRange, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from . import cam, theme
from .canvas import MODE_COLORS, DesignView, LaserView, add_view_buttons
from .gcode import Program, format_duration, parse_program
from .svgimport import SvgDoc, SvgShape, parse_svg
from .widgets import Card, CodeEditor

MODES = [("off", "Off (skip)"), ("line", "Line (outline)"), ("fill", "Fill (hatch)"), ("both", "Fill + Line")]
MODE_SHORT = {"off": "Off", "line": "Line", "fill": "Fill", "both": "Fill + Line"}
ORIGINS = [("bottom-left", "Bottom-left of the design"), ("top-left", "Top-left of the design (Y negative)"),
           ("center", "Centre of the design"), ("page", "SVG page, bottom-left (bed position)")]
ORDERS = [("engrave-first", "Fill first, lines last (recommended)"), ("listed", "As listed in the table")]
LASER_CMDS = [("M4", "M4 dynamic power (recommended)"), ("M3", "M3 constant power")]
LINE_FIELDS = ("line_power", "line_speed", "line_passes", "offset")
FILL_FIELDS = ("fill_power", "fill_speed", "fill_passes", "interval", "angle", "bidir", "crosshatch", "overscan")


class Switch(QCheckBox):
    """Toggle switch (pill with a sliding knob) with a text label."""

    def sizeHint(self):
        sh = super().sizeHint()
        return QSize(44 + 10 + self.fontMetrics().horizontalAdvance(self.text()), max(sh.height(), 24))

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on, en = self.isChecked(), self.isEnabled()
        h = 20
        y = (self.height() - h) / 2
        track = QColor(theme.ACCENT if on else "#2c3847")
        if not en:
            track = QColor("#1c3a37" if on else "#171e26")
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, y, 38, h), h / 2, h / 2)
        p.setBrush(QColor("#04211d" if on else theme.TEXT) if en else QColor("#4d5866"))
        p.drawEllipse(QRectF(20 if on else 2, y + 2, h - 4, h - 4))
        p.setPen(QColor(theme.TEXT if en else "#4d5866"))
        p.drawText(QRectF(48, 0, self.width() - 48, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())


class GenThread(QThread):
    done = Signal(object, object, int)

    def __init__(self, doc, settings, job, name, serial_no, parent=None):
        super().__init__(parent)
        self.doc, self.settings, self.job, self.name, self.serial_no = doc, settings, job, name, serial_no

    def run(self) -> None:
        try:
            res = cam.generate(self.doc, self.settings, self.job, self.name)
            prog = parse_program(res.gcode) if res.gcode else None
        except Exception as e:  # never let a CAM failure kill the thread silently
            res = cam.CamResult(warnings=[f"Conversion error: {e}"])
            prog = None
        self.done.emit(res, prog, self.serial_no)


def _dspin(lo, hi, val, dec=2, suffix="", step=None) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    if step:
        s.setSingleStep(step)
    return s


def _combo(items) -> QComboBox:
    c = QComboBox()
    for key, label in items:
        c.addItem(label, key)
    return c


def _swatch(shape: SvgShape) -> QIcon:
    pm = QPixmap(16, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    if shape.fill:
        p.setBrush(QColor(shape.fill))
    else:
        p.setBrush(Qt.NoBrush)
    p.setPen(QColor(shape.stroke) if shape.stroke else QColor(theme.BORDER))
    p.drawRoundedRect(2, 2, 12, 12, 3, 3)
    p.end()
    return QIcon(pm)


class ConverterPage(QWidget):
    sendRequested = Signal(str, str)     # gcode text, display name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.qs = QSettings("laserdeck", "laserdeck")
        self.doc: SvgDoc | None = None
        self.svg_path: str | None = None
        self._svg_text = ""
        self.settings: dict[str, cam.PathSettings] = {}
        self.result: cam.CamResult | None = None
        self.program: Program | None = None
        self.bed: tuple[float, float] | None = None
        self._serial = 0
        self._thread: GenThread | None = None
        self._loading = False
        self._dirty = False
        self._fit_next = True
        self._row: dict[str, int] = {}
        self._timer = QTimer(self, singleShot=True, interval=350)
        self._timer.timeout.connect(self.generate_now)
        self._build()
        self._load_widgets()
        self._update_buttons()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 6, 10)
        lv.setSpacing(10)
        q = self.qs

        # --- file
        c = Card("SVG file")
        row = QHBoxLayout()
        b = QPushButton("Open SVG…")
        b.setObjectName("primary")
        b.clicked.connect(self.open_svg)
        self.file_label = QLabel("No file")
        self.file_label.setObjectName("muted")
        self.file_label.setWordWrap(True)
        row.addWidget(b)
        row.addWidget(self.file_label, 1)
        c.body.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Resolution"))
        self.dpi = QSpinBox()
        self.dpi.setRange(10, 1200)
        self.dpi.setValue(int(q.value("conv/dpi", 96)))
        self.dpi.setSuffix(" px/inch")
        self.dpi.setToolTip("Size of px in a unit-less SVG: 96 (CSS standard, Inkscape ≥ 0.92) or 72/90 (old files)")
        self.dpi.editingFinished.connect(self._reload_svg)
        row.addWidget(self.dpi, 1)
        row.addWidget(QLabel("Scale"))
        self.scale = _dspin(1, 1000, 100.0, 1, " %")
        self.scale.editingFinished.connect(self._reload_svg)
        row.addWidget(self.scale)
        c.body.addLayout(row)
        lv.addWidget(c)

        # --- paths table
        c = Card("Paths")
        hint = QLabel("Click a path in the drawing or in the list · Ctrl / Shift = several")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        c.body.addWidget(hint)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Path", "Mode", "Power", "Speed"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(190)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for i, w in ((1, 84), (2, 64), (3, 72)):
            h.setSectionResizeMode(i, QHeaderView.Fixed)
            self.table.setColumnWidth(i, w)
        self.table.itemSelectionChanged.connect(self._table_selection)
        c.body.addWidget(self.table)
        row = QHBoxLayout()
        for text, slot, tip in (("All", self._sel_all, "Select every path"), ("None", self._sel_none, ""),
                                ("Invert", self._sel_invert, ""), ("Same colour", self._sel_same_colour,
                                                                    "Select the paths with the same stroke/fill colour as the selected ones")):
            bt = QPushButton(text)
            bt.setToolTip(tip)
            bt.clicked.connect(slot)
            row.addWidget(bt)
        c.body.addLayout(row)
        row = QHBoxLayout()
        self.chk_enabled = Switch("Selected paths enabled")
        self.chk_enabled.setToolTip("Switch the selected paths on or off. Switching off keeps their settings: switching on "
                                    "restores the previous mode and values.")
        self.chk_enabled.toggled.connect(self._enable_toggled)
        row.addWidget(self.chk_enabled)
        row.addStretch()
        c.body.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Set selection to"))
        self.quick: dict[str, QToolButton] = {}
        for key, _ in MODES[1:]:
            bt = QToolButton()
            bt.setText(MODE_SHORT[key])
            bt.setStyleSheet(f"padding:5px 8px; border-bottom: 2px solid {MODE_COLORS[key]};")
            bt.clicked.connect(lambda _=False, k=key: self._set_field("mode", k))
            row.addWidget(bt)
            self.quick[key] = bt
        c.body.addLayout(row)
        lv.addWidget(c)

        # --- laser settings of the selection
        c = Card("Laser settings of the selection")
        self.sel_label = QLabel("Select one or more paths")
        self.sel_label.setObjectName("muted")
        self.sel_label.setWordWrap(True)
        c.body.addWidget(self.sel_label)
        self.w: dict[str, QWidget] = {}
        self.w["mode"] = _combo(MODES[1:])      # "off" is the switch
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        g.addWidget(QLabel("What to do"), 0, 0)
        g.addWidget(self.w["mode"], 0, 1)
        c.body.addLayout(g)

        def group(title, rows):
            fr = QFrame()
            fr.setStyleSheet(f"QFrame#grp {{ border: none; border-top: 1px solid {theme.BORDER}; }}")
            fr.setObjectName("grp")
            gl = QGridLayout(fr)
            gl.setContentsMargins(0, 8, 0, 0)
            gl.setHorizontalSpacing(8)
            gl.setVerticalSpacing(6)
            t = QLabel(title.upper())
            t.setObjectName("cardTitle")
            gl.addWidget(t, 0, 0, 1, 2)
            for i, (lab, wid) in enumerate(rows, start=1):
                if lab:
                    gl.addWidget(QLabel(lab), i, 0)
                    gl.addWidget(wid, i, 1)
                else:
                    gl.addWidget(wid, i, 0, 1, 2)
            return fr

        W = self.w
        W["line_power"] = _dspin(0, 100, 30, 1, " %", 1)
        W["line_speed"] = _dspin(1, 30000, 1000, 0, " mm/min", 50)
        W["line_passes"] = QSpinBox()
        W["line_passes"].setRange(1, 100)
        W["offset"] = _dspin(-10, 10, 0, 3, " mm", 0.05)
        W["line_power"].setToolTip("Laser power as % of S max ($30)")
        W["offset"].setToolTip("Kerf compensation on closed paths: + moves the cut outward, − inward (a cut leaves a "
                               "gap of about the beam width; use half of it to keep the part at its nominal size)")
        W["fill_power"] = _dspin(0, 100, 25, 1, " %", 1)
        W["fill_speed"] = _dspin(1, 30000, 3000, 0, " mm/min", 100)
        W["fill_passes"] = QSpinBox()
        W["fill_passes"].setRange(1, 100)
        W["interval"] = _dspin(0.01, 5, 0.1, 3, " mm", 0.01)
        W["angle"] = _dspin(-180, 180, 0, 1, " °", 5)
        W["bidir"] = QCheckBox("Bidirectional (zig-zag)")
        W["crosshatch"] = QCheckBox("Cross-hatch (second pass at +90°)")
        W["overscan"] = _dspin(0, 20, 0, 1, " mm", 0.5)
        W["interval"].setToolTip("Distance between two scan lines: about the spot size for a solid fill, larger for a light shade")
        W["overscan"].setToolTip("Laser-off run-in/run-out beyond each scan line so the head is at full speed when it fires. "
                                 "Not needed with M4 dynamic power")
        self.grp_line = group("Line", [("Power", W["line_power"]), ("Speed", W["line_speed"]),
                                       ("Passes", W["line_passes"]), ("Offset", W["offset"])])
        self.grp_fill = group("Fill", [("Power", W["fill_power"]), ("Speed", W["fill_speed"]),
                                       ("Passes", W["fill_passes"]), ("Line interval", W["interval"]),
                                       ("Angle", W["angle"]), (None, W["bidir"]), (None, W["crosshatch"]),
                                       ("Overscan", W["overscan"])])
        c.body.addWidget(self.grp_line)
        c.body.addWidget(self.grp_fill)
        for fname, wid in W.items():
            if isinstance(wid, QComboBox):
                wid.currentIndexChanged.connect(lambda _=0, f=fname, w_=wid: self._field_edited(f, w_.currentData()))
            elif isinstance(wid, QCheckBox):
                wid.toggled.connect(lambda v, f=fname: self._field_edited(f, bool(v)))
            else:
                wid.valueChanged.connect(lambda v, f=fname: self._field_edited(f, v))
        row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.setToolTip("Saved power/speed/fill settings (for example per material)")
        b_apply = QPushButton("Apply")
        b_apply.clicked.connect(self._preset_apply)
        b_save = QPushButton("Save…")
        b_save.setToolTip("Save the settings of the first selected path as a preset")
        b_save.clicked.connect(self._preset_save)
        b_del = QPushButton("✕")
        b_del.setFixedWidth(34)
        b_del.clicked.connect(self._preset_delete)
        row.addWidget(QLabel("Presets"))
        row.addWidget(self.preset_combo, 1)
        row.addWidget(b_apply)
        row.addWidget(b_save)
        row.addWidget(b_del)
        c.body.addLayout(row)
        self._refresh_presets()
        lv.addWidget(c)

        # --- job
        c = Card("Job")
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(6)
        self.j_smax = _dspin(1, 100000, float(q.value("conv/smax", 1000)), 0, "  S", 100)
        self.j_smax.setToolTip("Value of S for 100 % power: the GRBL setting $30 (1000 on most diode lasers). "
                               "Filled in automatically when the laser is connected.")
        self.j_cmd = _combo(LASER_CMDS)
        self.j_origin = _combo(ORIGINS)
        self.j_order = _combo(ORDERS)
        for combo, key, default in ((self.j_cmd, "conv/cmd", "M4"), (self.j_origin, "conv/origin", "bottom-left"),
                                    (self.j_order, "conv/order", "engrave-first")):
            combo.setCurrentIndex(max(0, combo.findData(q.value(key, default))))
        self.j_origin.setToolTip("Where the G-code zero (X0 Y0) sits relative to the design. In the machine, put the laser head "
                                 "on that point and press «Zero XY». «SVG page» keeps the position of the design on the page.")
        self.j_cmd.setToolTip("M4: power follows the speed during acceleration (needs $32=1): best for engraving and cutting. "
                              "M3: constant power.")
        for i, (lab, wid) in enumerate((("Power scale", self.j_smax), ("Laser", self.j_cmd), ("Origin", self.j_origin),
                                        ("Order", self.j_order))):
            g.addWidget(QLabel(lab), i, 0)
            g.addWidget(wid, i, 1)
        c.body.addLayout(g)
        self.j_opt = QCheckBox("Optimise the path order (shorter travel)")
        self.j_inner = QCheckBox("Cut inner contours before the outer ones")
        self.j_home = QCheckBox("Return to X0 Y0 at the end")
        self.j_air = QCheckBox("Air assist (M8 / M9)")
        self.j_safe = QCheckBox("Force S0 on travel moves (only if $32=0)")
        self.j_safe.setToolTip("For firmware without laser mode: GRBL keeps the laser on during G0 moves, so the power is set "
                               "to 0 before every travel. Slower; leave off when $32=1.")
        for chk, key, default in ((self.j_opt, "conv/opt", "true"), (self.j_inner, "conv/inner", "true"),
                                  (self.j_home, "conv/home", "true"), (self.j_air, "conv/air", "false"),
                                  (self.j_safe, "conv/safe", "false")):
            chk.setChecked(q.value(key, default) == "true")
            c.body.addWidget(chk)
        lv.addWidget(c)
        for w_ in (self.j_smax,):
            w_.valueChanged.connect(self._job_edited)
        for w_ in (self.j_cmd, self.j_origin, self.j_order):
            w_.currentIndexChanged.connect(self._job_edited)
        for w_ in (self.j_opt, self.j_inner, self.j_home, self.j_air, self.j_safe):
            w_.toggled.connect(self._job_edited)

        # --- actions
        c = Card("G-code")
        self.btn_gen = QPushButton("Generate G-code")
        self.btn_gen.clicked.connect(self.generate_now)
        row = QHBoxLayout()
        self.btn_save = QPushButton("Save .gcode…")
        self.btn_save.clicked.connect(self.save_gcode)
        self.btn_send = QPushButton("Send to Laser  ➜")
        self.btn_send.setObjectName("primary")
        self.btn_send.setToolTip("Load the G-code into the Sender without saving a file; then frame and run from the Sender")
        self.btn_send.clicked.connect(self._send)
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_send, 1)
        c.body.addWidget(self.btn_gen)
        c.body.addLayout(row)
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        self.summary.setWordWrap(True)
        c.body.addWidget(self.summary)
        lv.addWidget(c)
        lv.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(430)
        scroll.setMaximumWidth(520)
        split.addWidget(scroll)

        # right: design / preview + tabs
        right = QSplitter(Qt.Vertical)
        right.setChildrenCollapsible(False)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 10, 10, 0)
        tv.setSpacing(6)
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.btn_view = []
        for i, text in enumerate(("Design", "Toolpath preview")):
            b = QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setStyleSheet("padding: 5px 14px; border-radius: 0;" + (
                "border-top-left-radius:7px; border-bottom-left-radius:7px;" if i == 0 else
                "border-top-right-radius:7px; border-bottom-right-radius:7px; border-left: none;"))
            b.clicked.connect(lambda _=False, n=i: self._set_view(n))
            seg.addWidget(b)
            self.btn_view.append(b)
        seg.addStretch()
        self.chk_bed = QCheckBox("Show machine area")
        self.chk_bed.setToolTip("Draws the machine travel ($130/$131) from the lower-left of the page / from the zero")
        self.chk_bed.toggled.connect(self._toggle_bed)
        seg.addWidget(self.chk_bed)
        tv.addLayout(seg)
        self.stack = QStackedWidget()
        self.design = DesignView()
        self.design.setStyleSheet("border: 1px solid %s; border-radius: 10px;" % theme.BORDER)
        self.design.selectionChanged.connect(self._design_selection)
        self.preview = LaserView()
        self.preview.setStyleSheet("border: 1px solid %s; border-radius: 10px;" % theme.BORDER)
        self.preview.empty_message = "Enable at least one path (Line or Fill) to see the toolpath"
        self.stack.addWidget(self.design)
        self.stack.addWidget(self.preview)
        tv.addWidget(self.stack, 1)
        add_view_buttons(self.design)
        add_view_buttons(self.preview, [("Travel", "Show the moves with the laser off", self._toggle_travel, True, False)])
        right.addWidget(top)
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 10, 10)
        tabs = QTabWidget()
        self.editor = CodeEditor()
        self.editor.setReadOnly(True)
        self.messages = QPlainTextEdit()
        self.messages.setReadOnly(True)
        tabs.addTab(self.editor, "Generated G-code")
        tabs.addTab(self.messages, "Messages")
        self.tabs = tabs
        bv.addWidget(tabs)
        right.addWidget(bottom)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([470, 1000])
        self._set_view(0)

    def _set_view(self, n: int) -> None:
        self.stack.setCurrentIndex(n)
        for i, b in enumerate(self.btn_view):
            b.setChecked(i == n)

    def _toggle_travel(self, on: bool) -> None:
        self.preview.show_rapids = on
        self.preview._invalidate()
        self.preview.update()

    def _toggle_bed(self, on: bool) -> None:
        self.design.show_bed = on and self.bed is not None
        self.preview.show_envelope = on and self.bed is not None
        self.design.update()
        self.preview.update()

    def set_machine(self, width: float | None, height: float | None, s_max: float | None) -> None:
        """Called by the main window when the laser reports $130/$131/$30."""
        if width and height:
            self.bed = (float(width), float(height))
            self.design.bed = self.bed
            self.preview.set_envelope((0.0, 0.0), self.bed)
            self.chk_bed.setEnabled(True)
        if s_max and abs(self.j_smax.value() - s_max) > 1e-6:
            self.j_smax.setValue(float(s_max))     # triggers _job_edited → regenerate

    # ------------------------------------------------------------------ SVG
    def open_svg(self) -> None:
        d = self.qs.value("conv/last_dir", os.path.expanduser("~"))
        path, _ = QFileDialog.getOpenFileName(self, "Open SVG", d, "SVG (*.svg);;All files (*)")
        if path:
            self.load_svg(path)

    def load_svg(self, path: str) -> bool:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, "Laser Deck", str(e))
            return False
        self.svg_path = path
        self._svg_text = text
        self.settings = {}              # a new file: new decisions
        self.qs.setValue("conv/last_dir", os.path.dirname(path))
        self._fit_next = True
        self._parse_svg()
        return self.doc is not None and bool(self.doc.shapes)

    def _reload_svg(self) -> None:
        if self._svg_text and self.doc is not None:
            self.qs.setValue("conv/dpi", self.dpi.value())
            self._parse_svg(keep_view=True)

    def _parse_svg(self, keep_view: bool = False) -> None:
        self.doc = parse_svg(self._svg_text, dpi=float(self.dpi.value()), scale=self.scale.value() / 100.0)
        name = os.path.basename(self.svg_path or "")
        self.file_label.setText(f"{name}\n{len(self.doc.shapes)} paths, {self.doc.width_mm:.1f} × {self.doc.height_mm:.1f} mm")
        valid = {s.sid for s in self.doc.shapes}
        self.settings = {k: v for k, v in self.settings.items() if k in valid}
        for s in self.doc.shapes:
            self.settings.setdefault(s.sid, cam.default_settings(s))
        self.design.set_doc(self.doc, fit=not keep_view)
        self._fill_table()
        self._sync_design()
        self._load_widgets()
        self._update_buttons()
        self.generate_now()

    # ------------------------------------------------------------------ table <-> selection
    def _fill_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._row = {}
        if self.doc:
            for i, s in enumerate(self.doc.shapes):
                self.table.insertRow(i)
                self._row[s.sid] = i
                it = QTableWidgetItem(_swatch(s), f"{s.label}  ({s.kind})")
                it.setData(Qt.UserRole, s.sid)
                self.table.setItem(i, 0, it)
                for c in (1, 2, 3):
                    self.table.setItem(i, c, QTableWidgetItem(""))
                self._refresh_row(s.sid)
        self.table.blockSignals(False)

    def _refresh_row(self, sid: str) -> None:
        i = self._row.get(sid)
        st = self.settings.get(sid)
        if i is None or st is None:
            return
        mode = st.mode
        if mode == "line":
            power, speed = f"{st.line_power:g} %", f"{st.line_speed:g}"
        elif mode == "fill":
            power, speed = f"{st.fill_power:g} %", f"{st.fill_speed:g}"
        elif mode == "both":
            power, speed = f"{st.fill_power:g}/{st.line_power:g} %", f"{st.fill_speed:g}/{st.line_speed:g}"
        else:
            power = speed = "—"
        for c, text in ((1, MODE_SHORT[mode]), (2, power), (3, speed)):
            it = self.table.item(i, c)
            it.setText(text)
            it.setForeground(QColor(MODE_COLORS[mode] if c == 1 else (theme.MUTED if mode == "off" else theme.TEXT)))
            it.setToolTip(text)

    def selected_sids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self.table.item(r, 0).data(Qt.UserRole) for r in rows]

    def _sync_design(self) -> None:
        self.design.set_modes({sid: st.mode for sid, st in self.settings.items()})
        # the origin follows the whole drawing: switching paths on or off never moves it
        self.design.origin = cam.origin_point(self.doc, self.job()) if self.doc is not None and self.doc.shapes else None
        self.design.update()

    def _table_selection(self) -> None:
        self.design.set_selected(set(self.selected_sids()))
        self._load_widgets()

    def _design_selection(self, sids: set, _mods) -> None:
        self.table.blockSignals(True)
        self.table.clearSelection()
        rows = sorted(self._row[s] for s in sids if s in self._row)
        for r in rows:
            self.table.setRangeSelected(QTableWidgetSelectionRange(r, 0, r, self.table.columnCount() - 1), True)
        if rows:
            self.table.scrollToItem(self.table.item(rows[0], 0))
        self.table.blockSignals(False)
        self._load_widgets()

    def _select(self, sids: set[str]) -> None:
        self._design_selection(set(sids), None)
        self.design.set_selected(set(sids))

    def _sel_all(self) -> None:
        self._select({s.sid for s in self.doc.shapes} if self.doc else set())

    def _sel_none(self) -> None:
        self._select(set())

    def _sel_invert(self) -> None:
        if self.doc:
            cur = set(self.selected_sids())
            self._select({s.sid for s in self.doc.shapes} - cur)

    def _sel_same_colour(self) -> None:
        if not self.doc:
            return
        cur = set(self.selected_sids())
        keys = {(s.fill, s.stroke) for s in self.doc.shapes if s.sid in cur}
        self._select({s.sid for s in self.doc.shapes if (s.fill, s.stroke) in keys})

    # ------------------------------------------------------------------ settings model <-> widgets
    def _load_widgets(self) -> None:
        sids = [s for s in self.selected_sids() if s in self.settings]
        self._loading = True
        en = bool(sids)
        any_on = any(self.settings[s].mode != "off" for s in sids)
        for wid in self.w.values():
            wid.setEnabled(en and any_on)
        for b in self.quick.values():
            b.setEnabled(en)
        self.chk_enabled.setEnabled(en)
        self.chk_enabled.setChecked(en and all(self.settings[s].mode != "off" for s in sids))
        if en:
            st = self.settings[sids[0]]
            for fname, wid in self.w.items():
                v = getattr(st, fname)
                if fname == "mode" and v == "off":
                    v = st.last_mode
                if isinstance(wid, QComboBox):
                    wid.setCurrentIndex(max(0, wid.findData(v)))
                elif isinstance(wid, QCheckBox):
                    wid.setChecked(bool(v))
                else:
                    wid.setValue(v)
            modes = {self.settings[s].mode for s in sids}
            self.grp_line.setVisible(any(m in ("line", "both") for m in modes))
            self.grp_fill.setVisible(any(m in ("fill", "both") for m in modes))
            names = ", ".join(sids[:3]) + (f" … (+{len(sids) - 3})" if len(sids) > 3 else "")
            self.sel_label.setText(f"{len(sids)} path{'s' if len(sids) != 1 else ''} selected: {names}"
                                   + ("\nMixed values: the panel shows the first path; an edited field is applied to all."
                                      if len(sids) > 1 else ""))
        else:
            self.grp_line.setVisible(True)
            self.grp_fill.setVisible(True)
            self.sel_label.setText("Select one or more paths (list or click in the drawing) to set what the laser does")
        self._loading = False

    def _set_field(self, fname: str, value) -> None:
        sids = [s for s in self.selected_sids() if s in self.settings]
        if not sids:
            return
        for sid in sids:
            st = self.settings[sid]
            if fname == "mode" and value == "off" and st.mode != "off":
                st.last_mode = st.mode             # remembered for the «On» button; the other settings stay untouched
            setattr(st, fname, value)
            self._refresh_row(sid)
        if fname == "mode":
            self._sync_design()
            self._load_widgets()
        self._schedule()

    def _enable_toggled(self, on: bool) -> None:
        if self._loading:
            return
        sids = [s for s in self.selected_sids() if s in self.settings]
        for sid in sids:
            st = self.settings[sid]
            if on and st.mode == "off":
                st.mode = st.last_mode if st.last_mode != "off" else "line"
            elif not on and st.mode != "off":
                st.last_mode = st.mode           # settings stay untouched, switching on restores them
                st.mode = "off"
            self._refresh_row(sid)
        if sids:
            self._sync_design()
            self._load_widgets()
            self._schedule()

    def _field_edited(self, fname: str, value) -> None:
        if self._loading:
            return
        self._set_field(fname, value)

    # presets
    def _presets(self) -> dict:
        try:
            return json.loads(self.qs.value("conv/presets", "{}"))
        except (ValueError, TypeError):
            return {}

    def _refresh_presets(self) -> None:
        self.preset_combo.clear()
        for name in sorted(self._presets()):
            self.preset_combo.addItem(name)

    def _preset_save(self) -> None:
        sids = self.selected_sids()
        if not sids:
            QMessageBox.information(self, "Laser Deck", "Select a path whose settings you want to save.")
            return
        name, ok = QInputDialog.getText(self, "Preset", "Preset name (e.g. «3 mm plywood cut»):")
        if not ok or not name.strip():
            return
        st = self.settings[sids[0]]
        data = {f.name: getattr(st, f.name) for f in fields(st) if f.name not in ("mode", "last_mode")}
        p = self._presets()
        p[name.strip()] = data
        self.qs.setValue("conv/presets", json.dumps(p))
        self._refresh_presets()
        self.preset_combo.setCurrentText(name.strip())

    def _preset_apply(self) -> None:
        data = self._presets().get(self.preset_combo.currentText())
        sids = [s for s in self.selected_sids() if s in self.settings]
        if not data or not sids:
            return
        for sid in sids:
            for k, v in data.items():
                if hasattr(self.settings[sid], k):
                    setattr(self.settings[sid], k, v)
            self._refresh_row(sid)
        self._load_widgets()
        self._schedule()

    def _preset_delete(self) -> None:
        name = self.preset_combo.currentText()
        p = self._presets()
        if name in p:
            del p[name]
            self.qs.setValue("conv/presets", json.dumps(p))
            self._refresh_presets()

    # ------------------------------------------------------------------ job
    def job(self) -> cam.Job:
        return cam.Job(s_max=self.j_smax.value(), laser_cmd=self.j_cmd.currentData(), air_assist=self.j_air.isChecked(),
                       origin=self.j_origin.currentData(), order=self.j_order.currentData(),
                       optimize=self.j_opt.isChecked(), inner_first=self.j_inner.isChecked(),
                       return_home=self.j_home.isChecked(), safe_travel=self.j_safe.isChecked())

    def _job_edited(self, *_) -> None:
        q = self.qs
        q.setValue("conv/smax", self.j_smax.value())
        q.setValue("conv/cmd", self.j_cmd.currentData())
        q.setValue("conv/origin", self.j_origin.currentData())
        q.setValue("conv/order", self.j_order.currentData())
        for key, chk in (("opt", self.j_opt), ("inner", self.j_inner), ("home", self.j_home), ("air", self.j_air),
                         ("safe", self.j_safe)):
            q.setValue(f"conv/{key}", "true" if chk.isChecked() else "false")
        self.preview.s_max = self.j_smax.value()
        self._sync_design()
        self._schedule()

    # ------------------------------------------------------------------ generation
    def _schedule(self) -> None:
        if self.doc is not None:
            self._dirty = True            # Save/Send stay disabled until the new G-code is ready
            self._update_buttons()
            self._timer.start()

    def generate_now(self) -> None:
        self._timer.stop()
        if self.doc is None or not self.doc.shapes:
            self._show_result(cam.CamResult(warnings=list(self.doc.warnings) if self.doc else []), None)
            return
        self._serial += 1                 # also invalidates any computation still running
        self._dirty = True
        self._update_buttons()
        self.summary.setText("Computing the toolpath…")
        t = GenThread(self.doc, copy.deepcopy(self.settings), self.job(), os.path.basename(self.svg_path or ""),
                      self._serial, self)
        t.done.connect(self._on_generated)
        t.finished.connect(t.deleteLater)
        self._thread = t
        t.start()

    def _on_generated(self, res, prog, serial_no: int) -> None:
        if serial_no != self._serial:
            return
        self._show_result(res, prog)

    def _show_result(self, res: cam.CamResult, prog: Program | None) -> None:
        self.result, self.program = res, prog
        self._dirty = False
        self.editor.setPlainText(res.gcode)
        self.preview.s_max = self.j_smax.value()
        self.preview.set_program(prog, fit=self._fit_next)
        if prog is not None and len(prog.path):
            self._fit_next = False
        msgs = list(self.doc.warnings if self.doc else []) + res.warnings + (prog.warnings if prog else [])
        if prog is not None:
            sz = prog.size
            info = (f"{res.n_paths} operations, {len(prog.commands)} lines, size {sz[0]:.1f} × {sz[1]:.1f} mm, "
                    f"burning {prog.burn_length:.0f} mm, estimated time ≈ {format_duration(prog.total_time)}")
            if self.bed and self.j_origin.currentData() != "page":
                if sz[0] > self.bed[0] + 1e-6 or sz[1] > self.bed[1] + 1e-6:
                    msgs.append(f"The job ({sz[0]:.0f} × {sz[1]:.0f} mm) is larger than the machine area "
                                f"({self.bed[0]:g} × {self.bed[1]:g} mm)")
            elif self.bed:
                lo, hi = prog.bounds_min, prog.bounds_max
                if lo[0] < -1e-6 or lo[1] < -1e-6 or hi[0] > self.bed[0] + 1e-6 or hi[1] > self.bed[1] + 1e-6:
                    msgs.append("Part of the design falls outside the machine area (page origin)")
            self.summary.setText(info + (f"\n⚠ {len(msgs)} warnings (Messages tab)" if msgs else ""))
            msgs.insert(0, info)
        elif self.doc is not None:
            self.summary.setText("No G-code: set Line or Fill on at least one path" + (" — see Messages" if msgs else ""))
        self.messages.setPlainText("\n".join(msgs))
        self._update_buttons()

    def _update_buttons(self) -> None:
        ok = bool(self.result and self.result.gcode) and not self._dirty
        self.btn_save.setEnabled(ok)
        self.btn_send.setEnabled(ok)
        self.btn_gen.setEnabled(self.doc is not None)

    # ------------------------------------------------------------------ output
    def suggested_name(self) -> str:
        return os.path.splitext(os.path.basename(self.svg_path or "design"))[0] + ".gcode"

    def save_gcode(self) -> None:
        if not (self.result and self.result.gcode) or self._dirty:
            return
        d = self.qs.value("conv/last_dir", os.path.expanduser("~"))
        path, _ = QFileDialog.getSaveFileName(self, "Save G-code", os.path.join(d, self.suggested_name()),
                                              "G-code (*.gcode *.nc)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.result.gcode)

    def _send(self) -> None:
        if self.result and self.result.gcode and not self._dirty:
            self.sendRequested.emit(self.result.gcode, self.suggested_name())
