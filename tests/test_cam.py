import numpy as np
import pytest

from laserdeck import cam
from laserdeck.gcode import BURN, parse_program
from laserdeck.svgimport import parse_svg

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="60mm" viewBox="0 0 100 60">
<rect id="plate" x="5" y="5" width="90" height="50" fill="none" stroke="#f00"/>
<circle id="disc" cx="30" cy="30" r="15" fill="#000"/>
<path id="ring" fill-rule="evenodd" fill="#000" d="M70 30 m-12 0 a12 12 0 1 0 24 0 a12 12 0 1 0 -24 0 M70 30 m-6 0 a6 6 0 1 0 12 0 a6 6 0 1 0 -12 0"/>
<path id="open" d="M10 50 L90 50" fill="none" stroke="#00f"/>
</svg>"""


@pytest.fixture
def doc():
    return parse_svg(SVG)


def settings(doc, **modes):
    out = {s.sid: cam.PathSettings(mode="off") for s in doc.shapes}
    for sid, mode in modes.items():
        out[sid] = cam.PathSettings(mode=mode)
    return out


def test_import_style_and_defaults(doc):
    by = {s.sid: s for s in doc.shapes}
    assert by["disc"].fill == "#000000" and by["disc"].stroke is None
    assert by["plate"].stroke == "#ff0000" and by["plate"].fill is None
    d = {s.sid: cam.default_settings(s).mode for s in doc.shapes}
    assert d == {"plate": "line", "disc": "fill", "ring": "fill", "open": "line"}


def test_nothing_enabled(doc):
    r = cam.generate(doc, settings(doc), cam.Job())
    assert r.gcode == "" and r.warnings


def test_hatch_covers_area_and_respects_holes(doc):
    ring = next(s for s in doc.shapes if s.sid == "ring")
    polys = cam.shape_polygons(ring)
    strokes = cam.hatch(polys, 0.1, 0.0, True)
    covered = sum(abs(s[1][0] - s[0][0]) for s in strokes) * 0.1
    area = np.pi * (12 ** 2 - 6 ** 2)
    assert abs(covered - area) / area < 0.01
    for s in strokes:                       # no stroke may cross the hole
        mid = (s[0] + s[1]) / 2
        assert np.hypot(mid[0] - 70, mid[1] - 30) > 6 - 0.2 or abs(s[1][0] - s[0][0]) < 12


@pytest.mark.parametrize("angle", [0, 30, 90])
def test_hatch_angle(doc, angle):
    disc = next(s for s in doc.shapes if s.sid == "disc")
    strokes = cam.hatch(cam.shape_polygons(disc), 0.2, angle, False)
    covered = sum(float(np.hypot(*(s[1] - s[0]))) for s in strokes) * 0.2
    assert abs(covered - np.pi * 225) / (np.pi * 225) < 0.02
    d = strokes[0][1] - strokes[0][0]
    assert abs(np.degrees(np.arctan2(d[1], d[0])) - angle) < 0.5 or abs(abs(np.degrees(np.arctan2(d[1], d[0])) - angle) - 180) < 0.5


def test_bidirectional_alternates(doc):
    disc = next(s for s in doc.shapes if s.sid == "disc")
    st = cam.hatch(cam.shape_polygons(disc), 1.0, 0.0, True)
    assert st[0][1][0] > st[0][0][0] and st[1][1][0] < st[1][0][0]
    st = cam.hatch(cam.shape_polygons(disc), 1.0, 0.0, False)
    assert all(s[1][0] > s[0][0] for s in st)


def test_gcode_structure_and_power(doc):
    r = cam.generate(doc, settings(doc, plate="line", disc="fill"), cam.Job(s_max=1000))
    lines = r.gcode.splitlines()
    assert lines[2] == "G21 G90 G94" and lines[3] == "M4 S0"
    assert lines[-1] == "M2" and "M5" in lines
    assert "G1X" not in lines[4]
    prog = parse_program(r.gcode)
    assert prog.max_spindle == 300                    # line default 30 % of 1000 (fill 25 % = 250)
    assert all(len(l) <= 79 for l in lines)
    assert not prog.warnings


def test_fill_first_then_lines_and_inner_before_outer():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="50mm" height="50mm" viewBox="0 0 50 50">
    <rect id="outer" x="2" y="2" width="46" height="46" fill="none" stroke="#000"/>
    <rect id="inner" x="15" y="15" width="20" height="20" fill="none" stroke="#000"/>
    <circle id="dot" cx="25" cy="25" r="3" fill="#000"/></svg>"""
    d = parse_svg(svg)
    st = {s.sid: cam.PathSettings(mode="line") for s in d.shapes}
    st["dot"].mode = "fill"
    r = cam.generate(d, st, cam.Job(origin="bottom-left"))
    prog = parse_program(r.gcode)
    kinds = [(int(k), float(p)) for k, p in zip(prog.path.kind, prog.path.power) if k == BURN]
    powers = [p for _, p in kinds]
    assert powers[0] == 250 and powers[-1] == 300     # the fill burns first, lines last
    # the inner rectangle (20 mm side) is cut before the outer one (46 mm side)
    burn = prog.path.kind == BURN
    xs = prog.path.end[burn & (prog.path.power == 300)][:, 0]
    assert len(xs) == 8                               # 4 corners per rectangle
    assert xs[:4].max() - xs[:4].min() == pytest.approx(20, abs=0.1)     # inner first ...
    assert xs[4:].max() - xs[4:].min() == pytest.approx(46, abs=0.1)     # ... outer last


