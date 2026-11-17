import os
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from laserdeck import simulator, theme
from laserdeck.mainwindow import MainWindow
from laserdeck.splash import SplashScreen

DEMO = os.path.join(os.path.dirname(__file__), "..", "examples", "demo.svg")


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    a.setStyleSheet(theme.STYLESHEET)
    return a


def pump(app, cond=lambda: False, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


@pytest.fixture
def win(app, monkeypatch):
    orig = simulator.SimulatedSerial.__init__
    monkeypatch.setattr(simulator.SimulatedSerial, "__init__",
                        lambda self, speed=300.0, boot_delay=0.05: orig(self, speed=speed, boot_delay=boot_delay))
    w = MainWindow()
    w.qs.clear()
    w.show()
    yield w
    w._running = False
    w.close()


def load(app, w):
    w.set_mode(1)
    assert w.converter.load_svg(DEMO)
    assert pump(app, lambda: w.converter.program is not None and not w.converter._dirty)
    return w.converter


def test_splash_renders(app):
    s = SplashScreen("0.0.0")
    s.set_time(1500)
    assert not s.grab().isNull()


def test_converter_defaults_from_svg_style(app, win):
    c = load(app, win)
    modes = {sid: st.mode for sid, st in c.settings.items()}
    assert modes == {"plate": "line", "disc": "fill", "ring": "fill", "wave": "line"}
    assert c.table.rowCount() == 4 and c.btn_save.isEnabled() and c.btn_send.isEnabled()


def test_click_in_the_drawing_selects_the_path(app, win):
    c = load(app, win)
    d = c.design
    d.resize(800, 500)
    d.fit()
    disc_centre = (30.0, 30.0)
    d._on_click(*disc_centre, Qt.NoModifier)           # inside the filled disc
    assert c.selected_sids() == ["disc"]
    d._on_click(5.0, 30.0, Qt.NoModifier)              # on the plate outline (left edge)
    assert c.selected_sids() == ["plate"]
    d._on_click(30.0, 30.0, Qt.ControlModifier)        # Ctrl adds
    assert set(c.selected_sids()) == {"plate", "disc"}
    d._on_click(150.0, 150.0, Qt.NoModifier)           # empty space clears
    assert c.selected_sids() == []


def test_field_edit_applies_to_all_selected_and_regenerates(app, win):
    c = load(app, win)
    c._select({"disc", "ring"})
    c.w["fill_power"].setValue(40.0)
    c.w["interval"].setValue(0.2)
    assert c.settings["disc"].fill_power == c.settings["ring"].fill_power == 40.0
    assert c.settings["plate"].line_power == 30.0      # not selected: untouched
    assert pump(app, lambda: c.result and "S400" in c.result.gcode and not c._dirty)
    assert "40 %" in c.table.item(c._row["disc"], 2).text()
    c._set_field("mode", "off")
    assert c.settings["disc"].mode == "off" and c.design.modes["disc"] == "off"
    assert pump(app, lambda: not c._dirty)


def test_all_off_gives_no_gcode(app, win):
    c = load(app, win)
    c._sel_all()
    c._set_field("mode", "off")
    assert pump(app, lambda: not c._dirty and c.result is not None)
    assert c.result.gcode == "" and not c.btn_send.isEnabled()


def test_send_to_laser_loads_the_sender(app, win):
    c = load(app, win)
    c._send()
    assert win.mode == 0
    assert pump(app, lambda: win.program is not None and len(win.program.path) > 0)
    assert win.program.uses_laser and win.editor.toPlainText().startswith("; LaserDeck")


def test_presets_roundtrip(app, win, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    c = load(app, win)
    c._select({"wave"})
    c.w["line_power"].setValue(77.0)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("3mm ply", True)))
    c._preset_save()
    c._select({"plate"})
    c.preset_combo.setCurrentText("3mm ply")
    c._preset_apply()
    assert c.settings["plate"].line_power == 77.0


def test_sender_connect_frame_run(app, win):
    c = load(app, win)
    c._send()
    i = win.port_combo.findData("simulator")
    win.port_combo.setCurrentIndex(i)
    win.toggle_connection()
    assert pump(app, lambda: win.connected and win.conn.settings.get(30))
    assert c.bed == (400.0, 400.0) and c.j_smax.value() == 1000
    assert not win.laser_bar.isVisible()               # simulator has laser mode on
    assert win.act_frame.isEnabled()
    win.frame_job()
    assert pump(app, lambda: abs(win.status.wpos[0] - 0) < 0.01 and win.status.base_state == "Idle", 20)
    sim = win.conn._ser
    assert sim.burned_mm == 0                          # frame power 0 %: laser stays off
    win.run_program()
    assert win._running
    assert pump(app, lambda: not win._running, 90)
    assert win.progress.value() == 1000
    assert sim.burned_mm > 0 and sim.stray_burn_mm == 0


