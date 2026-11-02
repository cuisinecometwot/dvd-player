"""Background rip jobs: ffmpeg subprocesses and raw disc-image copies, reported back on the GTK main loop."""

import glob
import os
import subprocess
import threading
import time
from collections.abc import Callable

from gi.repository import GLib

from domain.rip import eta_text, parse_progress_line, progress_fraction

ProgressHandler = Callable[[float, str], None]
DoneHandler = Callable[[bool, str], None]
IMAGE_CHUNK = 2048 * 512  # 1 MiB, a whole number of DVD sectors
PROGRESS_INTERVAL_S = 0.25


class RipJob:
    def __init__(self, on_progress: ProgressHandler, on_done: DoneHandler, partial_outputs: list[str]):
        self._on_progress = on_progress
        self._on_done = on_done
        self._partial_outputs = partial_outputs  # files or glob patterns removed on cancel/failure
        self._cancelled = threading.Event()
        self._started = time.monotonic()
        self._last_report = 0.0

    def cancel(self) -> None:
        self._cancelled.set()

    def _report(self, fraction: float, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_report >= PROGRESS_INTERVAL_S:
            self._last_report = now
            text = f"{fraction:.0%} · {eta_text(fraction, now - self._started)}"
            GLib.idle_add(self._on_progress, fraction, text)

    def _finish(self, ok: bool, message: str) -> None:
        if not ok:
            for pattern in self._partial_outputs:
                for path in glob.glob(glob.escape(pattern) if "*" not in pattern else pattern):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        GLib.idle_add(self._on_done, ok, message)


class FfmpegJob(RipJob):
    def start(self, steps: list[tuple[list[str], int]]) -> None:
        """Run (command, duration_ms) steps in order; progress spans all of them."""
        threading.Thread(target=self._run, args=(steps,), daemon=True).start()

    def _run(self, steps: list[tuple[list[str], int]]) -> None:
        total = sum(duration for _, duration in steps)
        done_before = 0
        for command, duration in steps:
            error = self._run_one(command, done_before, total)
            if error is not None:
                self._finish(False, error)
                return
            done_before += duration
        self._report(1.0, force=True)
        self._finish(True, "Rip finished")

    def _run_one(self, command: list[str], done_before: int, total: int) -> str | None:
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as e:
            return f"Couldn't start ffmpeg: {e}"
        for line in proc.stdout:
            if self._cancelled.is_set():
                proc.terminate()
                break
            done_ms = parse_progress_line(line)
            if done_ms is not None:
                self._report(progress_fraction(done_before + done_ms, total))
        proc.wait()
        if self._cancelled.is_set():
            return "Rip cancelled"
        if proc.returncode != 0:
            return "ffmpeg failed: " + (proc.stderr.read().strip().splitlines() or ["unknown error"])[-1]
        return None


class ImageJob(RipJob):
    def start(self, device: str, output: str, total_bytes: int) -> None:
        threading.Thread(target=self._run, args=(device, output, total_bytes), daemon=True).start()

    def _run(self, device: str, output: str, total_bytes: int) -> None:
        copied = 0
        try:
            with open(device, "rb", buffering=0) as src, open(output, "wb") as dst:
                while not self._cancelled.is_set():
                    chunk = src.read(IMAGE_CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
                    copied += len(chunk)
                    self._report(progress_fraction(copied, total_bytes))
        except OSError as e:
            self._finish(False, f"Read failed after {copied // 2**20} MiB: {e.strerror or e}")
            return
        if self._cancelled.is_set():
            self._finish(False, "Rip cancelled")
        else:
            self._report(1.0, force=True)
            self._finish(True, "Rip finished")
