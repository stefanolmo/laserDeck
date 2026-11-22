"""Promo video (60 s, 1920x1080, 30 fps) built from REAL screenshots of the app (offscreen Qt) + animated titles + a
synthesised soundtrack, encoded with ffmpeg.

    QT_QPA_PLATFORM=offscreen python3 docs/make_video.py                 # -> promo/laserdeck-promo.mp4
    QT_QPA_PLATFORM=offscreen python3 docs/make_video.py --stills 2,8,14 # PNGs of those seconds, for a quick look

The job shown in the Sender is a real generated G-code; its progress is driven frame by frame (no real machine involved).
"""
import math
import os
import subprocess
import sys
import time
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
from PySide6.QtCore import QCoreApplication, QPoint, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from laserdeck import __version__, theme  # noqa: E402
from laserdeck.mainwindow import MainWindow  # noqa: E402
from laserdeck.splash import SplashScreen  # noqa: E402
from laserdeck.theme import icon  # noqa: E402

W, H, FPS, DUR = 1920, 1080, 30, 60.0
OUT = os.path.join(ROOT, "promo")
FONT = "Inter"


# ------------------------------------------------------------------ tiny helpers
def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def lerp(a, b, t):
    return a + (b - a) * t


def seg(t, a, b):
    """0..1 progress of t inside [a, b]."""
    return clamp((t - a) / max(b - a, 1e-9))


def window(t, a, b, fade=0.5):
    """Opacity envelope of a scene living in [a, b]."""
    return ease(seg(t, a, a + fade)) * ease(seg(t, b - fade, b) * -1 + 1) if t < b else 0.0


def font(px, weight=QFont.DemiBold):
    f = QFont(FONT)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def text(p, s, x, y, px, color="#f1f5f9", alpha=1.0, weight=QFont.DemiBold, align=Qt.AlignLeft, w=1700, rise=0.0):
    p.save()
    p.setOpacity(p.opacity() * alpha)
    p.setFont(font(px, weight))
    p.setPen(QColor(color))
    fl = {Qt.AlignLeft: Qt.AlignLeft, Qt.AlignHCenter: Qt.AlignHCenter}.get(align, align)
    rect = QRectF(x - (w / 2 if align == Qt.AlignHCenter else 0), y + rise, w, px * 3.4)
    p.drawText(rect, int(fl | Qt.AlignTop | Qt.TextWordWrap), s)
    p.restore()


# ------------------------------------------------------------------ background
_BG = None


def background(p, t):
    global _BG
    if _BG is None:
        _BG = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        q = QPainter(_BG)
        g = QLinearGradient(0, 0, 0, H)
        g.setColorAt(0, QColor("#141b24"))
        g.setColorAt(1, QColor("#0a0e13"))
        q.fillRect(0, 0, W, H, g)
        q.end()
    p.drawImage(0, 0, _BG)
    # slowly drifting isometric grid + teal glow
    rz = math.radians(-35)
    pitch = math.radians(55)
    cz, sz, cx, sx = math.cos(rz), math.sin(rz), math.cos(pitch), math.sin(pitch)
    o = np.array([W * 0.62, H * 0.62])
    drift = (t * 6) % 40
    p.setPen(QPen(QColor(255, 255, 255, 14), 1.2))
    for v in range(-1200, 1201, 40):
        for horiz in (0, 1):
            a = np.array([[v + drift, -1200.0], [v + drift, 1200.0]]) if not horiz else np.array([[-1200.0, v + drift], [1200.0, v + drift]])
            pts = []
            for x, y in a:
                X = x * cz - y * sz
                Y = x * sz + y * cz
                pts.append((o[0] + X, o[1] - Y * sx))
            p.drawLine(QPointF(*pts[0]), QPointF(*pts[1]))
    g = QRadialGradient(QPointF(W * 0.7, H * 0.45), W * 0.7)
    g.setColorAt(0, QColor(45, 212, 191, 34))
    g.setColorAt(1, QColor(45, 212, 191, 0))
    p.fillRect(0, 0, W, H, g)


# ------------------------------------------------------------------ captures of the real app
class Captures:
    pass


