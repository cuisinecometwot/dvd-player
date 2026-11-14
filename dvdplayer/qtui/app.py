"""Qt application object: native Windows look, dark like the GTK version, background work via a queued signal."""

import os
import sys

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication

from dvdplayer import mainthread

# VLC normally draws into the video widget's own native window (fast, hardware decoding). Set this to 1 to have
# VLC hand frames to Qt instead, if that ever misbehaves on some machine. Always the case off Windows: Wayland
# has no window handle to give VLC.
FRAMES_ENV = "DVD_PLAYER_QT_FRAMES"


class _Invoker(QObject):
    """Lives on the UI thread; emitting from a worker thread queues the call onto it."""

    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, fn) -> None:
        fn()


def run(device: str | None) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName("DVD Player")
    app.setOrganizationName("dvd-player")
    app.setDesktopFileName("dev.huuduc.DvdPlayer")
    # A video app reads better dark, regardless of the system theme (same as the GTK version).
    if hasattr(Qt, "ColorScheme"):
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    invoker = _Invoker()
    mainthread.install(invoker.call.emit)

    from dvdplayer.qtui.window import MainWindow  # imports vlc, so only after winsetup/applog

    native_video = sys.platform == "win32" and os.environ.get(FRAMES_ENV) != "1"
    window = MainWindow(device, native_video)
    window.show()
    window.start()
    code = app.exec()
    window.shutdown()
    return code
