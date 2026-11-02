"""Ripping decisions and ffmpeg command construction (no process handling here)."""

import re
from dataclasses import dataclass
from enum import Enum

from domain.disc import DiscKind

CD_FRAMES_PER_SECOND = 75
CD_DATA_TRACK_FLAG = 0x04


class RipKind(Enum):
    DVD_TITLE = "dvd_title"
    AUDIO_TRACKS = "audio_tracks"
    DISC_IMAGE = "disc_image"


PROTECTED_REASON = (
    "This disc is copy-protected (CSS). Ripping it means bypassing that protection, "
    "which Japanese copyright law prohibits even for private copies, so ripping is disabled for it."
)


@dataclass(frozen=True)
class RipOptions:
    kinds: tuple[RipKind, ...]
    blocked_reason: str = ""


def rip_options(kind: DiscKind, is_video_dvd: bool, protected: bool) -> RipOptions:
    if protected:
        return RipOptions((), PROTECTED_REASON)
    if kind is DiscKind.AUDIO:
        return RipOptions((RipKind.AUDIO_TRACKS,))
    if is_video_dvd:
        return RipOptions((RipKind.DVD_TITLE, RipKind.DISC_IMAGE))
    if kind in (DiscKind.DATA, DiscKind.MIXED):
        return RipOptions((RipKind.DISC_IMAGE,))
    return RipOptions((), "Nothing to rip: the disc type isn't recognised.")


@dataclass(frozen=True)
class CdTrack:
    number: int
    start_lba: int
    is_data: bool = False


def audio_track_spans(tracks: list[CdTrack], leadout_lba: int) -> list[tuple[int, int, int]]:
    """(track number, start ms, end ms) for audio tracks, relative to the first audio track."""
    ordered = sorted(tracks, key=lambda t: t.start_lba)
    ends = [t.start_lba for t in ordered[1:]] + [leadout_lba]
    audio = [(t, end) for t, end in zip(ordered, ends, strict=True) if not t.is_data]
    if not audio:
        return []
    origin = audio[0][0].start_lba

    def ms(lba: int) -> int:
        return (lba - origin) * 1000 // CD_FRAMES_PER_SECOND

    return [(t.number, ms(t.start_lba), ms(end)) for t, end in audio]


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_filename(text: str, fallback: str = "disc") -> str:
    cleaned = _UNSAFE.sub("_", text).strip(" .")
    return cleaned[:120] or fallback


def dvd_title_command(device: str, title_number: int, output: str) -> list[str]:
    """Lossless remux of one DVD title (1-based) with all audio and subtitle streams."""
    return [
        *_FFMPEG,
        "-f", "dvdvideo", "-title", str(title_number), "-i", device,
        "-map", "0", "-c", "copy", output,
    ]  # fmt: skip


def audio_track_commands(
    device: str, spans: list[tuple[int, int, int]], output_pattern: str
) -> list[tuple[list[str], int]]:
    """One ffmpeg run per track, so every FLAC gets a correct header; returns (command, duration_ms) pairs.

    output_pattern is formatted with the track number, e.g. "Track {:02d}.flac".
    """
    commands = []
    for number, start_ms, end_ms in spans:
        duration = end_ms - start_ms
        cmd = [
            *_FFMPEG,
            "-f", "libcdio", "-ss", f"{start_ms / 1000:.3f}", "-t", f"{duration / 1000:.3f}", "-i", device,
            "-map", "0:a", "-c:a", "flac", output_pattern.format(number),
        ]  # fmt: skip
        commands.append((cmd, duration))
    return commands


_FFMPEG = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats"]


def parse_progress_line(line: str) -> int | None:
    """ffmpeg -progress emits key=value lines; return output time in ms from out_time_us/out_time_ms."""
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
        return max(0, int(value)) // 1000  # both keys are microseconds despite the name
    return None


def progress_fraction(done: int, total: int) -> float:
    return 0.0 if total <= 0 else min(1.0, max(0.0, done / total))


def eta_text(fraction: float, elapsed_s: float) -> str:
    if fraction <= 0.01 or elapsed_s <= 0:
        return "estimating…"
    remaining = elapsed_s * (1 - fraction) / fraction
    if remaining < 60:
        return "less than a minute left"
    minutes = round(remaining / 60)
    return f"about {minutes} min left"


def unique_name(name: str, exists) -> str:
    """'x.mkv' -> 'x (2).mkv' etc. until exists(name) is False; exists is injected to keep this pure."""
    if not exists(name):
        return name
    stem, dot, ext = name.rpartition(".") if "." in name else (name, "", "")
    n = 2
    while exists(f"{stem} ({n}){dot}{ext}"):
        n += 1
    return f"{stem} ({n}){dot}{ext}"
