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


Pipeline = list[list[str]]  # commands whose stdout feeds the next one's stdin; the last is ffmpeg


def dvd_title_command(device: str, title_number: int, output: str) -> Pipeline:
    """Lossless remux of one DVD title (1-based) with all audio and subtitle streams."""
    return [[
        *_FFMPEG,
        "-f", "dvdvideo", "-title", str(title_number), "-i", device,
        "-map", "0", "-c", "copy", output,
    ]]  # fmt: skip


def audio_track_commands(
    device: str,
    spans: list[tuple[int, int, int]],
    outputs: dict[int, tuple[str, list[str]]],
    cover: str = "",
    reader: str = "cd-paranoia",
    offset: int = 0,
) -> list[tuple[Pipeline, int]]:
    """One cd-paranoia | ffmpeg pipeline per track, so every FLAC gets a correct header.

    Returns (pipeline, duration_ms) pairs. cd-paranoia does the reading because ffmpeg's libcdio
    demuxer spins forever once a read comes within a fraction of a second of the lead-out,
    i.e. on every disc's last track. outputs maps track number -> (output path, extra ffmpeg args
    such as -metadata tags). cover, if given, is embedded as the FLAC front-cover picture.
    reader is libcdio's cd-paranoia or the original cdparanoia (Debian/Ubuntu); both take the same options.
    offset is the drive's read offset in samples (AccurateRip's convention); spans are track numbers, and
    track 0 is the hidden audio before track 1 (HTOA), which cd-paranoia reads like any other track.
    """
    picture = (
        ["-i", cover, "-map", "0:a", "-map", "1:v", "-c:v", "copy", "-disposition:v", "attached_pic",
         "-metadata:s:v", "comment=Cover (front)"]
        if cover else []
    )  # fmt: skip
    return [
        (
            [
                [reader, "--force-cdrom-device", device, *_offset_args(offset), "--quiet",
                 "--output-wav", str(number) if number == 0 else f"{number}-{number}", "-"],
                [*_FFMPEG, "-f", "wav", "-i", "pipe:0", *picture, "-c:a", "flac", *outputs[number][1],
                 outputs[number][0]],
            ],
            end_ms - start_ms,
        )
        for number, start_ms, end_ms in spans
    ]  # fmt: skip


def _offset_args(offset: int) -> list[str]:
    # Full paranoia (error correction, re-reads) is cd-paranoia's default; only the offset is added.
    return ["--sample-offset", str(offset)] if offset else []


def pcm_command(source: str) -> list[str]:
    """Decode a ripped track back to raw 16-bit stereo PCM on stdout, for checksums."""
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", source,
            "-map", "0:a", "-f", "s16le", "-acodec", "pcm_s16le", "-"]  # fmt: skip


def through_file(pipeline: Pipeline, wav: str) -> tuple[Pipeline, Pipeline]:
    """Split a cd-paranoia | ffmpeg pipeline into "read to a WAV file" and "encode that file".

    On Windows cd-paranoia's stdout is in text mode, so piped audio would get CR bytes inserted.
    """
    reader, encoder = pipeline
    i = encoder.index("pipe:0")
    return [[*reader[:-1], wav]], [[*encoder[:i], wav, *encoder[i + 1 :]]]


OPUS_BITRATE_KBPS = 160  # transparent for practically everyone; ~1.2 MB per minute
# Encoding a track to Opus takes ~1/20 of the time reading it off the CD does; weights the progress bar.
OPUS_PROGRESS_WEIGHT = 0.05


def opus_command(source: str, metadata_file: str, output: str, bitrate_kbps: int = OPUS_BITRATE_KBPS) -> Pipeline:
    """Opus copy of a ripped FLAC. Tags (and the cover, as METADATA_BLOCK_PICTURE) come from an ffmetadata
    file because ffmpeg can't mux a picture stream into Ogg and the encoded picture is too long for argv."""
    return [[
        *_FFMPEG, "-i", source, "-i", metadata_file,
        "-map", "0:a", "-map_metadata", "1", "-c:a", "libopus", "-b:a", f"{bitrate_kbps}k", output,
    ]]  # fmt: skip


_FFMPEG = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats"]


def parse_progress_line(line: str) -> int | None:
    """ffmpeg -progress emits key=value lines; return output time in ms from out_time_us/out_time_ms."""
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms") and value.lstrip("-").isdigit():
        return max(0, int(value)) // 1000  # both keys are microseconds despite the name
    return None


def progress_fraction(done: float, total: float) -> float:
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
