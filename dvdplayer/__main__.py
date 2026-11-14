"""Entry point: python -m dvdplayer [--qt | --gtk] [device]

The UI is Qt on Windows and GTK/libadwaita elsewhere; --qt / --gtk pick one explicitly (for development).
The device defaults to the first optical drive found (/dev/sr0 on Linux, D: on Windows).
"""

import sys

from dvdplayer import applog, winsetup


def main() -> int:
    winsetup.configure()  # before anything imports vlc
    args = sys.argv[1:]
    ui = "qt" if sys.platform == "win32" else "gtk"
    if "--qt" in args:
        ui = "qt"
    elif "--gtk" in args:
        ui = "gtk"
    device = next((a for a in args if not a.startswith("-")), None)
    applog.setup(ui)
    if ui == "qt":
        from dvdplayer.qtui.app import run
    else:
        from dvdplayer.gtkui.app import run
    return run(device)


if __name__ == "__main__":
    sys.exit(main())
