"""Points the packaged Windows build at its bundled tools; must run before `import vlc`."""

import os
import sys
from pathlib import Path


def configure() -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    app = Path(sys.executable).parent
    tools = app / "bin"  # ffmpeg, ffprobe, cd-paranoia, cd-info and their DLLs
    vlc = app / "vlc"
    os.environ["PATH"] = os.pathsep.join([str(tools), str(vlc), os.environ.get("PATH", "")])
    os.environ["PYTHON_VLC_LIB_PATH"] = str(vlc / "libvlc.dll")
    os.environ["PYTHON_VLC_MODULE_PATH"] = str(vlc / "plugins")
    os.environ.setdefault("VLC_PLUGIN_PATH", str(vlc / "plugins"))
    if vlc.is_dir():
        os.add_dll_directory(str(vlc))
