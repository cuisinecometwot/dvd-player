"""Minimal DVD-Video IFO parsing: title table, title durations and video aspect.

IFO files are unencrypted, so this works even where libvlc can't report lengths.
Layout reference: http://dvd.sourceforge.net/dvdinfo/ifo.html
"""

from dataclasses import dataclass

SECTOR = 2048
_FPS = {1: 25.0, 3: 30000 / 1001}


@dataclass(frozen=True)
class TitleEntry:
    number: int  # 1-based, as on the disc
    chapters: int
    vts: int
    vts_title: int


def _u16(b: bytes, off: int) -> int:
    return int.from_bytes(b[off : off + 2], "big")


def _u32(b: bytes, off: int) -> int:
    return int.from_bytes(b[off : off + 4], "big")


def _bcd(byte: int) -> int:
    return (byte >> 4) * 10 + (byte & 0x0F)


def parse_dvd_time(raw: bytes) -> int:
    """4-byte BCD playback time (hh mm ss ff, frame rate in the top 2 bits) to milliseconds."""
    h, m, s, f = raw[:4]
    fps = _FPS.get(f >> 6)
    frames_ms = round(_bcd(f & 0x3F) * 1000 / fps) if fps else 0
    return ((_bcd(h) * 60 + _bcd(m)) * 60 + _bcd(s)) * 1000 + frames_ms


def parse_title_table(vmg_ifo: bytes) -> list[TitleEntry]:
    base = _u32(vmg_ifo, 0xC4) * SECTOR
    count = _u16(vmg_ifo, base)
    entries = []
    for i in range(count):
        e = base + 8 + 12 * i
        entries.append(TitleEntry(i + 1, _u16(vmg_ifo, e + 2), vmg_ifo[e + 6], vmg_ifo[e + 7]))
    return entries


def parse_vts_title_durations(vts_ifo: bytes) -> dict[int, int]:
    """vts_title number -> duration in ms, from the PGC of each title's first chapter."""
    ptt = _u32(vts_ifo, 0xC8) * SECTOR
    pgcit = _u32(vts_ifo, 0xCC) * SECTOR
    pgc_count = _u16(vts_ifo, pgcit)
    durations = {}
    for t in range(_u16(vts_ifo, ptt)):
        first_ptt = ptt + _u32(vts_ifo, ptt + 8 + 4 * t)
        pgcn = _u16(vts_ifo, first_ptt)
        if not 1 <= pgcn <= pgc_count:
            continue
        pgc = pgcit + _u32(vts_ifo, pgcit + 8 + 8 * (pgcn - 1) + 4)
        durations[t + 1] = parse_dvd_time(vts_ifo[pgc + 4 : pgc + 8])
    return durations


def parse_vts_aspect(vts_ifo: bytes) -> float:
    """Display aspect ratio from VTS video attributes: 16:9 or 4:3."""
    return 16 / 9 if (_u16(vts_ifo, 0x200) >> 10) & 0x3 == 3 else 4 / 3
