"""2D views (QPainter + numpy): a base canvas with pan/zoom/grid, the G-code viewer (`LaserView`, colours = laser
power) and the SVG design viewer (`DesignView`, one colour per path mode, click to select)."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPolygonF, QRadialGradient, QTransform)
from PySide6.QtWidgets import QHBoxLayout, QToolButton, QWidget

from . import theme
from .gcode import BURN, Program

N_BUCKETS = 24
MODE_COLORS = {"off": "#4d5866", "line": "#4aa8ff", "fill": "#ff7a45", "both": "#ff7a45"}


def _lerp(c1: QColor, c2: QColor, t: float) -> QColor:
    return QColor(int(c1.red() + (c2.red() - c1.red()) * t), int(c1.green() + (c2.green() - c1.green()) * t),
                  int(c1.blue() + (c2.blue() - c1.blue()) * t))


def power_color(t: float) -> QColor:
    """t = 0 → low power, cool cyan;  t = 1 → full power, warm orange (same ramp as the CNC Deck depth colours)."""
    stops = [QColor("#38bdf8"), QColor("#4ade80"), QColor("#ffc53d"), QColor("#ff7a45")]
    t = min(max(t, 0.0), 1.0) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    return _lerp(stops[i], stops[i + 1], t - i)


class CanvasView(QWidget):
    """Pan (right/middle/left-drag) and zoom (wheel) over a world measured in mm; `y_up` selects the Y direction."""
    clicked = Signal(float, float, object)       # world x, y, keyboard modifiers

    y_up = True

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 300)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.scale = 4.0
        self.center = np.zeros(2)
        self.empty_message = ""
        self._drag = None
        self._moved = False

    # ---------- transforms
    def project(self, pts: np.ndarray) -> np.ndarray:
        pts = np.asarray(pts, float).reshape(-1, 2)
        out = np.empty((len(pts), 2))
        out[:, 0] = self.width() / 2 + (pts[:, 0] - self.center[0]) * self.scale
        sgn = -1.0 if self.y_up else 1.0
        out[:, 1] = self.height() / 2 + sgn * (pts[:, 1] - self.center[1]) * self.scale
        return out

    def unproject(self, x: float, y: float) -> np.ndarray:
        sgn = -1.0 if self.y_up else 1.0
        return np.array([(x - self.width() / 2) / self.scale + self.center[0],
                         sgn * (y - self.height() / 2) / self.scale + self.center[1]])

    def world_transform(self) -> QTransform:
        sgn = -1.0 if self.y_up else 1.0
        t = QTransform()
        t.translate(self.width() / 2, self.height() / 2)
        t.scale(self.scale, sgn * self.scale)
        t.translate(-self.center[0], -self.center[1])
        return t

    def scene_bounds(self):
        """(lo, hi) of what must be visible; subclasses override."""
        return np.array([-5.0, -5.0]), np.array([100.0, 100.0])

    def fit(self) -> None:
        lo, hi = self.scene_bounds()
        self.center = (lo + hi) / 2
        w, h = max(hi[0] - lo[0], 1.0), max(hi[1] - lo[1], 1.0)
        self.scale = 0.88 * min(max(self.width(), 50) / w, max(self.height(), 50) / h)
        self.on_view_changed()
        self.update()

    def on_view_changed(self) -> None:
        pass

    # ---------- input
    def mousePressEvent(self, e):
        self.setFocus()
        self._drag = (e.button(), e.position())
        self._moved = False

    def mouseReleaseEvent(self, e):
        if self._drag and self._drag[0] == Qt.LeftButton and not self._moved:
            w = self.unproject(e.position().x(), e.position().y())
            self.clicked.emit(float(w[0]), float(w[1]), e.modifiers())
        self._drag = None

    def mouseMoveEvent(self, e):
        if not self._drag:
            self.on_hover(e.position())
            return
        btn, last = self._drag
        d = e.position() - last
        if btn == Qt.LeftButton and not self._moved and abs(d.x()) + abs(d.y()) < 4:
            return
        self._moved = True
        self._drag = (btn, e.position())
        sgn = -1.0 if self.y_up else 1.0
        self.center -= np.array([d.x(), sgn * d.y()]) / self.scale
        self.on_view_changed()
        self.update()

    def on_hover(self, pos) -> None:
        pass

    def wheelEvent(self, e):
        f = 1.0015 ** e.angleDelta().y()
        before = self.unproject(e.position().x(), e.position().y())
        self.scale = min(max(self.scale * f, 0.05), 20000)
        after = self.unproject(e.position().x(), e.position().y())
        self.center += before - after
        self.on_view_changed()
        self.update()

    def mouseDoubleClickEvent(self, e):
        self.fit()

    def resizeEvent(self, e):
        self.on_view_changed()

    # ---------- painting helpers
    def paint_background(self, p: QPainter) -> None:
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor("#101720"))
        g.setColorAt(1, QColor("#0a0e13"))
        p.fillRect(self.rect(), g)

    def paint_grid(self, p: QPainter) -> None:
        tl, br = self.unproject(0, 0), self.unproject(self.width(), self.height())
        x_lo, x_hi = min(tl[0], br[0]), max(tl[0], br[0])
        y_lo, y_hi = min(tl[1], br[1]), max(tl[1], br[1])
        step = 1.0
        while step * self.scale < 12:
            step *= 5 if int(round(math.log10(step) * 3)) % 3 == 0 else 2
        major = step * 5
        xs = np.arange(math.floor(x_lo / step) * step, x_hi + step, step)
        ys = np.arange(math.floor(y_lo / step) * step, y_hi + step, step)
        for vals, horiz in ((xs, False), (ys, True)):
            for v in vals:
                is_major = abs(v / major - round(v / major)) < 1e-6
                a = np.array([[x_lo - step, v], [x_hi + step, v]]) if horiz else np.array([[v, y_lo - step], [v, y_hi + step]])
                s = self.project(a)
                p.setPen(QPen(QColor(255, 255, 255, 34 if is_major else 14), 1))
                p.drawLine(QPointF(*s[0]), QPointF(*s[1]))
        p.setFont(QFont("monospace", 7))
        p.setPen(QColor(255, 255, 255, 80))
        for v in xs:
            if abs(v / major - round(v / major)) < 1e-6 and abs(v) > 1e-9:
                s = self.project(np.array([[v, 0.0]]))[0]
                p.drawText(QPointF(s[0] + 2, min(max(s[1] + 11, 10), self.height() - 3)), f"{v:g}")
        for v in ys:
            if abs(v / major - round(v / major)) < 1e-6 and abs(v) > 1e-9:
                s = self.project(np.array([[0.0, v]]))[0]
                p.drawText(QPointF(min(max(s[0] + 3, 2), self.width() - 30), s[1] - 2), f"{v:g}")

    def paint_axes(self, p: QPainter, origin=(0.0, 0.0)) -> None:
        ln = 12.0 / self.scale
        o = np.array(origin, float)
        p.setFont(QFont("sans", 9, QFont.Bold))
        for i, name in enumerate("XY"):
            e = o.copy()
            e[i] += ln
            s = self.project(np.array([o, e]))
            p.setPen(QPen(QColor(theme.AXIS[name]), 2.2, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(*s[0]), QPointF(*s[1]))
            p.drawText(QPointF(s[1][0] + 4, s[1][1] - 4), name)
        s0 = self.project(o.reshape(1, 2))[0]
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(*s0), 3.2, 3.2)

    def paint_message(self, p: QPainter) -> None:
        p.setFont(QFont("sans", 13))
        p.setPen(QColor(255, 255, 255, 90))
        p.drawText(self.rect(), Qt.AlignCenter, self.empty_message)


# ===================================================================== G-code viewer
class LaserView(CanvasView):
    """Top view of a G-code program: burn moves coloured by power, travel dashed, executed part in grey, live head."""
    y_up = True

    def __init__(self, parent=None):
        super().__init__(parent)
        self.program: Program | None = None
        self.s_max = 1000.0
        self.head = np.zeros(2)
        self.done_upto = 0
        self.show_rapids = False                        # dashed travel moves: off by default (noisy across holes)
        self.show_envelope = False
        self.envelope: tuple[np.ndarray, np.ndarray] | None = None     # (lo, hi) in work coordinates
        self.frame: tuple[np.ndarray, np.ndarray] | None = None
        self.empty_message = "Open a G-code file to see the toolpath\n(drag a .nc / .gcode file here)"
        self._cache_key = None
        self._cache: QImage | None = None
        self._done_cache: QImage | None = None
        self._done_drawn = 0
        self._interactive = False
        self._settle = QTimer(self, singleShot=True, interval=140)
        self._settle.timeout.connect(self._end_interaction)
        self._bucket = np.zeros(0, np.int32)

    # ---------- data
    def set_program(self, prog: Program | None, fit: bool = True) -> None:
        self.program = prog
        self.done_upto = 0
        self._invalidate()
        if prog is not None and len(prog.path):
            p = prog.path
            top = max(self.s_max, float(p.power.max()), 1.0)
            self._bucket = np.clip((p.power / top * (N_BUCKETS - 1)).round().astype(np.int32), 0, N_BUCKETS - 1)
            self._top = top
            if fit:
                self.fit()
                return
        self.update()

    def set_progress_line(self, source_line: int) -> None:
        if self.program is None or not len(self.program.path):
            return
        k = int(np.searchsorted(self.program.path.line, source_line, side="right")) if source_line >= 0 else 0
        if k != self.done_upto:
            self.done_upto = k
            self.update()

    def set_tool(self, pos) -> None:
        self.head = np.asarray(pos, float)[:2]
        self.update()

    def set_envelope(self, lo, hi) -> None:
        self.envelope = None if lo is None else (np.asarray(lo, float)[:2], np.asarray(hi, float)[:2])
        self.update()

    def set_frame(self, lo, hi) -> None:
        self.frame = None if lo is None else (np.asarray(lo, float)[:2], np.asarray(hi, float)[:2])
        self.update()

    def _invalidate(self) -> None:
        self._cache_key = None
        self._done_cache = None
        self._done_drawn = 0

    def on_view_changed(self) -> None:
        self._interactive = True
        self._settle.start()
        self._invalidate()

    def _end_interaction(self) -> None:
        self._interactive = False
        self._invalidate()
        self.update()

    def scene_bounds(self):
        lo, hi = np.array([-5.0, -5.0]), np.array([100.0, 100.0])
        if self.program is not None and len(self.program.path):
            lo = np.array(self.program.bounds_min[:2])
            hi = np.array(self.program.bounds_max[:2])
            lo, hi = np.minimum(lo, 0.0), np.maximum(hi, 0.0)
            pad = max(hi[0] - lo[0], hi[1] - lo[1], 10.0) * 0.04
            lo, hi = lo - pad, hi + pad
        if self.envelope is not None and self.show_envelope:
            lo, hi = np.minimum(lo, self.envelope[0]), np.maximum(hi, self.envelope[1])
        return lo, hi

    # ---------- painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.paint_background(p)
        self.paint_grid(p)
        self._draw_envelope(p)
        self._draw_bbox(p)
        self._draw_path(p)
        self.paint_axes(p)
        self._draw_head(p)
        self._draw_hud(p)
        p.end()

    def _rect_points(self, lo, hi):
        return np.array([[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]], [lo[0], lo[1]]])

    def _draw_envelope(self, p: QPainter) -> None:
        if not (self.show_envelope and self.envelope):
            return
        s = self.project(self._rect_points(*self.envelope))
        p.setPen(QPen(QColor(theme.WARN), 1.2, Qt.DotLine))
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in s]))

    def _draw_bbox(self, p: QPainter) -> None:
        rect = None
        if self.frame is not None:
            rect = self.frame
        elif self.program is not None and len(self.program.path):
            rect = (np.array(self.program.bounds_min[:2]), np.array(self.program.bounds_max[:2]))
        if rect is None:
            return
        s = self.project(self._rect_points(*rect))
        p.setPen(QPen(QColor(255, 255, 255, 70), 1, Qt.DashLine))
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in s]))
        size = rect[1] - rect[0]
        p.setFont(QFont("monospace", 8))
        p.setPen(QColor(255, 255, 255, 150))
        p.drawText(QPointF((s[0][0] + s[1][0]) / 2 - 18, s[0][1] + 13), f"{size[0]:.1f}")
        p.drawText(QPointF(s[1][0] + 5, (s[1][1] + s[2][1]) / 2), f"{size[1]:.1f}")

    def _paint_segments(self, img: QImage, sl: slice, done: bool, decimate: int) -> None:
        path = self.program.path
        qp = QPainter(img)
        qp.setRenderHint(QPainter.Antialiasing, not self._interactive)
        start = path.start[sl][::decimate][:, :2]
        end = path.end[sl][::decimate][:, :2]
        kind = path.kind[sl][::decimate]
        bucket = self._bucket[sl][::decimate]
        if not len(kind):
            qp.end()
            return
        s0 = self.project(start.astype(float))
        s1 = self.project(end.astype(float))
        if self.show_rapids:
            m = kind != BURN
            if m.any():
                qp.setPen(QPen(QColor(120, 140, 170, 70 if not done else 35), 1, Qt.DashLine))
                qp.drawLines([QLineF(a[0], a[1], b[0], b[1]) for a, b in zip(s0[m], s1[m])])
        m = kind == BURN
        for b in np.unique(bucket[m]):
            mm = m & (bucket == b)
            col = QColor(62, 76, 96) if done else power_color(b / (N_BUCKETS - 1))
            qp.setPen(QPen(col, 2.0 if done else 1.5, Qt.SolidLine, Qt.RoundCap))
            qp.drawLines([QLineF(a[0], a[1], c[0], c[1]) for a, c in zip(s0[mm], s1[mm])])
        qp.end()

    def _layer(self, sl, done: bool, decimate: int) -> QImage:
        img = QImage(self.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        if sl is not None:
            self._paint_segments(img, sl, done, decimate)
        return img

    def _draw_path(self, p: QPainter) -> None:
        if self.program is None or not len(self.program.path):
            return
        n = len(self.program.path)
        decimate = max(1, n // 25000) if self._interactive else 1
        key = (self.width(), self.height(), round(self.scale, 4), tuple(np.round(self.center, 3)), decimate,
               self.show_rapids, id(self.program))
        if key != self._cache_key or self._cache is None:
            self._cache = self._layer(slice(0, n), False, decimate)
            self._cache_key = key
            self._done_cache = None
            self._done_drawn = 0
        p.drawImage(0, 0, self._cache)
        if self.done_upto > 0:
            if self._done_cache is None or self._done_drawn > self.done_upto:
                self._done_cache = self._layer(None, True, 1)
                self._done_drawn = 0
            if self._done_drawn < self.done_upto:
                self._paint_segments(self._done_cache, slice(self._done_drawn, self.done_upto), True, decimate)
                self._done_drawn = self.done_upto
            p.drawImage(0, 0, self._done_cache)

    def _draw_head(self, p: QPainter) -> None:
        s = self.project(self.head.reshape(1, 2))[0]
        grad = QRadialGradient(QPointF(*s), 18)
        grad.setColorAt(0, QColor(255, 122, 69, 150))
        grad.setColorAt(1, QColor(255, 122, 69, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QPointF(*s), 18, 18)
        p.setPen(QPen(QColor("#e5e7eb"), 1.3))
        p.drawLine(QPointF(s[0] - 9, s[1]), QPointF(s[0] + 9, s[1]))
        p.drawLine(QPointF(s[0], s[1] - 9), QPointF(s[0], s[1] + 9))
        p.setBrush(QColor(theme.ACCENT))
        p.setPen(QPen(QColor("#04211d"), 1.5))
        p.drawEllipse(QPointF(*s), 4.2, 4.2)

    def _draw_hud(self, p: QPainter) -> None:
        if self.program is None or not len(self.program.path):
            self.paint_message(p)
            return
        lo, hi = self.program.bounds_min, self.program.bounds_max
        p.setFont(QFont("monospace", 9))
        p.setPen(QColor(255, 255, 255, 120))
        p.drawText(QPointF(12, self.height() - 12), f"X {lo[0]:.1f}…{hi[0]:.1f}   Y {lo[1]:.1f}…{hi[1]:.1f} mm")
        x0, y0, wd, ht = self.width() - 28, 52, 8, 110       # legend: power ramp
        for i in range(ht):
            p.fillRect(QRectF(x0, y0 + i, wd, 1.2), power_color(1 - i / ht))
        p.setFont(QFont("monospace", 8))
        p.setPen(QColor(255, 255, 255, 150))
        p.drawText(QPointF(x0 - 44, y0 + 8), f"S{getattr(self, '_top', self.s_max):g}")
        p.drawText(QPointF(x0 - 30, y0 + ht), "S0")


# ===================================================================== SVG design viewer
class DesignView(CanvasView):
    """The SVG as the laser will see it: one colour per path mode, selected paths highlighted, click to select."""
    y_up = False
    selectionChanged = Signal(object, object)         # set of selected sids, modifiers of the click

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc = None
        self.modes: dict[str, str] = {}
        self.selected: set[str] = set()
        self.hover: str | None = None
        self.origin: tuple[float, float] | None = None
        self.bed: tuple[float, float] | None = None       # (w, h) mm, anchored at the lower-left of the page
        self.show_bed = False
        self.empty_message = "Open an SVG file\n(drag a .svg file here)"
        self._qpaths: dict[str, tuple[QPainterPath, QPainterPath]] = {}   # sid -> (fill path, stroke path)
        self._pts: dict[str, list[np.ndarray]] = {}
        self._bbox: dict[str, tuple[float, float, float, float]] = {}
        self.clicked.connect(self._on_click)

    # ---------- data
    def set_doc(self, doc, fit: bool = True) -> None:
        self.doc = doc
        self.selected = set()
        self.hover = None
        self._qpaths.clear()
        self._pts.clear()
        self._bbox.clear()
        if doc is not None:
            for sh in doc.shapes:
                fill, stroke = QPainterPath(), QPainterPath()
                fill.setFillRule(Qt.OddEvenFill if sh.fill_rule == "evenodd" else Qt.WindingFill)
                self._pts[sh.sid] = [sp.points for sp in sh.subpaths]
                for sp in sh.subpaths:
                    poly = QPolygonF([QPointF(float(x), float(y)) for x, y in sp.points])
                    stroke.addPolygon(poly)
                    if sp.closed:
                        stroke.closeSubpath()
                        fill.addPolygon(poly)
                        fill.closeSubpath()
                b = doc.bounds({sh.sid})
                if b:
                    self._bbox[sh.sid] = (float(b[0][0]), float(b[0][1]), float(b[1][0]), float(b[1][1]))
                self._qpaths[sh.sid] = (fill, stroke)
        if fit:
            self.fit()
        else:
            self.update()

    def set_modes(self, modes: dict[str, str]) -> None:
        self.modes = dict(modes)
        self.update()

    def set_selected(self, sids: set[str]) -> None:
        self.selected = set(sids)
        self.update()

    def scene_bounds(self):
        if self.doc is None:
            return np.array([0.0, 0.0]), np.array([100.0, 100.0])
        lo = np.array([0.0, 0.0])
        hi = np.array([max(self.doc.width_mm, 1.0), max(self.doc.height_mm, 1.0)])
        b = self.doc.bounds()
        if b:
            lo, hi = np.minimum(lo, b[0]), np.maximum(hi, b[1])
        if self.show_bed and self.bed:
            lo = np.minimum(lo, [0.0, self.doc.height_mm - self.bed[1]])
            hi = np.maximum(hi, [self.bed[0], self.doc.height_mm])
        return lo, hi

    # ---------- picking
    def _pick(self, wx: float, wy: float) -> str | None:
        if self.doc is None:
            return None
        tol = 6.0 / self.scale
        pt = np.array([wx, wy])
        best, bd = None, tol
        for sid, arrs in self._pts.items():
            bb = self._bbox.get(sid)
            if bb and (wx < bb[0] - tol or wx > bb[2] + tol or wy < bb[1] - tol or wy > bb[3] + tol):
                continue
            for pts in arrs:
                a, b = pts[:-1], pts[1:]
                closed_seg = np.array([pts[-1]]), np.array([pts[0]])
                a = np.vstack([a, closed_seg[0]])
                b = np.vstack([b, closed_seg[1]])
                ab = b - a
                t = np.clip(((pt - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-12), 0, 1)
                d = float(np.min(np.linalg.norm(a + t[:, None] * ab - pt, axis=1)))
                if d < bd:
                    best, bd = sid, d
        if best:
            return best
        inside, area = None, 1e30
        for sid, (fill, _) in self._qpaths.items():
            if fill.contains(QPointF(wx, wy)):
                bb = self._bbox.get(sid, (0, 0, 1, 1))
                a_ = (bb[2] - bb[0]) * (bb[3] - bb[1])
                if a_ < area:
                    inside, area = sid, a_
        return inside

    def _on_click(self, wx: float, wy: float, mods) -> None:
        sid = self._pick(wx, wy)
        new = set(self.selected)
        if mods & (Qt.ControlModifier | Qt.ShiftModifier):
            if sid:
                new ^= {sid}
        else:
            new = {sid} if sid else set()
        self.selected = new
        self.update()
        self.selectionChanged.emit(new, mods)

    def on_hover(self, pos) -> None:
        w = self.unproject(pos.x(), pos.y())
        h = self._pick(float(w[0]), float(w[1]))
        if h != self.hover:
            self.hover = h
            self.setCursor(Qt.PointingHandCursor if h else Qt.ArrowCursor)
            self.update()

    # ---------- painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        self.paint_background(p)
        if self.doc is None:
            self.paint_message(p)
            p.end()
            return
        self.paint_grid(p)
        self._draw_page(p)
        p.save()
        p.setTransform(self.world_transform())
        for sid, (fill, stroke) in self._qpaths.items():
            mode = self.modes.get(sid, "line")
            if mode in ("fill", "both"):
                c = QColor(MODE_COLORS["fill"])
                c.setAlpha(80 if sid not in self.selected else 130)
                p.fillPath(fill, c)
        for sid, (fill, stroke) in self._qpaths.items():
            mode = self.modes.get(sid, "line")
            col = QColor(MODE_COLORS[mode])
            if mode == "both":
                col = QColor(MODE_COLORS["line"])
            elif mode == "fill":
                col = QColor(MODE_COLORS["fill"])
            width = 1.4
            if sid == self.hover:
                col = col.lighter(140)
                width = 2.0
            pen = QPen(col, width, Qt.DashLine if mode == "off" else Qt.SolidLine)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(stroke)
        for sid in self.selected:
            if sid in self._qpaths:
                pen = QPen(QColor("#ffffff"), 2.6)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.drawPath(self._qpaths[sid][1])
        p.restore()
        if self.origin is not None:
            self._draw_origin(p)
        self._draw_hud(p)
        p.end()

    def _draw_page(self, p: QPainter) -> None:
        w, h = self.doc.width_mm, self.doc.height_mm
        s = self.project(np.array([[0, 0], [w, 0], [w, h], [0, h], [0, 0]]))
        p.setPen(QPen(QColor(255, 255, 255, 50), 1))
        p.setBrush(QColor(255, 255, 255, 8))
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in s]))
        if self.show_bed and self.bed:
            bw, bh = self.bed
            s = self.project(np.array([[0, h - bh], [bw, h - bh], [bw, h], [0, h], [0, h - bh]]))
            p.setPen(QPen(QColor(theme.WARN), 1.3, Qt.DotLine))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in s]))
            p.setFont(QFont("monospace", 8))
            p.setPen(QColor(theme.WARN))
            p.drawText(QPointF(s[3][0] + 4, s[3][1] - 5), f"machine area {bw:g} × {bh:g}")

    def _draw_origin(self, p: QPainter) -> None:
        o = np.array(self.origin, float)
        ln = 14.0 / self.scale
        p.setFont(QFont("sans", 9, QFont.Bold))
        for name, d in (("X", (1, 0)), ("Y", (0, -1))):     # Y of the machine points up on screen
            e = o + np.array(d) * ln
            s = self.project(np.array([o, e]))
            p.setPen(QPen(QColor(theme.AXIS[name]), 2.2, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(*s[0]), QPointF(*s[1]))
            p.drawText(QPointF(s[1][0] + 4, s[1][1] - 4), name)
        s0 = self.project(o.reshape(1, 2))[0]
        p.setPen(QPen(QColor("#04211d"), 1.4))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(*s0), 4, 4)

    def _draw_hud(self, p: QPainter) -> None:
        p.setFont(QFont("monospace", 9))
        p.setPen(QColor(255, 255, 255, 120))
        ids = {s for s, m in self.modes.items() if m != "off"}
        b = self.doc.bounds(ids) if ids else None
        if b:
            sz = b[1] - b[0]
            p.drawText(QPointF(12, self.height() - 12), f"job {sz[0]:.1f} × {sz[1]:.1f} mm   (page {self.doc.width_mm:.1f} × {self.doc.height_mm:.1f})")
        x = self.width() - 200
        y = 22
        p.setFont(QFont("sans", 9))
        for label, key in (("Line", "line"), ("Fill", "fill"), ("Off", "off")):
            p.setPen(QPen(QColor(MODE_COLORS[key]), 3))
            p.drawLine(QPointF(x, y - 4), QPointF(x + 16, y - 4))
            p.setPen(QColor(255, 255, 255, 150))
            p.drawText(QPointF(x + 22, y), label)
            x += 66


def add_view_buttons(view: CanvasView, extra: list[tuple] | None = None) -> QWidget:
    """Floating row of buttons on top of a view (Fit, plus the extras: (text, tooltip, slot, checkable, checked))."""
    bar = QWidget(view)
    bar.setStyleSheet("background: transparent;")
    lay = QHBoxLayout(bar)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)

    def mk(text, tip, slot, checkable=False, checked=False):
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setChecked(checked)
        b.setStyleSheet("padding:4px 9px; font-size:11px; background: rgba(20,26,33,200);")
        b.clicked.connect(slot)
        lay.addWidget(b)
        return b

    mk("Fit", "Fit everything (double-click)", view.fit)
    for e in extra or []:
        mk(*e)
    bar.move(10, 10)
    bar.adjustSize()
    return bar
