"""Log file in the temp folder, for debugging. Logs older than a day are deleted at startup.

DVD_PLAYER_LOG_DAYS changes how many days are kept (e.g. 7; 0 keeps only the current run).
"""

import logging
import logging.handlers
import os
import platform
import sys
import tempfile
import threading
import time
from pathlib import Path

from domain.housekeeping import expired, log_retention_days

LOG_DAYS_ENV = "DVD_PLAYER_LOG_DAYS"
MAX_BYTES = 5 * 2**20  # per file; one rotated copy is kept, so a run never exceeds ~10 MB
FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"

log = logging.getLogger(__name__)
_current: Path | None = None


def log_dir() -> Path:
    # /tmp is shared between users on Linux, so the folder name carries the user id.
    name = "dvd-player-logs" if sys.platform == "win32" else f"dvd-player-logs-{os.getuid()}"
    return Path(tempfile.gettempdir()) / name


def current_log() -> Path | None:
    return _current


def old_logs() -> list[Path]:
    """Every log file except the ones this run is writing."""
    stem = _current.name if _current else None
    try:
        return [p for p in log_dir().glob("*.log*") if p.is_file() and (stem is None or not p.name.startswith(stem))]
    except OSError:
        return []


def _purge(directory: Path, keep_days: float) -> None:
    try:
        entries = [(p.name, p.stat().st_mtime) for p in directory.glob("*.log*") if p.is_file()]
    except OSError:
        return
    for name in expired(entries, time.time(), keep_days):
        try:
            (directory / name).unlink()
        except OSError:
            pass


def setup(ui: str) -> Path | None:
    """Start logging for this run; returns the log file (None if the temp folder isn't writable)."""
    global _current
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if sys.stderr is not None:  # a windowed Windows exe has no console
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)
    directory = log_dir()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _purge(directory, log_retention_days(os.environ.get(LOG_DAYS_ENV)))
        path = directory / f"dvd-player-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=1, encoding="utf-8")
    except OSError:
        path = None
    else:
        handler.setFormatter(logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)
        _current = path
    logging.captureWarnings(True)
    _hook_uncaught()
    log.info(
        "DVD Player starting: ui=%s, Python %s, %s %s (%s)",
        ui, platform.python_version(), platform.system(), platform.release(), platform.machine(),
    )  # fmt: skip
    return path


def _hook_uncaught() -> None:
    previous = sys.excepthook

    def on_uncaught(kind, value, tb):
        logging.getLogger("uncaught").critical("Uncaught exception", exc_info=(kind, value, tb))
        if previous is not None:
            previous(kind, value, tb)

    def on_thread_uncaught(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        logging.getLogger("uncaught").critical(
            "Uncaught exception in thread %s", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
        )

    sys.excepthook = on_uncaught
    threading.excepthook = on_thread_uncaught
