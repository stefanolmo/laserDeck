"""SVG polylines -> laser toolpaths -> G-code for GRBL 1.1 in laser mode.

Every SVG shape has its own `PathSettings`: what the laser must do (nothing / line / fill / fill + line) and with which
power, speed and passes. Fill uses a scan-line hatch (angle, interval, bidirectional, overscan, cross-hatch) computed on
the shape's polygons with the SVG fill rule, so holes stay empty. Lines follow the outline once (optionally offset by the
kerf). Geometry operations (union / offset) use pyclipper, the same Clipper library as CNC Deck.

Ordering follows common laser practice: engrave first, cut last, inner contours before outer ones, nearest-neighbour
travel; passes of a group run one after the other so a thick cut does not scorch a single spot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pyclipper

from .svgimport import SvgDoc, SvgShape

SCALE = 10000.0            # Clipper integer units per mm (0.1 µm)
ARC_TOL = 0.005            # mm, round-join arc tolerance
MODES = ("off", "line", "fill", "both")


@dataclass
class PathSettings:
    mode: str = "line"             # off | line | fill | both (fill + line)
    last_mode: str = "line"        # mode before switching to off: restored by the «On» button
    # line (outline)
    line_power: float = 30.0       # % of S max
    line_speed: float = 1000.0     # mm/min
    line_passes: int = 1
    offset: float = 0.0            # mm, + grows the part, − shrinks it (closed paths): kerf compensation
    # fill (hatch)
    fill_power: float = 25.0
    fill_speed: float = 3000.0
    fill_passes: int = 1
    interval: float = 0.1          # mm between scan lines
    angle: float = 0.0             # degrees
    bidir: bool = True             # zig-zag (False: always left → right)
    crosshatch: bool = False       # second pass at +90°
    overscan: float = 0.0          # mm of laser-off run-in/run-out beyond each line

    @property
    def does_line(self) -> bool:
        return self.mode in ("line", "both")

    @property
    def does_fill(self) -> bool:
        return self.mode in ("fill", "both")


@dataclass
class Job:
    s_max: float = 1000.0          # $30 (value of S for 100 % power)
    laser_cmd: str = "M4"          # M4 dynamic power (recommended) | M3 constant power
    air_assist: bool = False       # M8 ... M9
    origin: str = "bottom-left"    # bottom-left | top-left | center | page
    order: str = "engrave-first"   # engrave-first | listed
    optimize: bool = True          # nearest-neighbour ordering of the paths
    inner_first: bool = True       # cut inner contours before the outer ones
    return_home: bool = True       # G0 X0 Y0 at the end
    safe_travel: bool = False      # force S0 on travel moves: only for firmware with $32=0 (slower)
    name: str = ""


@dataclass
class CamResult:
    gcode: str = ""
    warnings: list[str] = field(default_factory=list)
    n_paths: int = 0


def default_settings(shape: SvgShape) -> PathSettings:
    """Suggest what to do from the SVG style: stroke only → line, fill only → fill, both/none → line."""
    st = PathSettings()
    if shape.fill and not shape.stroke and any(sp.closed for sp in shape.subpaths):
        st.mode = "fill"
    return st


# ---------------------------------------------------------------- geometry helpers
def _to_clip(points: np.ndarray):
    return [(int(round(x * SCALE)), int(round(y * SCALE))) for x, y in points]


def _from_clip(path) -> np.ndarray:
    return np.array(path, dtype=float) / SCALE


def shape_polygons(shape: SvgShape) -> list:
    """Closed subpaths of one shape as Clipper paths, resolved with the shape's fill rule (outer + holes)."""
    paths = [_to_clip(sp.points) for sp in shape.subpaths if sp.closed and len(sp.points) >= 3]
    if not paths:
        return []
    pc = pyclipper.Pyclipper()
    pc.AddPaths(paths, pyclipper.PT_SUBJECT, True)
    ft = pyclipper.PFT_EVENODD if shape.fill_rule == "evenodd" else pyclipper.PFT_NONZERO
    return pc.Execute(pyclipper.CT_UNION, ft, ft)


