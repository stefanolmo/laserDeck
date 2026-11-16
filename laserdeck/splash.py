"""Animated splash screen: a stylised laser raster fill (zig-zag scan lines inside a rounded contour, projected
isometrically) drawn by a glowing beam, in the colours of the app, with title, credits and version."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QApplication, QWidget

from . import theme
from .canvas import power_color

W, H = 780, 440
CREDITS = "Stefano Ciurleo & Claude Code - 2026"


def _ease(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


class SplashScreen(QWidget):
    finished = Signal()

    def __init__(self, version: str, duration_ms: int = 3000, parent=None):
        super().__init__(parent)
        self.version = version
        self.duration = duration_ms
        self.t = 0.0                       # elapsed milliseconds (set_time() overrides it for stills)
        self.setWindowFlags(Qt.SplashScreen | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(W, H)
        self._clock = QElapsedTimer()
        self._tick = QTimer(self, interval=16)
        self._tick.timeout.connect(self._advance)
        self._build_art()

    # ------------------------------------------------------------------ geometry of the artwork
    def _build_art(self) -> None:
        """A raster fill: bidirectional scan lines clipped to a squircle, drawn row after row."""
        n_rows, n_pts, k = 30, 26, 3.2
        rows = []
        for r in range(n_rows):
            y = -1.0 + 2.0 * (r + 0.5) / n_rows
            hw = (1.0 - abs(y) ** k) ** (1.0 / k)
            xs = np.linspace(-hw, hw, n_pts)
            if r % 2:
                xs = xs[::-1]
            rows.append(np.column_stack([xs, np.full(n_pts, y), np.full(n_pts, r / (n_rows - 1))]))
        pts = np.vstack(rows)
        self.raster = pts[:, :2] * 118.0
        self.row_t = pts[:, 2]                         # 0..1 along the job: drives the power colour
        self.row_id = np.repeat(np.arange(n_rows), n_pts)
        t = np.linspace(0, 2 * math.pi, 240, endpoint=False)
        c, s = np.cos(t), np.sin(t)
        e = 2.0 / k
        self.contour = np.column_stack([1.16 * np.sign(c) * np.abs(c) ** e, 1.16 * np.sign(s) * np.abs(s) ** e]) * 118.0
        yaw, pitch = math.radians(-35), math.radians(55)
        cz, sz, cx, sx = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
        rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
        rx = np.array([[1, 0, 0], [0, cx, sx], [0, -sx, cx]])
        self._rot = rx @ rz

    def _proj(self, pts: np.ndarray, origin=(0.0, 0.0), z: float = 0.0) -> np.ndarray:
        pts = np.atleast_2d(pts)
        p3 = np.column_stack([pts[:, 0], pts[:, 1], np.full(len(pts), z)]) if pts.shape[1] == 2 else pts
        q = p3 @ self._rot.T
        return np.column_stack([origin[0] + q[:, 0], origin[1] - q[:, 1]])

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        scr = QApplication.primaryScreen()
        if scr is not None:
            g = scr.availableGeometry()
            self.move(g.center().x() - W // 2, g.center().y() - H // 2)
        self.show()
        self.raise_()
        self._clock.start()
        self._tick.start()

    def _advance(self) -> None:
        self.t = float(self._clock.elapsed())
        if self.t >= self.duration:
            self._tick.stop()
            self.close()
            self.finished.emit()
            return
        self.update()

    def set_time(self, ms: float) -> None:
        """Freeze the animation at `ms` (used for screenshots and tests)."""
        self.t = ms
        self.update()

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        k = self.t / max(self.duration, 1)
        fade_in = _ease(self.t / 350.0)
        fade_out = 1.0 - _ease((self.t - (self.duration - 300)) / 300.0)
        p.setOpacity(max(0.0, min(fade_in, fade_out)))
        self._paint_card(p)
        self._paint_grid(p)
        self._paint_job(p, _ease(k / 0.8))
        self._paint_axes(p)
        self._paint_text(p)
        self._paint_progress(p, k)
        p.end()

    def _paint_card(self, p: QPainter) -> None:
        rect = QRectF(8, 8, W - 16, H - 16)
        path = QPainterPath()
        path.addRoundedRect(rect, 22, 22)
        glow = QRadialGradient(QPointF(W * 0.68, H * 0.45), W * 0.62)
        glow.setColorAt(0, QColor(45, 212, 191, 38))
        glow.setColorAt(1, QColor(45, 212, 191, 0))
        g = QLinearGradient(0, 0, 0, H)
        g.setColorAt(0, QColor("#141b24"))
        g.setColorAt(1, QColor("#0a0e13"))
        p.fillPath(path, g)
        p.fillPath(path, QBrush(glow))
        p.setPen(QPen(QColor(theme.BORDER), 1.5))
        p.drawPath(path)
        p.setClipPath(path)

    def _paint_grid(self, p: QPainter) -> None:
        origin = (W * 0.66, H * 0.56)
        p.setPen(QPen(QColor(255, 255, 255, 16), 1))
        span, step = 220, 22
        for v in range(-span, span + 1, step):
            a = self._proj(np.array([[v, -span], [v, span]], float), origin, -45.0)
            b = self._proj(np.array([[-span, v], [span, v]], float), origin, -45.0)
            p.drawLine(QPointF(*a[0]), QPointF(*a[1]))
            p.drawLine(QPointF(*b[0]), QPointF(*b[1]))
        fade = QLinearGradient(0, 0, W * 0.55, 0)
        fade.setColorAt(0, QColor("#0e1319"))
        fade.setColorAt(1, QColor(14, 19, 25, 0))
        p.fillRect(QRectF(0, 0, W * 0.55, H), fade)

    def _paint_job(self, p: QPainter, prog: float) -> None:
        origin = (W * 0.66, H * 0.56)
        n = len(self.raster)
        m = max(2, int(n * prog))
        cont = self._proj(self.contour, origin)
        p.setPen(QPen(QColor(255, 122, 69, int(190 * min(1.0, prog * 1.6))), 2.2))      # outline (the cut), warm
        p.drawPolyline([QPointF(*q) for q in np.vstack([cont, cont[:1]])])
        pts = self._proj(self.raster[:m], origin)
        for i in range(1, m):
            if self.row_id[i] != self.row_id[i - 1]:
                continue                                                               # the head turns: no burn
            p.setPen(QPen(power_color(self.row_t[i]), 2.0, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(*pts[i - 1]), QPointF(*pts[i]))
        # the beam: a thin vertical line with a glowing spot on the material
        tip = pts[-1]
        top = self._proj(np.array([[*self.raster[m - 1]]]), origin, 120.0)[0]
        halo = QRadialGradient(QPointF(*tip), 30)
        halo.setColorAt(0, QColor(255, 122, 69, 200))
        halo.setColorAt(1, QColor(255, 122, 69, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(*tip), 30, 30)
        beam = QLinearGradient(QPointF(*top), QPointF(*tip))
        beam.setColorAt(0, QColor(255, 255, 255, 0))
        beam.setColorAt(1, QColor(255, 190, 150, 230))
        p.setPen(QPen(QBrush(beam), 2.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(*top), QPointF(*tip))
        p.setPen(QPen(QColor("#fff7ed"), 2))
        p.setBrush(QColor("#ffb48a"))
        p.drawEllipse(QPointF(*tip), 4.5, 4.5)

    def _paint_axes(self, p: QPainter) -> None:
        o = (W * 0.66 - 150, H * 0.56 + 150)
        for i, name in enumerate("XY"):
            e = np.zeros(3)
            e[i] = 46
            a = self._proj(np.array([[0, 0, 0.0], e]), o)
            col = QColor(theme.AXIS[name])
            col.setAlpha(190)
            p.setPen(QPen(col, 2.2, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(*a[0]), QPointF(*a[1]))
            p.setFont(QFont("sans", 8, QFont.Bold))
            p.drawText(QPointF(a[1][0] + 4, a[1][1] - 3), name)

    def _paint_text(self, p: QPainter) -> None:
        x = 58
        f = QFont("sans", 46, QFont.Black)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 1.5)
        p.setFont(f)
        p.setPen(QColor("#f1f5f9"))
        p.drawText(QPointF(x, H * 0.47), "Laser")
        w = p.fontMetrics().horizontalAdvance("Laser ")
        p.setPen(QColor(theme.ACCENT))
        p.drawText(QPointF(x + w, H * 0.47), "Deck")
        p.setPen(Qt.NoPen)
        g = QLinearGradient(x, 0, x + 270, 0)
        g.setColorAt(0, QColor(theme.ACCENT))
        g.setColorAt(1, QColor(45, 212, 191, 0))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(x, H * 0.47 + 16, 270, 3), 1.5, 1.5)
        p.setFont(QFont("sans", 11))
        p.setPen(QColor(theme.MUTED))
        p.drawText(QPointF(x + 2, H * 0.47 + 50), CREDITS)
        p.setFont(QFont("sans", 10, QFont.DemiBold))
        p.setPen(QColor(theme.ACCENT))
        p.drawText(QPointF(x + 2, H * 0.47 + 76), f"Version {self.version}")
        p.setFont(QFont("sans", 8))
        p.setPen(QColor(255, 255, 255, 70))
        p.drawText(QPointF(x + 2, H - 38), "GRBL laser sender  ·  SVG to G-code converter")

    def _paint_progress(self, p: QPainter, k: float) -> None:
        x0, x1, y = 58.0, W - 58.0, H - 24.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 18))
        p.drawRoundedRect(QRectF(x0, y, x1 - x0, 3), 1.5, 1.5)
        g = QLinearGradient(x0, 0, x1, 0)
        g.setColorAt(0, QColor(theme.ACCENT))
        g.setColorAt(1, QColor("#38bdf8"))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(x0, y, (x1 - x0) * min(k, 1.0), 3), 1.5, 1.5)
