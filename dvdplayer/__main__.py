"""Entry point: python -m dvdplayer [device]"""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

from dvdplayer.window import MainWindow  # noqa: E402

APP_ID = "dev.huuduc.DvdPlayer"


def main() -> int:
    device = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "/dev/sr0"
    app = Adw.Application(application_id=APP_ID)
    # Video apps read better dark, regardless of the system theme.
    app.connect("startup", lambda _a: Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK))
    app.connect("activate", lambda a: (a.get_active_window() or MainWindow(a, device)).present())
    # Covers exits where the window's close handler never runs (e.g. quitting with a dialog open).
    app.connect("shutdown", lambda a: [w.shutdown() for w in a.get_windows() if isinstance(w, MainWindow)])
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