def capture_app() -> Captures:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(theme.STYLESHEET)
    f = QFont(app.font())
    f.setPointSize(10)
    app.setFont(f)

    def pump(ms):
        t0 = time.time()
        while time.time() - t0 < ms / 1000:
            QCoreApplication.processEvents()
            time.sleep(0.01)

    c = Captures()
    w = MainWindow()
    w.qs.clear()
    w.resize(1760, 990)
    w.show()
    w.set_mode(1)
    conv = w.converter
    conv.load_svg(os.path.join(ROOT, "examples", "promo.svg"))
    pump(1200)
    scroll = conv.findChildren(QScrollArea)[0]

    def rect_of(wid):
        tl = wid.mapTo(w, QPoint(0, 0))
        return QRectF(tl.x(), tl.y(), wid.width(), wid.height())

    def grab():
        pump(250)
        return w.grab().toImage().convertToFormat(QImage.Format_ARGB32)

    # A: everything at its defaults, list on top
    scroll.verticalScrollBar().setValue(0)
    c.A = grab()
    c.r_file, c.r_table = rect_of(conv.file_label.parentWidget()), rect_of(conv.table)
    c.r_design = rect_of(conv.design)

    # B: the star selected, fill settings visible
    conv._select({"star"})
    conv.w["fill_power"].setValue(35.0)
    conv.w["interval"].setValue(0.08)
    pump(700)
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum() * 0 + conv.sel_label.parentWidget().y() - 20)
    c.B = grab()
    card = conv.sel_label.parentWidget()
    while card is not None and card.objectName() != "card":
        card = card.parentWidget()
    c.r_settings = rect_of(card)

    # C: the ring switched off with the enable switch
    scroll.verticalScrollBar().setValue(0)
    conv._select({"ring"})
    conv.chk_enabled.setChecked(False)
    pump(700)
    c.C = grab()
    c.r_switch = rect_of(conv.chk_enabled)
    c.r_table2 = rect_of(conv.table)

    # D: ring back on, toolpath preview
    conv.chk_enabled.setChecked(True)
    conv._select(set())
    pump(1500)
    conv._set_view(1)
    pump(500)
    c.D = grab()
    c.r_preview = rect_of(conv.stack)

    # sender: the generated job loaded, simulator connected
    conv._send()
    pump(800)
    w.port_combo.setCurrentIndex(w.port_combo.findData("simulator"))
    w.toggle_connection()
    pump(2000)
    w.view.show_rapids = False
    c.w = w
    c.program = w.program
    c.r_view = rect_of(w.view)
    c.r_left = rect_of(w.stack.widget(0).widget(0))
    c.r_editor = rect_of(w.editor)
    w._running = True                                   # toolbar in "running" state; from now on no event pumping
    w._update_enabled()
    w.view.set_frame(None, None)
    return c


def sender_frame(c: Captures, f: float) -> QImage:
    """The Sender with the job `f` (0..1) completed."""
    w, prog = c.w, c.program
    p = prog.path
    n = len(p)
    k = max(1, int(f * n))
    line = int(p.line[k - 1])
    head = p.end[k - 1][:2]
    w.view.set_progress_line(line)
    w.view.set_tool(head)
    w.editor.set_exec_line(line)
    w.axes["X"].set_values(float(head[0]), float(head[0]))
    w.axes["Y"].set_values(float(head[1]), float(head[1]))
    w.badge.set_state("Run")
    w.progress.setValue(int(1000 * f))
    el = f * prog.total_time
    w.sb_time.setText(f"{int(el // 60):02d}:{int(el % 60):02d}  /  remaining ≈ {int((prog.total_time - el) // 60):02d}:{int((prog.total_time - el) % 60):02d}")
    w.live.setText(f"Speed 3000 mm/min    Power {int(p.power[k - 1])} S ({p.power[k - 1] / 10:.0f} %)")
    return w.grab().toImage().convertToFormat(QImage.Format_ARGB32)


# ------------------------------------------------------------------ compositing helpers
def crop_around(rect: QRectF, width: float, img: QImage, dy=0.0) -> QRectF:
    cx, cy = rect.center().x(), rect.center().y() + dy
    h = width * 9 / 16
    x = clamp(cx - width / 2, 0, img.width() - width)
    y = clamp(cy - h / 2, 0, img.height() - h)
    return QRectF(x, y, width, h)


