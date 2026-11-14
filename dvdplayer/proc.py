"""Subprocess options shared by every external tool call."""

import subprocess
import sys

# A windowed (no-console) Windows exe would otherwise flash a console window for each child process.
NO_WINDOW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
