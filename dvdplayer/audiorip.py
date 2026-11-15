"""Plan for ripping an audio CD: lyrics, tags, one FLAC per track, optional Opus copies for phones.

Every track is read in cd-paranoia's secure mode with the drive's read offset corrected, then checked against
AccurateRip; the album folder gets a CUE sheet and a rip.log saying how each track came out.
"""

import logging
import shutil
import subprocess
import sys
import time
import zlib
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from domain import accuraterip as ar
from domain.cdmeta import (
    AlbumInfo,
    ffmetadata,
    metadata_args,
    musicbrainz_disc_id,
    picture_block,
    track_file_name,
    track_tags,
)
from domain.imageinfo import image_info
from domain.lyrics import Lyrics, lrc_path_for, lrc_text
from domain.rip import OPUS_PROGRESS_WEIGHT, CdTrack, audio_track_commands, opus_command, pcm_command, through_file
from domain.riplog import HTOA_FILE_STEM, TrackLog, cue_sheet, rip_log
from dvdplayer import accuraterip, lrclib, net
from dvdplayer.proc import NO_WINDOW
from dvdplayer.ripper import Prepare, Step

log = logging.getLogger(__name__)
# Checksumming a track takes a few seconds against a minute or more of reading; weights the progress bar.
VERIFY_PROGRESS_WEIGHT = 0.03
WAV_HEADER_BYTES = 44  # cd-paranoia's plain RIFF header
# Peak level (of 32767) at or below which the audio before track 1 counts as silence, not a hidden track.
HTOA_SILENCE_PEAK = 16
HTOA_TIMEOUT_S = 300


@dataclass
class _Verification:
    """Filled in while the rip runs; turned into rip.log, the CUE sheet and the final message."""

    offset: int | None = None
    offset_source: str = ""
    ids: ar.DiscIds | None = None
    pressings: list[list[ar.TrackEntry]] | None = None  # None: AccurateRip couldn't be reached
    catalog: str = ""
    htoa: str = "none"
    htoa_kept: bool = False
    tracks: list[TrackLog] = field(default_factory=list)

    def results(self) -> list[ar.TrackResult]:
        return [t.result for t in self.tracks if t.result is not None]

    def verdict(self) -> str:
        if self.pressings is None:
            return "Couldn't reach AccurateRip, so the rip isn't verified"
        text = ar.summary(self.results())
        if self.offset is None and any(r.in_database and not r.accurate for r in self.results()):
            text += " (drive read offset unknown)"
        return text


def _paranoia() -> str:
    """Fedora ships libcdio's cd-paranoia; Debian/Ubuntu usually only have the original cdparanoia."""
    return "cd-paranoia" if shutil.which("cd-paranoia") or not shutil.which("cdparanoia") else "cdparanoia"


def _opus_picture(cover: str) -> str:
    """METADATA_BLOCK_PICTURE value for the cover, or "" when there is none or it can't be read."""
    if not cover:
        return ""
    try:
        data = Path(cover).read_bytes()
    except OSError:
        return ""
    info = image_info(data)
    if info is None:
        return ""  # not a JPEG/PNG: players would reject the block anyway
    mime, width, height = info
    return picture_block(data, mime, width, height)


