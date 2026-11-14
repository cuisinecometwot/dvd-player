"""Plan for ripping an audio CD: lyrics, tags, one FLAC per track, optional Opus copies for phones."""

import shutil
import sys
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

from domain.cdmeta import (  # noqa: E402
    AlbumInfo,
    ffmetadata,
    metadata_args,
    picture_block,
    track_file_name,
    track_tags,
)
from domain.lyrics import Lyrics, lrc_path_for, lrc_text  # noqa: E402
from domain.rip import OPUS_PROGRESS_WEIGHT, audio_track_commands, opus_command, through_file  # noqa: E402
from dvdplayer import lrclib, net  # noqa: E402
from dvdplayer.ripper import Prepare, Step  # noqa: E402


def _paranoia() -> str:
    """Fedora ships libcdio's cd-paranoia; Debian/Ubuntu usually only have the original cdparanoia."""
    return "cd-paranoia" if shutil.which("cd-paranoia") or not shutil.which("cdparanoia") else "cdparanoia"


def _opus_picture(cover: str) -> str:
    """METADATA_BLOCK_PICTURE value for the cover, or "" when there is none or it can't be read."""
    if not cover:
        return ""
    try:
        data = Path(cover).read_bytes()
        info = GdkPixbuf.Pixbuf.get_file_info(cover)
    except (OSError, GLib.Error):
        return ""
    fmt, width, height = info if info and info[0] else (None, 0, 0)
    mime = "image/png" if cover.lower().endswith(".png") else "image/jpeg"
    return picture_block(data, mime, width, height)


def plan(
    device: str,
    info: AlbumInfo,
    spans: list[tuple[int, int, int]],
    album_dir: Path,
    cover: str,
    known_lyrics: dict[int, Lyrics | None],
    want_lyrics: bool,
    opus_dir: Path | None,
    work_dir: Path,
) -> tuple[Prepare, list[str]]:
    """(prepare callback for FfmpegJob, files to remove if the rip fails or is cancelled)."""
    total = len(info.tracks) or len(spans)
    flac = {n: str(album_dir / track_file_name(info, n)) for n, _, _ in spans}
    opus = {n: str(opus_dir / track_file_name(info, n, ".opus")) for n, _, _ in spans} if opus_dir else {}
    lrc_dirs = [album_dir] + ([opus_dir] if opus_dir else [])
    partial = [*flac.values(), *opus.values()]
    partial += [lrc_path_for(str(d / track_file_name(info, n))) for d in lrc_dirs for n, _, _ in spans]
    if cover:
        partial.append(cover)

    def prepare(status) -> list[Step]:
        tags = {n: track_tags(info, n, total) for n, _, _ in spans}
        if want_lyrics:
            _add_lyrics(status, info, spans, known_lyrics, tags, lrc_dirs)
        outputs = {n: (flac[n], metadata_args(tags[n])) for n, _, _ in spans}
        rips = audio_track_commands(device, spans, outputs, cover, _paranoia())
        if not opus_dir and sys.platform != "win32":
            return rips
        picture = _opus_picture(cover) if opus_dir else ""
        steps: list[Step] = []
        for (number, _, _), (pipeline, duration) in zip(spans, rips, strict=True):
            if sys.platform == "win32":
                read, encode = through_file(pipeline, str(work_dir / f"{number}.wav"))
                steps += [(read, duration), (encode, duration, OPUS_PROGRESS_WEIGHT)]
            else:
                steps.append((pipeline, duration))
            if not opus_dir:
                continue
            metadata = work_dir / f"{number}.ffmeta"
            opus_tags = {**tags[number], **({"METADATA_BLOCK_PICTURE": picture} if picture else {})}
            metadata.write_text(ffmetadata(opus_tags), encoding="utf-8")
            # Encoded from the fresh FLAC, so the disc is read only once.
            steps.append((opus_command(flac[number], str(metadata), opus[number]), duration, OPUS_PROGRESS_WEIGHT))
        return steps

    return prepare, partial


def _add_lyrics(status, info, spans, known, tags, lrc_dirs) -> None:
    """LYRICS tags from the cache or LRCLIB, plus a synced .lrc next to each output when there is one."""
    offline = False
    for i, (number, start, end) in enumerate(spans, start=1):
        track = info.track(number)
        if number in known:
            lyrics = known[number]  # already cached; no network
        elif offline:
            continue
        else:
            status(f"fetching lyrics {i}/{len(spans)}…")
            try:
                lyrics = lrclib.fetch(track.title, track.artist or info.artist, (end - start) / 1000)
            except net.NetworkError:
                offline = True  # rip without lyrics rather than wait on every track
                continue
        if lyrics is None:
            continue
        if lyrics.plain:
            tags[number]["LYRICS"] = lyrics.plain
        if lyrics.synced:
            text = lrc_text(lyrics.synced, track.title, track.artist or info.artist, info.title)
            for folder in lrc_dirs:
                try:
                    Path(lrc_path_for(str(folder / track_file_name(info, number)))).write_text(text, encoding="utf-8")
                except OSError:
                    pass  # lyrics are a bonus; never fail the rip over them
