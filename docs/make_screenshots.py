"""Regenerates the README images (offscreen):  QT_QPA_PLATFORM=offscreen python3 docs/make_screenshots.py"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from laserdeck import __version__, theme  # noqa: E402
from laserdeck.mainwindow import MainWindow  # noqa: E402
from laserdeck.splash import SplashScreen  # noqa: E402

HERE = os.path.dirname(__file__)
EX = os.path.join(HERE, "..", "examples")


def pump(ms: float) -> None:
    t = time.time()
    while time.time() - t < ms / 1000:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def main() -> None:
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.STYLESHEET)
    f = QFont(app.font())
    f.setPointSize(10)
    app.setFont(f)

    sp = SplashScreen(__version__)
    sp.set_time(2300)
    sp.grab().save(os.path.join(HERE, "splash.png"))

    w = MainWindow()
    w.resize(1480, 940)
    w.show()
    w.set_mode(1)
    w.converter.load_svg(os.path.join(EX, "demo.svg"))
    pump(900)
    conv = w.converter
    conv._select({"disc", "ring"})
    conv._set_field("fill_power", 28.0)
    pump(900)
    w.grab().save(os.path.join(HERE, "converter.png"))
    conv._set_view(1)
    pump(300)
    w.grab().save(os.path.join(HERE, "converter-preview.png"))

    w.set_mode(0)
    i = w.port_combo.findData("simulator")
    w.port_combo.setCurrentIndex(i)
    w.toggle_connection()
    pump(1500)
    conv._send()
    pump(600)
    w.frame_job()
    pump(800)
    w._begin_run(w.program.commands)
    pump(9000)
    w.grab().save(os.path.join(HERE, "sender.png"))
    w._running = False
    w.close()


if __name__ == "__main__":
    main()
