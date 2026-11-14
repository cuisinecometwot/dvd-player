"""Optical drive access for the running platform."""

import sys

if sys.platform == "win32":
    from dvdplayer.drive_windows import Drive, find_optical_device
else:
    from dvdplayer.drive_linux import Drive, find_optical_device

__all__ = ["Drive", "find_optical_device"]
