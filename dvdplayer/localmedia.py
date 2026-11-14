"""Reads an album folder from disk (tags, lengths, lyrics, cover) with ffprobe; meant for a worker thread."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from domain.library import LocalAlbum, Probe, audio_files, build_album, find_cover, local_lyrics, lrc_name
from domain.lyrics import Lyrics

PROBE_TIMEOUT_S = 15


@dataclass(frozen=True)
class OpenedAlbum:
    folder: Path
    album: LocalAlbum
    lyrics: dict[int, Lyrics | None]
    cover: Path | None

    def path(self, number: int) -> Path:
        return self.folder / self.album.files[number]


def probe(path: Path) -> Probe:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
             "format=duration:format_tags:stream_tags", "-of", "json", str(path)],
            capture_output=True,
            timeout=PROBE_TIMEOUT_S,
            check=True,
        ).stdout  # fmt: skip
        data = json.loads(out)
        fmt = data.get("format", {})
        duration = float(fmt.get("duration") or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return Probe()  # unreadable tags: the file name still gives a title
    # Ogg (Opus) keeps its Vorbis comments on the audio stream; FLAC and most others on the container.
    stream = (data.get("streams") or [{}])[0]
    raw = {**(stream.get("tags") or {}), **(fmt.get("tags") or {})}
    tags = {str(k).lower(): str(v) for k, v in raw.items()}
    return Probe(tags, int(duration * 1000))


def _embedded_cover(track: Path, work_dir: Path) -> Path | None:
    """The picture inside a track (the Opus copies have no cover.jpg beside them)."""
    target = work_dir / "cover"
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(track), "-map", "0:v:0", "-c", "copy",
             "-f", "image2", "-frames:v", "1", "-update", "1", str(target)],
            capture_output=True,
            timeout=PROBE_TIMEOUT_S,
            check=True,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return target if target.is_file() and target.stat().st_size else None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_album(folder: Path, work_dir: Path) -> OpenedAlbum | None:
    """None when the folder holds no audio files."""
    try:
        names = [p.name for p in folder.iterdir() if p.is_file()]
    except OSError:
        return None
    tracks = audio_files(names)
    if not tracks:
        return None
    album = build_album(folder.name, {name: probe(folder / name) for name in tracks})
    lyrics = {
        n: local_lyrics(album.plain_lyrics.get(n, ""), _read_text(folder / lrc_name(album.files[n])))
        for n in album.numbers
    }
    cover_name = find_cover(names)
    cover = folder / cover_name if cover_name else _embedded_cover(folder / album.files[album.numbers[0]], work_dir)
    return OpenedAlbum(folder, album, lyrics, cover)
