"""Streaming the generated G-code to the built-in GRBL simulator through the real serial thread."""
import time

import pytest
from PySide6.QtWidgets import QApplication

from laserdeck import cam, simulator
from laserdeck.gcode import parse_program
from laserdeck.grbl import SIM_PORT, GrblConnection
from laserdeck.svgimport import parse_svg

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="30mm" viewBox="0 0 40 30">
<rect id="r" x="2" y="2" width="36" height="26" rx="4" fill="none" stroke="#000"/>
<circle id="c" cx="20" cy="15" r="8" fill="#000"/></svg>"""


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, cond, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def conn(app, monkeypatch):
    orig = simulator.SimulatedSerial.__init__

    def fast(self, speed=400.0, boot_delay=0.05):
        orig(self, speed=speed, boot_delay=boot_delay)

    monkeypatch.setattr(simulator.SimulatedSerial, "__init__", fast)
    c = GrblConnection(SIM_PORT)
    state = {"connected": False, "status": None, "program": [], "acks": [], "settings": None, "console": []}
    c.connectionChanged.connect(lambda ok, m: state.__setitem__("connected", ok))
    c.statusChanged.connect(lambda st: state.__setitem__("status", st))
    c.programState.connect(lambda s: state["program"].append(s))
    c.lineAcked.connect(lambda i, ok, code: state["acks"].append((i, ok, code)))
    c.settingsChanged.connect(lambda d: state.__setitem__("settings", d))
    c.console.connect(lambda t, k: state["console"].append((k, t)))
    c.start()
    assert pump(app, lambda: state["connected"] and state["status"] and state["settings"])
    c.state = state
    yield c
    c.disconnect_port()
    c.wait(3000)


def test_machine_settings_are_a_laser(conn):
    s = conn.settings
    assert s[32] == 1 and s[30] == 1000 and s[130] == 400 and s[131] == 400


def test_generated_job_runs_clean_and_burns_only_during_feed_moves(app, conn):
    doc = parse_svg(SVG)
    st = {"r": cam.PathSettings(mode="line"), "c": cam.PathSettings(mode="fill", interval=0.3)}
    res = cam.generate(doc, st, cam.Job())
    prog = parse_program(res.gcode)
    conn.start_program(prog.commands)
    assert pump(app, lambda: "done" in conn.state["program"], 60), conn.state["program"]
    sim = conn._ser
    assert sim.overflows == 0 and sim.max_rx_used <= 127
    assert len(conn.state["acks"]) == len(prog.commands)
    assert all(ok for _, ok, _ in conn.state["acks"]), [a for a in conn.state["acks"] if not a[1]]
    assert sim.stray_burn_mm == 0.0                  # never fires during G0
    assert sim.burned_mm == pytest.approx(prog.burn_length, rel=0.01)
    assert sim.max_power == 300
    st = conn.state["status"]
    assert abs(st.mpos[0]) < 0.01 and abs(st.mpos[1]) < 0.01   # returned to X0 Y0


def test_laser_mode_off_makes_rapids_burn(app, conn):
    """Why the Sender warns about $32: with laser mode off, G0 keeps the laser on."""
    conn.send_line("$32=0")
    assert pump(app, lambda: conn.settings.get(32) == 0)
    prog = parse_program("G21G90\nM4S500\nG0X20Y0\nG1X30F3000\nM5\n")
    conn.start_program(prog.commands)
    assert pump(app, lambda: "done" in conn.state["program"], 20)
    assert conn._ser.stray_burn_mm == pytest.approx(20, abs=0.5)


def test_safe_travel_option_protects_non_laser_mode(app, conn):
    conn.send_line("$32=0")
    assert pump(app, lambda: conn.settings.get(32) == 0)
    doc = parse_svg(SVG)
    res = cam.generate(doc, {"r": cam.PathSettings(mode="line")}, cam.Job(safe_travel=True))
    prog = parse_program(res.gcode)
    conn.start_program(prog.commands)
    assert pump(app, lambda: "done" in conn.state["program"], 30)
    assert conn._ser.stray_burn_mm == 0.0


def test_stop_aborts_and_keeps_position_valid(app, conn):
    lines = ["G21G90", "M4S100"] + [f"G1X{(i % 2) * 50}Y{i * 0.1:.2f}F600" for i in range(200)]
    prog = parse_program("\n".join(lines))
    conn.start_program(prog.commands)
    assert pump(app, lambda: conn.state["status"].state == "Run")
    conn.stop()
    assert pump(app, lambda: "aborted" in conn.state["program"] and conn.state["status"].state in ("Idle", "Alarm"), 10)
    assert conn._ser._spindle == "M5"