def lerp_rect(a: QRectF, b: QRectF, t: float) -> QRectF:
    return QRectF(lerp(a.x(), b.x(), t), lerp(a.y(), b.y(), t), lerp(a.width(), b.width(), t), lerp(a.height(), b.height(), t))


DEST = QRectF(80, 50, 1760, 990)


def draw_window(p, img, src: QRectF, alpha=1.0, ring: QRectF | None = None, ring_a=1.0, ring_color="#2dd4bf"):
    p.save()
    p.setOpacity(p.opacity() * alpha)
    path = QPainterPath()
    path.addRoundedRect(DEST, 18, 18)
    # shadow
    for i in range(8):
        sp = QPainterPath()
        sp.addRoundedRect(DEST.adjusted(-i * 3, -i * 3 + 14, i * 3, i * 3 + 14), 22, 22)
        p.fillPath(sp, QColor(0, 0, 0, 14))
    p.setClipPath(path)
    p.drawImage(DEST, img, src)
    if ring is not None and ring_a > 0:
        k = DEST.width() / src.width()
        rr = QRectF(DEST.x() + (ring.x() - src.x()) * k, DEST.y() + (ring.y() - src.y()) * k, ring.width() * k, ring.height() * k).adjusted(-6, -6, 6, 6)
        col = QColor(ring_color)
        col.setAlpha(int(255 * ring_a))
        glow = QColor(ring_color)
        glow.setAlpha(int(60 * ring_a))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(glow, 14))
        p.drawRoundedRect(rr, 12, 12)
        p.setPen(QPen(col, 3.5))
        p.drawRoundedRect(rr, 12, 12)
    p.setClipping(False)
    p.setPen(QPen(QColor(theme.BORDER), 2))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(DEST, 18, 18)
    p.restore()


def caption(p, s, t, a, b, sub=None):
    """Bottom pill that slides in at `a` and out at `b`."""
    k = ease(seg(t, a, a + 0.45)) * (1 - ease(seg(t, b - 0.4, b)))
    if k <= 0:
        return
    p.save()
    p.setOpacity(p.opacity() * k)
    p.setFont(font(40, QFont.DemiBold))
    fm = p.fontMetrics()
    tw = fm.horizontalAdvance(s)
    bw = tw + 90
    rect = QRectF((W - bw) / 2, H - 150 + (1 - k) * 24, bw, 84)
    path = QPainterPath()
    path.addRoundedRect(rect, 42, 42)
    p.fillPath(path, QColor(8, 12, 17, 225))
    p.setPen(QPen(QColor(45, 212, 191, 200), 2))
    p.drawPath(path)
    p.setPen(QColor("#f1f5f9"))
    p.drawText(rect, int(Qt.AlignCenter), s)
    p.restore()


# ------------------------------------------------------------------ scenes
def scene_splash(p, t, splash):
    a = window(t, 0.0, 4.6, 0.6) if t > 0.6 else ease(seg(t, 0, 0.6))
    ms = min(t * 1000.0, 2650)
    splash.set_time(ms)
    img = splash.grab().toImage().convertToFormat(QImage.Format_ARGB32)
    k = 2.0 + 0.05 * seg(t, 0, 4.6)
    w_, h_ = img.width() * k, img.height() * k
    p.save()
    p.setOpacity(a)
    p.drawImage(QRectF((W - w_) / 2, (H - h_) / 2, w_, h_), img)
    p.restore()