def test_multiple_passes_interleave(doc):
    st = settings(doc, plate="line", open="line")
    st["plate"].line_passes = 2
    st["open"].line_passes = 1
    r = cam.generate(doc, st, cam.Job(optimize=False, inner_first=False))
    # 2 passes over the plate = the closed square is burned twice
    prog = parse_program(r.gcode)
    assert abs(prog.burn_length - (2 * 280 + 80)) < 1.0


@pytest.mark.parametrize("origin,lo,hi", [
    ("bottom-left", (0, 0), (90, 50)), ("top-left", (0, -50), (90, 0)),
    ("center", (-45, -25), (45, 25)), ("page", (5, 5), (95, 55))])
def test_origins(doc, origin, lo, hi):
    r = cam.generate(doc, settings(doc, plate="line"), cam.Job(origin=origin))
    prog = parse_program(r.gcode)
    assert np.allclose(prog.bounds_min[:2], lo, atol=0.02) and np.allclose(prog.bounds_max[:2], hi, atol=0.02)


def test_offset_kerf(doc):
    st = settings(doc, plate="line")
    st["plate"].offset = 0.5
    r = cam.generate(doc, st, cam.Job(origin="page"))
    prog = parse_program(r.gcode)
    assert prog.size[0] == pytest.approx(91.0, abs=0.05) and prog.size[1] == pytest.approx(51.0, abs=0.05)
    st["plate"].offset = -0.5
    prog = parse_program(cam.generate(doc, st, cam.Job(origin="page")).gcode)
    assert prog.size[0] == pytest.approx(89.0, abs=0.05)


def test_overscan_and_crosshatch(doc):
    st = settings(doc, disc="fill")
    st["disc"].overscan = 2.0
    r = cam.generate(doc, st, cam.Job())
    prog = parse_program(r.gcode)
    assert "S0" in r.gcode                            # laser-off run-in / run-out
    assert prog.bounds_max[0] - prog.bounds_min[0] == pytest.approx(30, abs=0.3)   # burn area unchanged
    st["disc"].overscan = 0
    one = parse_program(cam.generate(doc, st, cam.Job()).gcode).burn_length
    st["disc"].crosshatch = True
    two = parse_program(cam.generate(doc, st, cam.Job()).gcode).burn_length
    assert two == pytest.approx(2 * one, rel=0.03)


def test_air_assist_m3_and_safe_travel(doc):
    r = cam.generate(doc, settings(doc, plate="line"), cam.Job(air_assist=True, laser_cmd="M3", safe_travel=True))
    g = r.gcode.splitlines()
    assert "M8" in g and "M9" in g and "M3 S0" in g
    assert any(l.startswith("G0") and "S0" in l for l in g[4:])


def test_open_path_fill_warns(doc):
    r = cam.generate(doc, settings(doc, open="fill"), cam.Job())
    assert any("closed" in w for w in r.warnings)


def test_power_s_mapping():
    w = cam.Writer(cam.Job(s_max=255))
    assert w.power_s(0) == 0 and w.power_s(100) == 255 and w.power_s(0.1) == 1 and w.power_s(50) == 128


@pytest.mark.parametrize("origin,lo,hi", [
    ("bottom-left", (10, 10), (40, 40)), ("top-left", (10, -40), (40, -10)), ("center", (-35, -15), (-5, 15))])
def test_origin_depends_on_the_whole_drawing_not_on_enabled_paths(doc, origin, lo, hi):
    """The disc (x 15..45, y 15..45 on a drawing spanning 5..95 x 5..55) lands at the same coordinates whether the
    other paths are enabled or not."""
    for st in (settings(doc, disc="line"),
               {s_.sid: cam.PathSettings(mode="line") for s_ in doc.shapes}):
        r = cam.generate(doc, st, cam.Job(origin=origin))
        prog = parse_program(r.gcode)
        pts = prog.path.end[prog.path.kind == BURN][:, :2]
        # the disc is the smallest closed outline: pick the burned points inside its expected box
        inside = pts[(pts[:, 0] >= lo[0] - 0.05) & (pts[:, 0] <= hi[0] + 0.05) & (pts[:, 1] >= lo[1] - 0.05) & (pts[:, 1] <= hi[1] + 0.05)]
        assert np.allclose(inside.min(axis=0), lo, atol=0.05) and np.allclose(inside.max(axis=0), hi, atol=0.05)
