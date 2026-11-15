"""Text written next to a ripped album: a CUE sheet and a rip log that says how trustworthy the copy is."""

from dataclasses import dataclass

from domain.accuraterip import TrackResult
from domain.cdmeta import AlbumInfo

HTOA_FILE_STEM = "00 - Hidden track"


def _cue_text(text: str) -> str:
    return text.replace('"', "'")  # CUE strings have no escape for quotes


def cue_sheet(
    info: AlbumInfo,
    files: dict[int, str],
    *,
    htoa_file: str = "",
    catalog: str = "",
    cddb_id: str = "",
) -> str:
    """One FILE per track. Gaps between tracks sit at the end of the previous file, as ripped from the TOC.

    A hidden track before track 1 becomes track 1's INDEX 00 in its own file, the usual layout.
    """
    lines = []
    if info.year:
        lines.append(f"REM DATE {info.year}")
    if cddb_id:
        lines.append(f"REM DISCID {cddb_id.upper()}")
    lines.append('REM COMMENT "dvd-player"')
    if catalog:
        lines.append(f"CATALOG {catalog}")
    lines += [f'PERFORMER "{_cue_text(info.artist)}"', f'TITLE "{_cue_text(info.title)}"']
    for i, (number, name) in enumerate(sorted(files.items())):
        track = info.track(number)
        header = [
            f"  TRACK {number:02d} AUDIO",
            f'    TITLE "{_cue_text(track.title)}"',
            f'    PERFORMER "{_cue_text(track.artist or info.artist)}"',
        ]
        if i == 0 and htoa_file:
            lines += [f'FILE "{_cue_text(htoa_file)}" WAVE', *header, "    INDEX 00 00:00:00"]
            lines += [f'FILE "{_cue_text(name)}" WAVE', "    INDEX 01 00:00:00"]
        else:
            lines += [f'FILE "{_cue_text(name)}" WAVE', *header, "    INDEX 01 00:00:00"]
    return "\r\n".join(lines) + "\r\n"  # CRLF: what the classic Windows tools write and expect


@dataclass(frozen=True)
class TrackLog:
    number: int
    file: str
    copy_crc: int
    v1: int
    v2: int
    result: TrackResult | None  # None when AccurateRip couldn't be asked
    reader_notes: str = ""  # cd-paranoia's summary for this track, if it reported anything


def rip_log(
    *,
    when: str,
    drive: str,
    offset: int | None,
    offset_source: str,
    reader: str,
    disc_id: str,
    accuraterip_id: str,
    info: AlbumInfo,
    tracks: list[TrackLog],
    htoa: str,
    verdict: str,
) -> str:
    offset_line = f"{offset:+d} samples ({offset_source})" if offset is not None else f"not corrected ({offset_source})"
    lines = [
        "dvd-player rip log",
        "",
        f"Date:           {when}",
        f"Drive:          {drive}",
        f"Read offset:    {offset_line}",
        f"Reader:         {reader} (secure mode: error correction on, read again on mismatch)",
        f"MusicBrainz ID: {disc_id}",
        f"AccurateRip ID: {accuraterip_id or 'n/a (unsupported disc layout)'}",
        f"Album:          {info.artist} - {info.title}",
        f"Hidden track:   {htoa}",
        "",
        "Track  Copy CRC  AR v1     AR v2     AccurateRip",
    ]
    for t in tracks:
        result = t.result.text() if t.result else "not checked (AccurateRip unreachable)"
        lines.append(f"{t.number:5d}  {t.copy_crc:08X}  {t.v1:08X}  {t.v2:08X}  {result}")
        lines.append(f"       {t.file}")
        if t.reader_notes:
            lines += [f"       {note}" for note in t.reader_notes.splitlines() if note.strip()]
    lines += ["", verdict, ""]
    return "\n".join(lines)