def offset_polys(paths: list, delta_mm: float) -> list:
    if not paths or abs(delta_mm) < 1e-6:
        return paths
    po = pyclipper.PyclipperOffset(2.0, ARC_TOL * SCALE)
    po.AddPaths(paths, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    return po.Execute(delta_mm * SCALE)


def hatch(polys: list, interval: float, angle_deg: float, bidir: bool) -> list[np.ndarray]:
    """Scan-line fill of Clipper polygons (outer rings + holes). Returns segments as (2,2) arrays [[x0,y0],[x1,y1]]
    in row order; with `bidir` every other row runs backwards so the head never travels empty."""
    rings = [_from_clip(p) for p in polys if len(p) >= 3]
    if not rings:
        return []
    interval = max(interval, 0.01)
    th = math.radians(angle_deg)
    c, s = math.cos(th), math.sin(th)
    # rotate by -angle so the scan lines become horizontal
    rot = np.array([[c, s], [-s, c]])
    edges = []
    for r in rings:
        r2 = r @ rot.T
        nxt = np.roll(r2, -1, axis=0)
        edges.append(np.hstack([r2, nxt]))
    e = np.vstack(edges)
    x0, y0, x1, y1 = e[:, 0], e[:, 1], e[:, 2], e[:, 3]
    horiz = y0 == y1
    x0, y0, x1, y1 = x0[~horiz], y0[~horiz], x1[~horiz], y1[~horiz]
    if not len(x0):
        return []
    ymin, ymax = float(min(y0.min(), y1.min())), float(max(y0.max(), y1.max()))
    n_rows = int(math.floor((ymax - ymin) / interval + 1e-9))
    ys = ymin + interval * (np.arange(n_rows) + 0.5) if (ymax - ymin) >= interval else np.array([(ymin + ymax) / 2])
    lo, hi = np.minimum(y0, y1), np.maximum(y0, y1)
    slope = (x1 - x0) / (y1 - y0)
    back = np.array([[c, -s], [s, c]])
    rows: list[list[tuple[float, float, float]]] = []
    chunk = max(1, int(2_000_000 // max(len(x0), 1)))
    for a in range(0, len(ys), chunk):
        yy = ys[a:a + chunk][:, None]
        mask = (lo[None, :] <= yy) & (hi[None, :] > yy)
        xs = np.where(mask, x0[None, :] + (yy - y0[None, :]) * slope[None, :], np.inf)
        xs.sort(axis=1)
        cnt = mask.sum(axis=1)
        for r in range(xs.shape[0]):
            n = int(cnt[r]) - (int(cnt[r]) % 2)
            segs = [(float(xs[r, k]), float(xs[r, k + 1]), float(yy[r, 0])) for k in range(0, n, 2)
                    if xs[r, k + 1] - xs[r, k] > 1e-4]
            rows.append(segs)
    out: list[np.ndarray] = []
    for ri, segs in enumerate(rows):
        rev = bidir and ri % 2 == 1
        for (xa, xb, y) in (reversed(segs) if rev else segs):
            p = np.array([[xb, y], [xa, y]] if rev else [[xa, y], [xb, y]])
            out.append(p @ back.T)
    return out


# ---------------------------------------------------------------- tasks
@dataclass
class Task:
    kind: str                       # "fill" | "line"
    sid: str
    points: np.ndarray              # line: (N,2) path; fill: (K,2,2) strokes
    closed: bool = False
    settings: PathSettings = field(default_factory=PathSettings)
    seq: int = 0                    # position of the shape in the document
    group: int = 0

    @property
    def power(self) -> float:
        return self.settings.fill_power if self.kind == "fill" else self.settings.line_power

    @property
    def speed(self) -> float:
        return self.settings.fill_speed if self.kind == "fill" else self.settings.line_speed

    @property
    def passes(self) -> int:
        return max(1, self.settings.fill_passes if self.kind == "fill" else self.settings.line_passes)

    def start_point(self) -> np.ndarray:
        return self.points[0, 0] if self.kind == "fill" else self.points[0]


def origin_point(doc: SvgDoc, enabled: set[str], job: Job) -> tuple[float, float]:
    """Where the machine zero sits, in SVG coordinates (mm, y down)."""
    b = doc.bounds(enabled)
    if not b:
        return (0.0, 0.0)
    (minx, miny), (maxx, maxy) = b
    if job.origin == "top-left":
        return (float(minx), float(miny))
    if job.origin == "center":
        return (float((minx + maxx) / 2), float((miny + maxy) / 2))
    if job.origin == "page":
        return (0.0, float(doc.height_mm))              # lower-left corner of the SVG page = bed origin
    return (float(minx), float(maxy))                   # bottom-left of the design (default)


def _transform_fn(doc: SvgDoc, enabled: set[str], job: Job):
    """SVG (mm, y down) → machine coordinates: X = x − x0, Y = y0 − y (Y up, as on the machine)."""
    ox, oy = origin_point(doc, enabled, job)

    def f(p):
        return np.column_stack([p[:, 0] - ox, oy - p[:, 1]])
    return f


def _depths(tasks: list[Task]) -> list[int]:
    """Nesting depth of each closed line task (number of other closed outlines that contain it)."""
    rings = [(_to_clip(t.points), i) for i, t in enumerate(tasks) if t.closed and len(t.points) >= 3]
    depth = [0] * len(tasks)
    for ring, i in rings:
        pt = _to_clip(tasks[i].points[:1])[0]
        for other, j in rings:
            if i != j and pyclipper.PointInPolygon(pt, other) == 1:
                depth[i] += 1
    return depth


def _nn_order(tasks: list[Task], cur: np.ndarray) -> list[Task]:
    """Greedy nearest-neighbour ordering. Closed paths are rotated to start at the vertex closest to the head."""
    left = list(tasks)
    out: list[Task] = []
    while left:
        bi, bd = 0, 1e30
        for i, t in enumerate(left):
            if t.kind == "line" and t.closed:
                d = float(np.min(np.hypot(*(t.points - cur).T)))
            elif t.kind == "line":
                d = min(float(np.hypot(*(t.points[0] - cur))), float(np.hypot(*(t.points[-1] - cur))))
            else:
                d = float(np.hypot(*(t.start_point() - cur)))
            if d < bd:
                bi, bd = i, d
        t = left.pop(bi)
        if t.kind == "line":
            if t.closed:
                k = int(np.argmin(np.hypot(*(t.points - cur).T)))
                t.points = np.roll(t.points, -k, axis=0)
            elif float(np.hypot(*(t.points[-1] - cur))) < float(np.hypot(*(t.points[0] - cur))):
                t.points = t.points[::-1].copy()    # open path: start from the nearer end
        cur = t.points[-1] if (t.kind == "line" and not t.closed) else t.start_point()
        out.append(t)
    return out


# ---------------------------------------------------------------- G-code writer
def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class Writer:
    def __init__(self, job: Job):
        self.job = job
        self.lines: list[str] = []
        self.x: float | None = None
        self.y: float | None = None
        self.f: float | None = None
        self.s: int | None = None
        self.burn_mm = 0.0

    def power_s(self, pct: float) -> int:
        return 0 if pct <= 0 else max(1, int(round(min(pct, 100.0) / 100.0 * self.job.s_max)))

    def _xy(self, x: float, y: float) -> str:
        w = ""
        if self.x is None or _fmt(x) != _fmt(self.x):
            w += f"X{_fmt(x)}"
        if self.y is None or _fmt(y) != _fmt(self.y):
            w += f"Y{_fmt(y)}"
        self.x, self.y = x, y
        return w

    def travel(self, x: float, y: float) -> None:
        if self.x is not None and abs(x - self.x) < 5e-4 and abs(y - self.y) < 5e-4:
            return
        w = self._xy(x, y)
        if not w:
            return
        s = ""
        if self.job.safe_travel and self.s != 0:
            s = "S0"
            self.s = 0
        self.lines.append(f"G0{w}{s}")

    def cut(self, x: float, y: float, s: int, feed: float) -> None:
        prev = (self.x, self.y)
        w = self._xy(x, y)
        if not w:
            return
        if s > 0 and prev[0] is not None:
            self.burn_mm += math.hypot(x - prev[0], y - prev[1])
        w += self._sf(s, feed)
        self.lines.append(f"G1{w}")

    def _sf(self, s: int, feed: float) -> str:
        w = ""
        if self.s != s:
            w += f"S{s}"
            self.s = s
        if self.f is None or abs(self.f - feed) > 1e-6:
            w += f"F{_fmt(feed)}"
            self.f = feed
        return w

    def burn_path(self, pts: np.ndarray, closed: bool, s: int, feed: float) -> None:
        self.travel(*pts[0])
        for p in pts[1:]:
            self.cut(p[0], p[1], s, feed)
        if closed:
            self.cut(pts[0][0], pts[0][1], s, feed)

    def burn_stroke(self, a: np.ndarray, b: np.ndarray, s: int, feed: float, overscan: float) -> None:
        d = b - a
        ln = float(np.hypot(*d))
        if overscan > 0 and ln > 1e-9:
            u = d / ln
            self.travel(*(a - u * overscan))
            self.cut(a[0], a[1], 0, feed)
            self.cut(b[0], b[1], s, feed)
            e = b + u * overscan
            self.cut(e[0], e[1], 0, feed)
        else:
            self.travel(*a)
            self.cut(b[0], b[1], s, feed)


def generate(doc: SvgDoc, settings: dict[str, PathSettings], job: Job, name: str = "") -> CamResult:
    res = CamResult()
    shapes = [s for s in doc.shapes if settings.get(s.sid, PathSettings(mode="off")).mode != "off"]
    if not shapes:
        res.warnings.append("No path is enabled: set Line or Fill on at least one path")
        return res
    enabled = {s.sid for s in shapes}
    tf = _transform_fn(doc, enabled, job)
    tasks: list[Task] = []
    for seq, sh in enumerate(shapes):
        st = settings[sh.sid]
        if st.does_fill:
            polys = shape_polygons(sh)
            if not polys:
                res.warnings.append(f"{sh.label}: Fill needs a closed shape (open paths are skipped)")
            else:
                strokes = hatch(polys, st.interval, st.angle, st.bidir)
                if st.crosshatch:
                    strokes += hatch(polys, st.interval, st.angle + 90.0, st.bidir)
                if not strokes:
                    res.warnings.append(f"{sh.label}: the fill is empty (shape smaller than the line interval?)")
                else:
                    arr = tf(np.asarray(strokes).reshape(-1, 2)).reshape(-1, 2, 2)
                    tasks.append(Task("fill", sh.sid, arr, settings=st, seq=seq))
        if st.does_line:
            if abs(st.offset) > 1e-6:
                polys = offset_polys(shape_polygons(sh), st.offset)
                rings = [(_from_clip(p), True) for p in polys]
                if any(not sp.closed for sp in sh.subpaths):
                    res.warnings.append(f"{sh.label}: the offset is ignored on open paths")
                    rings += [(sp.points, False) for sp in sh.subpaths if not sp.closed]
            else:
                rings = [(sp.points, sp.closed) for sp in sh.subpaths]
            for pts, closed in rings:
                if len(pts) >= 2:
                    tasks.append(Task("line", sh.sid, tf(np.asarray(pts, float)), closed, settings=st, seq=seq))
    if not tasks:
        return res

    # ---- grouping and ordering
    fills = [t for t in tasks if t.kind == "fill"]
    lines = [t for t in tasks if t.kind == "line"]
    cur = np.zeros(2)
    groups: list[list[Task]] = []
    if job.order == "engrave-first":
        if job.optimize and len(fills) > 1:
            fills = _nn_order(fills, cur)
        if fills:
            groups.append(fills)
        if lines:
            if job.inner_first and len(lines) <= 800:
                dep = _depths(lines)
                ordered: list[Task] = []
                c = fills[-1].points[-1, 1] if fills else cur
                for d in sorted(set(dep), reverse=True):
                    lvl = [t for t, dd in zip(lines, dep) if dd == d]
                    lvl = _nn_order(lvl, c) if job.optimize else lvl
                    if lvl:
                        c = lvl[-1].points[-1] if not lvl[-1].closed else lvl[-1].points[0]
                    ordered += lvl
                lines = ordered
            else:
                if job.inner_first:
                    res.warnings.append("Too many paths for 'inner contours first': using the nearest-neighbour order")
                if job.optimize:
                    lines = _nn_order(lines, cur)
            groups.append(lines)
    else:
        for t in tasks:
            groups.append([t])

    w = Writer(job)
    for grp in groups:
        for p in range(max(t.passes for t in grp)):
            for t in grp:
                if p >= t.passes:
                    continue
                s = w.power_s(t.power)
                if t.power > 100:
                    res.warnings.append(f"{t.sid}: power above 100 % limited to 100 %")
                if t.kind == "fill":
                    for seg in t.points:
                        w.burn_stroke(seg[0], seg[1], s, t.speed, t.settings.overscan)
                else:
                    w.burn_path(t.points, t.closed, s, t.speed)
    res.n_paths = len(tasks)

    head = [f"; LaserDeck - {name or job.name or 'untitled'}",
            f"; {len(enabled)} paths, {len(tasks)} operations, origin: {job.origin}",
            "G21 G90 G94"]
    if job.air_assist:
        head.append("M8")
    head.append(f"{job.laser_cmd} S0")
    tail = ["M5"]
    if job.air_assist:
        tail.append("M9")
    if job.return_home:
        tail.append("G0X0Y0")
    tail.append("M2")
    res.gcode = "\n".join(head + w.lines + tail) + "\n"
    return res
