"""GTK application object: single instance, forced dark, background work routed through GLib.idle_add."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402

from dvdplayer import mainthread  # noqa: E402

APP_ID = "dev.huuduc.DvdPlayer"


def _run_once(fn) -> bool:
    fn()
    return False  # GLib: don't call again


def run(device: str | None) -> int:
    mainthread.install(lambda fn: GLib.idle_add(_run_once, fn))
    from dvdplayer.gtkui.window import MainWindow  # imports vlc, so only after winsetup/applog

    app = Adw.Application(application_id=APP_ID)
    # Video apps read better dark, regardless of the system theme.
    app.connect("startup", lambda _a: Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK))
    app.connect("activate", lambda a: (a.get_active_window() or MainWindow(a, device)).present())
    # Covers exits where the window's close handler never runs (e.g. quitting with a dialog open).
    app.connect("shutdown", lambda a: [w.shutdown() for w in a.get_windows() if isinstance(w, MainWindow)])
    return app.run([sys.argv[0]])
