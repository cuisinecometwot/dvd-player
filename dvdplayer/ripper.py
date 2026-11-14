"""Background rip jobs: ffmpeg subprocesses and raw disc-image copies, reported back on the GTK main loop."""

import glob
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable

from gi.repository import GLib

from domain.rip import Pipeline, eta_text, parse_progress_line, progress_fraction
from dvdplayer.proc import NO_WINDOW

ProgressHandler = Callable[[float, str], None]
# (pipeline, duration_ms) or (pipeline, duration_ms, weight): weight scales a step's share of the progress bar
# for steps much faster than real time (encoding a file vs. reading the disc).
Step = tuple[Pipeline, int] | tuple[Pipeline, int, float]
# Runs on the worker thread before any ffmpeg starts (e.g. network lookups); gets a status-text callback.
Prepare = Callable[[Callable[[str], None]], list[Step]]
DoneHandler = Callable[[bool, str], None]
IMAGE_CHUNK = 2048 * 512  # 1 MiB, a whole number of DVD sectors
PROGRESS_INTERVAL_S = 0.25
KILL_GRACE_S = 3
log = logging.getLogger(__name__)
# Windows has no SIGPIPE: a reader whose consumer quit just fails its write and exits with an error code.
_SIGPIPE_EXIT = -signal.SIGPIPE if hasattr(signal, "SIGPIPE") else 0


class RipJob:
    def __init__(self, on_progress: ProgressHandler, on_done: DoneHandler, partial_outputs: list[str]):
        self._on_progress = on_progress
        self._on_done = on_done
        self._partial_outputs = partial_outputs  # files or glob patterns removed on cancel/failure
        self._cancelled = threading.Event()
        self._started = time.monotonic()
        self._last_report = 0.0

    def cancel(self) -> None:
        log.info("rip cancel requested")
        self._cancelled.set()

    def _report(self, fraction: float, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_report >= PROGRESS_INTERVAL_S:
            self._last_report = now
            text = f"{fraction:.0%} · {eta_text(fraction, now - self._started)}"
            GLib.idle_add(self._on_progress, fraction, text)

    def _finish(self, ok: bool, message: str) -> None:
        log.log(
            logging.INFO if ok else logging.WARNING,
            "rip %s after %.0f s: %s",
            "done" if ok else "stopped",
            time.monotonic() - self._started,
            message,
        )
        if not ok:
            for pattern in self._partial_outputs:
                for path in glob.glob(glob.escape(pattern) if "*" not in pattern else pattern):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        GLib.idle_add(self._on_done, ok, message)


class FfmpegJob(RipJob):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()

    def start(self, steps: list[Step] | Prepare) -> None:
        """Run (pipeline, duration_ms) steps in order; progress spans all of them."""
        threading.Thread(target=self._run, args=(steps,), daemon=True).start()

    def cancel(self) -> None:
        # Kill directly: a stuck reader prints nothing, so the progress loop may never see the flag.
        super().cancel()
        with self._procs_lock:
            procs = list(self._procs)
        for proc in procs:
            _stop(proc)

    def _run(self, steps: list[Step] | Prepare) -> None:
        if callable(steps):
            steps = steps(lambda text: GLib.idle_add(self._on_progress, 0.0, text))
            if self._cancelled.is_set():
                self._finish(False, "Rip cancelled")
                return
        weighted = [(step[0], step[1], step[2] if len(step) > 2 else 1.0) for step in steps]
        total = sum(duration * weight for _, duration, weight in weighted)
        done_before = 0.0
        for pipeline, duration, weight in weighted:
            error = self._run_one(pipeline, done_before, total, weight)
            if error is not None:
                self._finish(False, error)
                return
            done_before += duration * weight
        self._report(1.0, force=True)
        self._finish(True, "Rip finished")

    def _run_one(self, pipeline: Pipeline, done_before: float, total: float, weight: float) -> str | None:
        procs: list[subprocess.Popen] = []
        stderr_tails: list[tuple[list[str], threading.Thread]] = []
        log.info("run: %s", " | ".join(shlex.join(command) for command in pipeline))
        try:
            with self._procs_lock:
                if self._cancelled.is_set():
                    return "Rip cancelled"
                for i, command in enumerate(pipeline):
                    last = i == len(pipeline) - 1
                    proc = subprocess.Popen(
                        command,
                        stdin=procs[-1].stdout if procs else subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=last,
                        **NO_WINDOW,
                    )
                    if procs:
                        procs[-1].stdout.close()  # only the next process holds the read end now
                    procs.append(proc)
                    stderr_tails.append(_drain(proc.stderr))
                self._procs = procs
        except OSError as e:
            for proc in procs:
                _stop(proc)
            return f"Couldn't start {e.filename or pipeline[0][0]}: {e.strerror or e}"

        ffmpeg = procs[-1]
        furthest = 0
        for line in ffmpeg.stdout:
            done_ms = parse_progress_line(line)
            if done_ms is not None:
                # Never step back: with an attached cover picture ffmpeg's final report says 0.
                furthest = max(furthest, done_ms)
                self._report(progress_fraction(done_before + furthest * weight, total))
        for proc in procs:
            proc.wait()
        with self._procs_lock:
            self._procs = []
        if self._cancelled.is_set():
            return "Rip cancelled"
        for command, proc, (tail, reader) in zip(pipeline, procs, stderr_tails, strict=True):
            # A reader killed by SIGPIPE only means the process after it gave up first.
            if proc.returncode not in (0, _SIGPIPE_EXIT):
                reader.join(1)
                log.warning("%s exited with %s; last output:\n%s", command[0], proc.returncode, "".join(tail))
                detail = tail[-1].strip() if tail else f"exit code {proc.returncode}"
                return f"{command[0]} failed: {detail}"
        return None


def _drain(stream) -> tuple[list[str], threading.Thread]:
    """Read a stderr pipe in the background so a chatty process can't block on a full pipe."""
    tail: list[str] = []

    def pump() -> None:
        for raw in stream:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
            if line.strip():
                tail[:] = [*tail[-4:], line]

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return tail, thread


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL: ffmpeg ignores SIGTERM while its demuxer thread is stuck."""
    if proc.poll() is not None:
        return
    proc.terminate()

    def reap() -> None:
        try:
            proc.wait(KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()

    threading.Thread(target=reap, daemon=True).start()


class ImageJob(RipJob):
    def start(self, device: str, output: str, total_bytes: int) -> None:
        threading.Thread(target=self._run, args=(device, output, total_bytes), daemon=True).start()

    def _run(self, device: str, output: str, total_bytes: int) -> None:
        log.info("copy disc image %s -> %s (%d bytes)", device, output, total_bytes)
        copied = 0
        try:
            with open(device, "rb", buffering=0) as src, open(output, "wb") as dst:
                while not self._cancelled.is_set():
                    # A raw Windows volume errors when read past its end instead of returning EOF.
                    want = min(IMAGE_CHUNK, total_bytes - copied) if total_bytes else IMAGE_CHUNK
                    chunk = src.read(want) if want > 0 else b""
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