def scene_pitch(p, t):
    a0, a1 = 4.3, 11.6
    al = window(t, a0, a1, 0.5)
    if al <= 0:
        return
    p.save()
    p.setOpacity(al)
    u = lambda s0: ease(seg(t, s0, s0 + 0.7))  # noqa: E731
    text(p, "Dall'SVG al laser.", W / 2, 300, 150, "#f1f5f9", u(4.8), QFont.Black, Qt.AlignHCenter, rise=(1 - u(4.8)) * 50)
    text(p, "Decidi cosa fa ogni singolo path.", W / 2, 500, 70, theme.ACCENT, u(6.0), QFont.DemiBold, Qt.AlignHCenter, rise=(1 - u(6.0)) * 40)
    # the three modes as chips
    chips = [("Linea", "#4aa8ff"), ("Riempimento", "#ff7a45"), ("Riempimento + Linea", "#ff7a45")]
    p.setFont(font(44))
    widths = [p.fontMetrics().horizontalAdvance(s) + 100 for s, _ in chips]
    total = sum(widths) + 40 * (len(chips) - 1)
    x = (W - total) / 2
    for i, ((s, col), wd) in enumerate(zip(chips, widths)):
        k = u(7.2 + i * 0.35)
        r = QRectF(x, 680 + (1 - k) * 40, wd, 100)
        p.save()
        p.setOpacity(p.opacity() * k)
        path = QPainterPath()
        path.addRoundedRect(r, 50, 50)
        c = QColor(col)
        c.setAlpha(40)
        p.fillPath(path, c)
        p.setPen(QPen(QColor(col), 3))
        p.drawPath(path)
        p.setPen(QColor("#f1f5f9"))
        p.drawText(r, int(Qt.AlignCenter), s)
        p.restore()
        x += wd + 40
    text(p, "con potenza, velocità e passate proprie", W / 2, 850, 46, theme.MUTED, u(8.6), QFont.Normal, Qt.AlignHCenter)
    p.restore()


def scene_converter(p, t, c: Captures):
    a0, a1 = 11.2, 27.4
    al = window(t, a0, a1, 0.5)
    if al <= 0:
        return
    full = QRectF(0, 0, c.A.width(), c.A.height())
    steps = [  # (t0, t1, image, src0, src1, ring, text)
        (11.2, 15.6, c.A, full, crop_around(c.r_table, 1130, c.A, dy=-60), c.r_table, "Importa l'SVG: ogni path ha le sue impostazioni"),
        (15.6, 19.6, c.B, crop_around(c.r_table, 1130, c.B, dy=-60), crop_around(c.r_settings, 1000, c.B), c.r_settings,
         "Linea, riempimento o entrambi: potenza, velocità, passate"),
        (19.6, 23.6, c.C, crop_around(c.r_settings, 1000, c.C), crop_around(c.r_switch, 1150, c.C, dy=120), c.r_switch,
         "Uno switch accende o spegne i path: le impostazioni restano"),
        (23.6, 27.4, c.D, crop_around(c.r_switch, 1150, c.D, dy=120), full, c.r_preview,
         "Anteprima colorata per potenza: incisioni prima, tagli per ultimi"),
    ]
    p.save()
    p.setOpacity(al)
    for i, (t0, t1, img, s0, s1, ring, cap) in enumerate(steps):
        if t < t0 - 0.01 or t > t1 + 0.7:
            continue
        u = seg(t, t0, t1)
        src = lerp_rect(s0, s1, ease(u))
        fade = ease(seg(t, t0, t0 + 0.6)) if i > 0 else 1.0
        # the previous image stays underneath while the new one fades in (a cross-fade between the real captures)
        if i > 0 and fade < 1.0:
            pimg, ps = steps[i - 1][2], steps[i - 1][4]
            draw_window(p, pimg, lerp_rect(ps, ps, 0))
        draw_window(p, img, src, alpha=fade, ring=ring, ring_a=ease(seg(t, t0 + 0.8, t0 + 1.4)) * (1 - ease(seg(t, t1 - 0.5, t1))))
        caption(p, cap, t, t0 + 0.3, t1 - 0.05)
    p.restore()


def scene_sender(p, t, c: Captures, cache):
    a0, a1 = 27.0, 43.4
    al = window(t, a0, a1, 0.5)
    if al <= 0:
        return
    f = ease(seg(t, 30.6, 42.4)) * 0.999 if t >= 30.6 else 0.0
    # before the run: the job loaded, framed (dashed bbox); then the real run
    if f <= 0:
        key = "idle"
        if key not in cache:
            c.w.view.set_progress_line(-1)
            cache[key] = sender_frame(c, 0.0005)
        img = cache[key]
    else:
        img = sender_frame(c, f)
    full = QRectF(0, 0, img.width(), img.height())
    zoom = crop_around(full, 1690, img)                  # gentle, centred push-in: the whole window stays readable
    src = lerp_rect(full, zoom, ease(seg(t, 27.0, 43.0)))
    ring = c.r_view if t < 30.6 else None
    p.save()
    p.setOpacity(al)
    draw_window(p, img, src, ring=ring, ring_a=ease(seg(t, 27.6, 28.2)) * (1 - ease(seg(t, 30.0, 30.6))))
    caption(p, "Frame: il bordo del lavoro, a laser spento", t, 27.4, 30.4)
    caption(p, "Streaming GRBL a conteggio caratteri, anche a velocità alta", t, 31.0, 36.6)
    caption(p, "La parte già incisa diventa grigia, con tempo residuo", t, 37.0, 42.8)
    p.restore()


