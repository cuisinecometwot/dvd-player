"""Where the app keeps its own files, and clearing them ("Clear Saved Data"). Standard library only, any UI.

Kept: the per-disc cache (album names incl. the user's edits, covers, lyrics), log files, and temp folders of
rips/albums left behind by a crash. Never touched: rips and anything else the user saved.
"""

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dvdplayer import applog

# Prefixes of the temp folders the app creates with tempfile.mkdtemp (rip work files, covers out of tracks).
TEMP_PREFIXES = ("dvd-player-rip-", "dvd-player-album-")


def cache_root() -> Path:
    """Same place GLib uses on Linux (XDG_CACHE_HOME or ~/.cache); %LOCALAPPDATA% on Windows."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "dvd-player"


def cache_root_display() -> str:
    return str(cache_root()) if sys.platform == "win32" else "~/.cache/dvd-player"


@dataclass(frozen=True)
class Clearable:
    paths: tuple[Path, ...]  # files and folders that would be deleted
    size: int


def _size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    except OSError:
        return 0


def clearable(in_use: set[Path]) -> Clearable:
    """What "Clear Saved Data" would delete. in_use: temp folders of the running rip/album, which stay."""
    paths: list[Path] = []
    if cache_root().is_dir():
        paths.append(cache_root())
    paths += applog.old_logs()
    try:
        for entry in Path(tempfile.gettempdir()).iterdir():
            if entry.name.startswith(TEMP_PREFIXES) and entry.is_dir() and entry not in in_use:
                paths.append(entry)
    except OSError:
        pass
    return Clearable(tuple(paths), sum(_size(p) for p in paths))


def clear(target: Clearable) -> int:
    """Deletes what clearable() found; returns the bytes actually freed."""
    freed = 0
    for path in target.paths:
        size = _size(path)
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            continue  # someone else's temp folder, or a file Windows still has open
        freed += size
    return freed
