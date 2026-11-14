"""The user's Music, Videos and Downloads folders, on Linux (XDG user-dirs) and Windows (known folders)."""

import os
import sys
from pathlib import Path

from domain.userdirs import parse_user_dirs

MUSIC, VIDEOS, DOWNLOADS = "MUSIC", "VIDEOS", "DOWNLOAD"

# Windows KNOWNFOLDERID values for the same folders.
_KNOWN_FOLDERS = {
    MUSIC: "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    VIDEOS: "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    DOWNLOADS: "{374DE290-123F-4565-9164-39C4925E467B}",
}


def special_dir(kind: str) -> Path:
    """The folder, or the home folder when it isn't configured or doesn't exist."""
    found = _windows_known_folder(kind) if sys.platform == "win32" else _xdg_dir(kind)
    return found if found is not None and found.is_dir() else Path.home()


def _xdg_dir(kind: str) -> Path | None:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    try:
        text = (config / "user-dirs.dirs").read_text(encoding="utf-8")
    except OSError:
        return None
    value = parse_user_dirs(text, str(Path.home())).get(kind)
    return Path(value) if value else None


def _windows_known_folder(kind: str) -> Path | None:
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

    guid = GUID()
    ctypes.memmove(guid.data, uuid.UUID(_KNOWN_FOLDERS[kind]).bytes_le, 16)
    path = wintypes.LPWSTR()
    shell32 = ctypes.windll.shell32
    if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
        return None
    try:
        return Path(path.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)