CARDS = [
    ("frame", "Frame del lavoro", "Traccia il bordo a laser spento o con un puntatore a bassa potenza"),
    ("laser", "Impostazioni per path", "Linea, riempimento o entrambi, con potenza e velocità proprie"),
    ("save", "Preset per materiale", "«Compensato 3 mm» salvato e riapplicato in un clic"),
    ("home", "Homing e zero automatici", "Va ai finecorsa, avanza di X/Y mm e imposta lo zero"),
    ("check", "Protezioni", "Avvisi su laser mode, soft limits e porta; jog lontano dai finecorsa"),
    ("gear", "GRBL 1.1 compatibile", "Longer Ray5 mini e simili, con simulatore integrato"),
]


def scene_features(p, t):
    a0, a1 = 43.0, 54.4
    al = window(t, a0, a1, 0.5)
    if al <= 0:
        return
    p.save()
    p.setOpacity(al)
    text(p, "Tutto quello che serve davanti al laser", W / 2, 70, 74, "#f1f5f9", ease(seg(t, 43.4, 44.1)), QFont.Black, Qt.AlignHCenter)
    cw, ch, gx, gy = 560, 330, 40, 40
    x0 = (W - (3 * cw + 2 * gx)) / 2
    y0 = 230
    for i, (ic, title, body) in enumerate(CARDS):
        k = ease(seg(t, 44.0 + i * 0.45, 44.8 + i * 0.45))
        if k <= 0:
            continue
        col, row = i % 3, i // 3
        r = QRectF(x0 + col * (cw + gx), y0 + row * (ch + gy) + (1 - k) * 60, cw, ch)
        p.save()
        p.setOpacity(p.opacity() * k)
        path = QPainterPath()
        path.addRoundedRect(r, 22, 22)
        p.fillPath(path, QColor(26, 34, 44, 235))
        p.setPen(QPen(QColor(theme.BORDER), 2))
        p.drawPath(path)
        pm = icon(ic, theme.ACCENT).pixmap(96, 96)
        p.drawPixmap(int(r.x() + 36), int(r.y() + 36), pm)
        text(p, title, r.x() + 36, r.y() + 160, 40, "#f1f5f9")
        text(p, body, r.x() + 36, r.y() + 220, 28, theme.MUTED, 1.0, QFont.Normal, w=r.width() - 72)
        p.restore()
    p.restore()


def scene_outro(p, t, logo):
    a0 = 53.9
    al = ease(seg(t, a0, a0 + 0.7)) * (1 - ease(seg(t, 59.0, 60.0)))
    if al <= 0:
        return
    p.save()
    p.setOpacity(al)
    k = ease(seg(t, a0, a0 + 1.0))
    s = 300 + 20 * seg(t, a0, 60)
    logo.render(p, QRectF((W - s) / 2, 150 - (1 - k) * 30, s, s))
    p.setFont(font(150, QFont.Black))
    wl = p.fontMetrics().horizontalAdvance("Laser ")
    wd = p.fontMetrics().horizontalAdvance("Deck")
    x = (W - (wl + wd)) / 2
    p.setPen(QColor("#f1f5f9"))
    p.drawText(QPointF(x, 700), "Laser ")
    p.setPen(QColor(theme.ACCENT))
    p.drawText(QPointF(x + wl, 700), "Deck")
    text(p, "Sender e convertitore SVG per laser GRBL 1.1", W / 2, 740, 50, theme.MUTED, ease(seg(t, a0 + 0.8, a0 + 1.5)), QFont.Normal, Qt.AlignHCenter)
    text(p, "github.com/stefanolmo/laserDeck", W / 2, 840, 44, theme.ACCENT, ease(seg(t, a0 + 1.4, a0 + 2.1)), QFont.DemiBold, Qt.AlignHCenter)
    text(p, "Stefano Ciurleo & Claude Code  ·  2026", W / 2, 930, 32, "#64748b", ease(seg(t, a0 + 1.9, a0 + 2.6)), QFont.Normal, Qt.AlignHCenter)
    p.restore()


