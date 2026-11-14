import pytest

from domain.disc import DiscKind
from domain.rip import CdTrack
from domain.wincd import (
    copyright_protection_type,
    disc_kind_from_tracks,
    msf_to_lba,
    parse_mcn,
    parse_toc,
)


def _entry(number: int, control: int, m: int, s: int, f: int, adr: int = 1) -> bytes:
    return bytes([0, (adr << 4) | control, number, 0, 0, m, s, f])


def _toc(*entries: bytes, first: int = 1, last: int | None = None) -> bytes:
    last = last if last is not None else len(entries) - 1
    body = b"".join(entries)
    return (len(body) + 2).to_bytes(2, "big") + bytes([first, last]) + body


def test_msf_to_lba_skips_the_pregap():
    assert msf_to_lba(0, 2, 0) == 0
    assert msf_to_lba(1, 0, 0) == 4350


def test_parse_toc_audio_disc():
    raw = _toc(_entry(1, 0, 0, 2, 0), _entry(2, 0, 3, 0, 10), _entry(0xAA, 0, 40, 0, 0))
    tracks, leadout = parse_toc(raw)
    assert tracks == [CdTrack(1, 0), CdTrack(2, msf_to_lba(3, 0, 10))]
    assert leadout == msf_to_lba(40, 0, 0)
    assert disc_kind_from_tracks(tracks) is DiscKind.AUDIO


def test_parse_toc_data_flag_is_in_the_low_nibble():
    raw = _toc(_entry(1, 0x4, 0, 2, 0, adr=0), _entry(2, 0, 10, 0, 0, adr=0x4), _entry(0xAA, 0x4, 50, 0, 0))
    tracks, _ = parse_toc(raw)
    assert [t.is_data for t in tracks] == [True, False]
    assert disc_kind_from_tracks(tracks) is DiscKind.MIXED
    assert disc_kind_from_tracks(tracks[:1]) is DiscKind.DATA
    assert disc_kind_from_tracks([]) is DiscKind.UNKNOWN


@pytest.mark.parametrize("raw", [b"", _toc(_entry(1, 0, 0, 2, 0)), bytes([0, 2, 0, 0])])
def test_parse_toc_rejects_malformed(raw):
    with pytest.raises(ValueError):
        parse_toc(raw)


def test_parse_mcn():
    raw = bytes(8) + b"\x80" + b"4988001234567\0\0" + bytes(0)
    assert parse_mcn(raw) == "4988001234567"
    assert parse_mcn(bytes(8) + b"\x00" + b"4988001234567\0\0") == ""  # Mcval clear
    assert parse_mcn(bytes(8) + b"\x80" + b"0000000000000\0\0") == ""
    assert parse_mcn(b"short") == ""


def test_copyright_protection_type():
    assert copyright_protection_type(bytes([0, 6, 0, 0, 1, 0, 0, 0])) == 1
    with pytest.raises(ValueError):
        copyright_protection_type(b"\0\0")