def test_laser_mode_off_shows_warning_bar(app, win):
    i = win.port_combo.findData("simulator")
    win.port_combo.setCurrentIndex(i)
    win.toggle_connection()
    assert pump(app, lambda: win.connected and win.conn.settings.get(30))
    win.send("$32=0")
    assert pump(app, lambda: win.laser_bar.isVisible())
    win.send("$32=1")
    assert pump(app, lambda: not win.laser_bar.isVisible())


def test_hold_to_fire_needs_enable_and_switches_off(app, win):
    i = win.port_combo.findData("simulator")
    win.port_combo.setCurrentIndex(i)
    win.toggle_connection()
    assert pump(app, lambda: win.connected and win.status.base_state == "Idle")
    assert not win.btn_fire.isEnabled()
    win.chk_fire.setChecked(True)
    assert pump(app, lambda: win.btn_fire.isEnabled())
    win._fire_on()
    assert pump(app, lambda: win.conn._ser._spindle == "M3")
    win._fire_off()
    assert pump(app, lambda: win.conn._ser._spindle == "M5")


def test_switch_off_then_on_restores_previous_mode_and_settings(app, win):
    c = load(app, win)
    c._select({"disc"})
    c.w["fill_power"].setValue(33.0)
    c._set_field("mode", "both")
    assert c.chk_enabled.isChecked()
    c.chk_enabled.setChecked(False)
    assert c.settings["disc"].mode == "off" and c.design.modes["disc"] == "off"
    assert not c.w["fill_power"].isEnabled()
    c.chk_enabled.setChecked(True)
    st = c.settings["disc"]
    assert st.mode == "both" and st.fill_power == 33.0 and c.w["fill_power"].isEnabled()


def test_door_state_shows_help_and_resume(app, win):
    from laserdeck.grbl import Status
    win.on_status(Status(state="Door:1", pins="D"))
    assert win.door_bar.isVisibleTo(win) and "Pn:D" in win.door_label.text()
    assert "Door open" in win.badge.text()
    win.on_status(Status(state="Door:0"))
    assert "Resume" in win.door_label.text() and "Door closed" in win.badge.text()
    win.on_status(Status(state="Idle"))
    assert not win.door_bar.isVisibleTo(win)


def test_door_at_connection_triggers_one_automatic_reset(app, win):
    from laserdeck.grbl import Status, ANSI_RE
    assert ANSI_RE.sub("", "\x1b[0;32mI (29091) gpio: GPIO[47]\x1b[0m") == "I (29091) gpio: GPIO[47]"
    i = win.port_combo.findData("simulator")
    win.port_combo.setCurrentIndex(i)
    win.toggle_connection()
    assert pump(app, lambda: win.connected and win.status.base_state == "Idle")
    n = []
    win.conn.reset = lambda: n.append(1)
    win.on_status(Status(state="Door:0"))
    win.on_status(Status(state="Door:0"))
    assert n == [1]                                    # once only; afterwards the banner stays for the user


def test_soft_limits_without_homing_show_fix(app, win):
    win.on_settings({20: 1, 22: 0, 30: 1000, 32: 1})
    assert win.limits_bar.isVisibleTo(win)
    win.on_settings({20: 0, 22: 0, 30: 1000, 32: 1})
    assert not win.limits_bar.isVisibleTo(win)
    win.on_settings({20: 1, 22: 1, 30: 1000, 32: 1})
    assert not win.limits_bar.isVisibleTo(win)


def test_auto_home_after_connecting(app, win, monkeypatch):
    monkeypatch.setitem(simulator.DEFAULT_SETTINGS, 22, 1)
    win.chk_autohome.setChecked(True)
    win.port_combo.setCurrentIndex(win.port_combo.findData("simulator"))
    win.toggle_connection()
    assert pump(app, lambda: win.connected and "$H" in win.conn._ser.log, 10)


def test_auto_home_skipped_when_homing_disabled(app, win):
    win.chk_autohome.setChecked(True)
    win.port_combo.setCurrentIndex(win.port_combo.findData("simulator"))
    win.toggle_connection()                            # the simulator default is $22=0
    assert pump(app, lambda: not win._autohome_pending and win.connected, 10)
    assert "$H" not in win.conn._ser.log
