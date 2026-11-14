"""Parsing of the Windows CD/DVD ioctl result structures (ntddcdrm.h, ntddcdvd.h); raw bytes in, facts out."""

from domain.disc import DiscKind
from domain.rip import CD_DATA_TRACK_FLAG, CdTrack

CD_LEADOUT = 0xAA
CD_PREGAP_FRAMES = 150  # MSF addresses count the 2 s lead-in pregap; LBA 0 is at 00:02:00
TOC_HEADER_SIZE = 4
TRACK_DATA_SIZE = 8
TOC_SIZE = TOC_HEADER_SIZE + 100 * TRACK_DATA_SIZE
MCN_BUFFER_SIZE = 24
COPYRIGHT_BUFFER_SIZE = 8


def msf_to_lba(minutes: int, seconds: int, frames: int) -> int:
    return (minutes * 60 + seconds) * 75 + frames - CD_PREGAP_FRAMES


def parse_toc(raw: bytes) -> tuple[list[CdTrack], int]:
    """CDROM_TOC (MSF addresses) -> (tracks, lead-out LBA). Raises ValueError on a malformed buffer."""
    if len(raw) < TOC_HEADER_SIZE:
        raise ValueError("TOC too short")
    first, last = raw[2], raw[3]
    if not 1 <= first <= last <= 99:
        raise ValueError("no tracks in TOC")
    tracks: list[CdTrack] = []
    leadout = 0
    for i in range(last - first + 2):  # every track, then the lead-out entry
        entry = raw[TOC_HEADER_SIZE + i * TRACK_DATA_SIZE : TOC_HEADER_SIZE + (i + 1) * TRACK_DATA_SIZE]
        if len(entry) < TRACK_DATA_SIZE:
            raise ValueError("TOC truncated")
        control = entry[1] & 0x0F  # bit-field order on MSVC: Control is the low nibble, Adr the high one
        number = entry[2]
        lba = msf_to_lba(entry[5], entry[6], entry[7])
        if number == CD_LEADOUT:
            leadout = lba
        else:
            tracks.append(CdTrack(number, lba, bool(control & CD_DATA_TRACK_FLAG)))
    if not leadout:
        raise ValueError("TOC has no lead-out")
    return tracks, leadout


def disc_kind_from_tracks(tracks: list[CdTrack]) -> DiscKind:
    if not tracks:
        return DiscKind.UNKNOWN
    data = sum(t.is_data for t in tracks)
    if data == 0:
        return DiscKind.AUDIO
    return DiscKind.DATA if data == len(tracks) else DiscKind.MIXED


def parse_mcn(raw: bytes) -> str:
    """SUB_Q_MEDIA_CATALOG_NUMBER -> the 13-digit barcode, or "" when the disc has none."""
    if len(raw) < MCN_BUFFER_SIZE or not raw[8] & 0x80:  # Mcval: the catalog number is valid
        return ""
    digits = raw[9:24].split(b"\0", 1)[0].decode("ascii", "replace")
    return digits if digits.isdigit() and digits.strip("0") else ""


def copyright_protection_type(raw: bytes) -> int:
    """DVD_DESCRIPTOR_HEADER + DVD_COPYRIGHT_DESCRIPTOR -> CopyrightProtectionType (1 = CSS)."""
    if len(raw) < TOC_HEADER_SIZE + 1:
        raise ValueError("copyright descriptor too short")
    return raw[4]