# ------------------------------------------------------------------ soundtrack
def make_audio(path):
    sr = 44100
    n = int(sr * DUR)
    tt = np.arange(n) / sr
    out = np.zeros(n)
    prog = [(110.00, 130.81, 164.81), (87.31, 110.00, 130.81), (130.81, 164.81, 196.00), (98.00, 123.47, 146.83)]
    bar = 7.5
    for i in range(int(DUR / bar) + 1):
        a, b = int(i * bar * sr), min(n, int((i + 1) * bar * sr))
        if a >= n:
            break
        ch = prog[i % 4]
        x = tt[a:b] - tt[a]
        env = np.minimum(1, x / 1.2) * np.minimum(1, (bar - x) / 1.2)
        pad = sum(np.sin(2 * np.pi * f * x) + 0.4 * np.sin(2 * np.pi * 2 * f * x * 1.002) + 0.2 * np.sin(2 * np.pi * 3 * f * x) for f in ch)
        out[a:b] += 0.11 * pad * env
    step = 60 / 112 / 2                                         # eighth notes at 112 bpm
    notes = [220.00, 261.63, 329.63, 392.00, 329.63, 261.63]
    for j in range(int(DUR / step)):
        s0 = j * step
        if s0 < 4.0 or s0 > 57.0:
            continue
        chord = prog[int(s0 // bar) % 4]
        f = chord[j % 3] * 2 * (2 if j % 6 == 5 else 1)
        a = int(s0 * sr)
        ln = int(0.5 * sr)
        x = np.arange(min(ln, n - a)) / sr
        out[a:a + len(x)] += 0.06 * np.sin(2 * np.pi * f * x) * np.exp(-x * 7)
    beat = 60 / 112
    for j in range(int(DUR / beat)):
        s0 = j * beat
        if s0 < 11.0 or s0 > 54.0:
            continue
        a = int(s0 * sr)
        x = np.arange(min(int(0.3 * sr), n - a)) / sr
        out[a:a + len(x)] += 0.28 * np.sin(2 * np.pi * (48 + 90 * np.exp(-x * 30)) * x) * np.exp(-x * 11)
    out *= np.minimum(1, tt / 1.5) * np.minimum(1, (DUR - tt) / 3.0)
    out /= max(1e-9, np.abs(out).max()) / 0.7
    st = np.stack([out, np.roll(out, 90) * 0.97], 1)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes((st * 32767).astype("<i2").tobytes())


# ------------------------------------------------------------------ main
def main():
    stills = None
    if "--stills" in sys.argv:
        stills = [float(v) for v in sys.argv[sys.argv.index("--stills") + 1].split(",")]
    os.makedirs(OUT, exist_ok=True)
    print("capturing the app…", flush=True)
    c = capture_app()
    splash = SplashScreen(__version__)
    logo = QSvgRenderer(os.path.join(ROOT, "laserdeck", "laserdeck.svg"))
    cache: dict = {}

    def frame(t) -> QImage:
        img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        background(p, t)
        if t < 5.0:
            scene_splash(p, t, splash)
        scene_pitch(p, t)
        scene_converter(p, t, c)
        scene_sender(p, t, c, cache)
        scene_features(p, t)
        scene_outro(p, t, logo)
        p.end()
        return img

    if stills:
        for t in stills:
            frame(t).save(os.path.join(OUT, f"still_{t:05.1f}.png"))
        os._exit(0)

    wav = os.path.join(OUT, "soundtrack.wav")
    make_audio(wav)
    mp4 = os.path.join(OUT, "laserdeck-promo.mp4")
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(FPS),
                           "-i", "-", "-i", wav, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                           "-c:a", "aac", "-b:a", "192k", "-t", str(DUR), "-movflags", "+faststart", mp4], stdin=subprocess.PIPE)
    total = int(DUR * FPS)
    t0 = time.time()
    for i in range(total):
        img = frame(i / FPS)
        ff.stdin.write(bytes(img.constBits()))
        if i % 90 == 0:
            print(f"frame {i}/{total}  ({time.time() - t0:.0f}s)", flush=True)
    ff.stdin.close()
    ff.wait()
    print("done:", mp4)
    os._exit(0)


if __name__ == "__main__":
    main()
