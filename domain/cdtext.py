"""CD-Text (album/track names stored on the disc itself), parsed from libcdio's `cd-info` report."""

import re

from domain.cdmeta import AlbumInfo, TrackInfo

_SECTION = re.compile(r"^CD-TEXT for (?:Disc|Track\s+(\d+)):", re.IGNORECASE)
_FIELD = re.compile(r"^\s+(TITLE|PERFORMER):\s?(.*)$")


def parse_cd_info(report: str, track_numbers: list[int]) -> AlbumInfo | None:
    """AlbumInfo from CD-Text, or None when the disc has none (or no usable titles).

    Discs can hold several language blocks; the first value seen for each field wins.
    """
    sections: dict[int, dict[str, str]] = {}  # 0 = the disc itself
    current: int | None = None
    for line in report.splitlines():
        if m := _SECTION.match(line):
            current = int(m.group(1)) if m.group(1) else 0
            sections.setdefault(current, {})
        elif current is not None and (m := _FIELD.match(line)):
            value = m.group(2).strip()
            if value:
                sections[current].setdefault(m.group(1).upper(), value)
        elif line.startswith("___"):
            current = None  # cd-info's section separator; language headers may sit inside a block

    disc = sections.get(0, {})
    tracks = [
        TrackInfo(n, sections.get(n, {}).get("TITLE") or f"Track {n:02d}", sections.get(n, {}).get("PERFORMER", ""))
        for n in track_numbers
    ]
    if not disc.get("TITLE") and not any(sections.get(n, {}).get("TITLE") for n in track_numbers):
        return None
    return AlbumInfo(
        title=disc.get("TITLE", "Unknown Album"),
        artist=disc.get("PERFORMER", "Unknown Artist"),
        tracks=tuple(tracks),
    )