def plan(
    drive,
    toc: tuple[list[CdTrack], int],
    info: AlbumInfo,
    spans: list[tuple[int, int, int]],
    album_dir: Path,
    cover: str,
    known_lyrics: dict[int, Lyrics | None],
    want_lyrics: bool,
    opus_dir: Path | None,
    work_dir: Path,
) -> tuple[Prepare, list[str], Callable[[], str]]:
    """(prepare callback for FfmpegJob, files to remove if the rip fails or is cancelled, final message)."""
    tracks, leadout = toc
    device, reader = drive.device, _paranoia()
    total = len(info.tracks) or len(spans)
    flac = {n: str(album_dir / track_file_name(info, n)) for n, _, _ in spans}
    opus = {n: str(opus_dir / track_file_name(info, n, ".opus")) for n, _, _ in spans} if opus_dir else {}
    lrc_dirs = [album_dir] + ([opus_dir] if opus_dir else [])
    htoa_path = album_dir / f"{HTOA_FILE_STEM}.flac"
    cue_path, log_path = album_dir / f"{album_dir.name}.cue", album_dir / "rip.log"
    partial = [*flac.values(), *opus.values(), str(htoa_path), str(cue_path), str(log_path)]
    partial += [lrc_path_for(str(d / track_file_name(info, n))) for d in lrc_dirs for n, _, _ in spans]
    if cover:
        partial.append(cover)
    state = _Verification()
    first = min(tracks, key=lambda t: t.start_lba) if tracks else None
    # Audio before track 1's index 1 (a "hidden track") only exists when track 1 doesn't start at sector 0.
    htoa_sectors = first.start_lba if first and not first.is_data and spans and first.number == spans[0][0] else 0

    def verify_track(index: int, number: int) -> None:
        try:
            pcm = subprocess.run(pcm_command(flac[number]), capture_output=True, check=True, **NO_WINDOW).stdout
        except (OSError, subprocess.CalledProcessError) as e:
            log.warning("couldn't decode %s for checksums: %s", flac[number], e)
            pcm = b""
        v1, v2 = ar.checksums(pcm, index == 0, index == len(spans) - 1)
        result = None if state.pressings is None else ar.verify(state.pressings, index, v1, v2)
        name = Path(flac[number]).name
        state.tracks.append(TrackLog(number, name, zlib.crc32(pcm), v1, v2, result))
        log.info("track %d: CRC %08X, AR v1 %08X v2 %08X: %s", number, zlib.crc32(pcm), v1, v2,
                 result.text() if result else "not checked")  # fmt: skip

    def rip_htoa(tags: dict[str, str]) -> None:
        """Read the pregap of track 1; keep it only if it holds sound. Never fails the rip: some drives
        can't read before track 1 at all."""
        wav = work_dir / "htoa.wav"
        [(pipeline, _)] = audio_track_commands(device, [(0, 0, 0)], {0: ("", [])}, "", reader, state.offset or 0)
        [read], _ = through_file(pipeline, str(wav))
        try:
            subprocess.run(read, capture_output=True, check=True, timeout=HTOA_TIMEOUT_S, **NO_WINDOW)
            samples = array("h", wav.read_bytes()[WAV_HEADER_BYTES:])
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("hidden track not readable: %s", e)
            state.htoa = "couldn't be read by this drive"
            return
        seconds = len(samples) / 2 / 44100
        if not samples or max(max(samples), -min(samples)) <= HTOA_SILENCE_PEAK:
            state.htoa = f"none ({seconds:.1f} s of silence before track 1, not kept)"
            return
        encode = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
                  "-c:a", "flac", *metadata_args(tags), str(htoa_path)]  # fmt: skip
        try:
            subprocess.run(encode, capture_output=True, check=True, **NO_WINDOW)
        except (OSError, subprocess.CalledProcessError) as e:
            log.warning("hidden track not encoded: %s", e)
            state.htoa = "found but couldn't be saved"
            return
        state.htoa, state.htoa_kept = f"kept as {htoa_path.name} ({seconds:.1f} s)", True

    def write_reports() -> None:
        ids = state.ids
        files = {t.number: t.file for t in state.tracks}
        cue = cue_sheet(info, files, htoa_file=htoa_path.name if state.htoa_kept else "", catalog=state.catalog,
                        cddb_id=f"{ids.cddb:08x}" if ids else "")  # fmt: skip
        text = rip_log(
            when=time.strftime("%Y-%m-%d %H:%M"),
            drive=drive.name,
            offset=state.offset,
            offset_source=state.offset_source,
            reader=reader,
            disc_id=musicbrainz_disc_id(tracks[0].number, tracks[-1].number, leadout, [t.start_lba for t in tracks]),
            accuraterip_id=ids.text if ids else "",
            info=info,
            tracks=state.tracks,
            htoa=state.htoa,
            verdict=state.verdict(),
        )
        try:
            # BOM: Windows tools read a BOM-less CUE as the local code page, which mangles Japanese titles.
            cue_path.write_text(cue, encoding="utf-8-sig", newline="")
            log_path.write_text(text, encoding="utf-8")
        except OSError as e:
            log.warning("couldn't write the rip log/CUE: %s", e)

    def prepare(status) -> list[Step]:
        tags = {n: track_tags(info, n, total) for n, _, _ in spans}
        if want_lyrics:
            _add_lyrics(status, info, spans, known_lyrics, tags, lrc_dirs)
        _look_up_accuraterip(status, drive, tracks, leadout, len(spans), state)
        outputs = {n: (flac[n], metadata_args(tags[n])) for n, _, _ in spans}
        rips = audio_track_commands(device, spans, outputs, cover, reader, state.offset or 0)
        picture = _opus_picture(cover) if opus_dir else ""
        steps: list[Step] = []
        if htoa_sectors:
            htoa_tags = {"title": "Hidden track", "album": info.title, "album_artist": info.artist,
                         "artist": info.artist, "track": "0"}  # fmt: skip
            steps.append((lambda: rip_htoa(htoa_tags), htoa_sectors * 1000 // 75))
        for index, ((number, _, _), (pipeline, duration)) in enumerate(zip(spans, rips, strict=True)):
            if sys.platform == "win32":
                read, encode = through_file(pipeline, str(work_dir / f"{number}.wav"))
                steps += [(read, duration), (encode, duration, OPUS_PROGRESS_WEIGHT)]
            else:
                steps.append((pipeline, duration))
            steps.append((lambda i=index, n=number: verify_track(i, n), duration, VERIFY_PROGRESS_WEIGHT))
            if not opus_dir:
                continue
            metadata = work_dir / f"{number}.ffmeta"
            opus_tags = {**tags[number], **({"METADATA_BLOCK_PICTURE": picture} if picture else {})}
            metadata.write_text(ffmetadata(opus_tags), encoding="utf-8")
            # Encoded from the fresh FLAC, so the disc is read only once.
            steps.append((opus_command(flac[number], str(metadata), opus[number]), duration, OPUS_PROGRESS_WEIGHT))
        steps.append((write_reports, 0))
        return steps

    return prepare, partial, state.verdict


def _look_up_accuraterip(status, drive, tracks, leadout, audio_count, state: _Verification) -> None:
    status("looking up the drive's read offset (AccurateRip)…")
    state.offset, state.offset_source = accuraterip.read_offset(drive.name)
    state.catalog = drive.mcn()
    state.ids = ar.disc_ids(tracks, leadout)
    if state.ids is None or state.ids.track_count != audio_count:
        state.ids, state.pressings = None, []  # a layout AccurateRip doesn't cover: "not in AccurateRip"
        return
    status("looking up this disc's checksums (AccurateRip)…")
    try:
        state.pressings = accuraterip.fetch_entry(state.ids)
    except net.NetworkError:
        state.pressings = None


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
