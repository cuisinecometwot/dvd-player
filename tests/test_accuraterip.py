import struct
import sys
from array import array

from domain import accuraterip
from domain.accuraterip import (
    SKIPPED_EDGE,
    DiscIds,
    TrackEntry,
    cddb_disc_id,
    checksums,
    disc_ids,
    drive_offset,
    parse_entry,
    parse_offsets,
    summary,
    verify,
)
from domain.rip import CdTrack


def _pcm(words: list[int]) -> bytes:
    data = array("I", words)
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


def _reference(words: list[int], first: bool, last: bool) -> tuple[int, int]:
    """Straight port of the reference C loop (accuraterip-checksum), slow but obviously right."""
    check_from = SKIPPED_EDGE if first else 0
    check_to = len(words) - SKIPPED_EDGE if last else len(words)
    lo = hi = 0
    for multiplier, word in enumerate(words, start=1):
        if check_from <= multiplier <= check_to:
            product = multiplier * word
            lo = (lo + (product & 0xFFFFFFFF)) & 0xFFFFFFFF
            hi = (hi + (product >> 32)) & 0xFFFFFFFF
    return lo, (lo + hi) & 0xFFFFFFFF


def test_checksums_match_the_reference_loop():
    words = [(i * 2654435761) & 0xFFFFFFFF for i in range(4 * 588 * 5)]
    for first, last in [(False, False), (True, False), (False, True), (True, True)]:
        assert checksums(_pcm(words), first, last) == _reference(words, first, last)


def test_checksums_are_the_same_across_chunk_boundaries(monkeypatch):
    words = [(i * 40503 + 7) & 0xFFFFFFFF for i in range(588 * 12)]
    expected = checksums(_pcm(words), True, True)
    monkeypatch.setattr(accuraterip, "_CHUNK", 1000)
    assert checksums(_pcm(words), True, True) == expected == _reference(words, True, True)


def test_checksums_of_a_too_short_edge_track_are_zero():
    assert checksums(_pcm([1] * 100), True, True) == (0, 0)


def _toc():
    return [CdTrack(1, 0), CdTrack(2, 15000), CdTrack(3, 30000)], 45000


def test_disc_ids_for_a_plain_audio_cd():
    tracks, leadout = _toc()
    ids = disc_ids(tracks, leadout)
    assert ids.track_count == 3
    assert ids.id1 == 0 + 15000 + 30000 + 45000
    assert ids.id2 == 1 * 1 + 15000 * 2 + 30000 * 3 + 45000 * 4
    assert ids.cddb == cddb_disc_id(tracks, leadout)


def test_cddb_disc_id_follows_the_freedb_formula():
    tracks, leadout = _toc()
    # Track starts at 2 s, 202 s, 402 s (150-frame lead-in); length 600 s; digit sums 2 + 4 + 6.
    assert cddb_disc_id(tracks, leadout) == (12 << 24) | (600 << 8) | 3


def test_enhanced_cd_ids_ignore_the_data_session():
    tracks = [CdTrack(1, 0), CdTrack(2, 15000), CdTrack(3, 60000, is_data=True)]
    ids = disc_ids(tracks, 80000)
    assert ids.track_count == 2
    assert ids.id1 == 0 + 15000 + (60000 - 11400)
    assert disc_ids([CdTrack(1, 0, is_data=True), CdTrack(2, 20000)], 40000) is None


def test_entry_path_uses_the_last_digits_of_id1():
    ids = DiscIds(3, 0x0001ABCD, 0x00222222, 0x1F02580C)
    assert ids.path == "d/c/b/dBAR-003-0001abcd-00222222-1f02580c.bin"


def _block(ids: DiscIds, rows: list[tuple[int, int]]) -> bytes:
    data = struct.pack("<BIII", len(rows), ids.id1, ids.id2, ids.cddb)
    return data + b"".join(struct.pack("<BII", conf, crc, 0) for conf, crc in rows)


def test_parse_entry_reads_every_pressing():
    ids = DiscIds(2, 1, 2, 3)
    data = _block(ids, [(5, 0xAAAA), (4, 0xBBBB)]) + _block(ids, [(2, 0xCCCC), (2, 0xDDDD)])
    assert parse_entry(data) == [
        [TrackEntry(5, 0xAAAA), TrackEntry(4, 0xBBBB)],
        [TrackEntry(2, 0xCCCC), TrackEntry(2, 0xDDDD)],
    ]
    assert parse_entry(data[:-3]) == [[TrackEntry(5, 0xAAAA), TrackEntry(4, 0xBBBB)]]


def test_verify_takes_the_best_matching_pressing():
    pressings = [[TrackEntry(5, 0x11)], [TrackEntry(9, 0x22)]]
    assert verify(pressings, 0, 0x11, 0x99).confidence == 5
    best = verify(pressings, 0, 0x11, 0x22)
    assert (best.confidence, best.version) == (9, 2)
    miss = verify(pressings, 0, 0x33, 0x44)
    assert miss.in_database and not miss.accurate
    assert not verify([], 0, 1, 2).in_database


def test_summary():
    good, bad = verify([[TrackEntry(3, 1)]], 0, 1, 0), verify([[TrackEntry(3, 1)]], 0, 2, 0)
    assert summary([good, good]) == "All 2 tracks accurately ripped (AccurateRip)"
    assert "1 of 2" in summary([good, bad])
    assert "not in AccurateRip" in summary([verify([], 0, 1, 2)])


_PAGE = """
<tr>
<td bgcolor="#F4F4F4"><font face="Arial" size="2">Panasonic - DVD-RAM UJ8B0AW</font></td>
<td align="center" bgcolor="#F4F4F4"><font face="Arial" size="2">+102</font></td>
<td align="center"><font face="Arial" size="2">174</font></td>
</tr>
<tr>
<td><font face="Arial" size="2">LG Electronics - DVDRAM GP65NB60</font></td>
<td align="center"><font face="Arial" size="2">+6</font></td>
</tr>
<tr>
<td><font face="Arial" size="2">PIONEER  - DVD-RW  DVR-221L</font></td>
<td align="center"><font face="Arial" size="2">+667</font></td>
</tr>
<tr>
<td><font face="Arial" size="2">ACME - BROKEN 1</font></td>
<td align="center"><font face="Arial" size="2"><i>[Purged]</i></font></td>
</tr>
"""


def test_parse_offsets_skips_purged_drives():
    table = parse_offsets(_PAGE)
    assert table == {
        "PANASONIC DVD-RAM UJ8B0AW": 102,
        "LG ELECTRONICS DVDRAM GP65NB60": 6,
        "PIONEER DVD-RW DVR-221L": 667,
    }


def test_drive_offset_maps_vendor_names_and_windows_suffix():
    table = parse_offsets(_PAGE)
    assert drive_offset(table, "MATSHITA DVD-RAM UJ8B0AW") == 102
    assert drive_offset(table, "HL-DT-ST DVDRAM GP65NB60 (E:)") == 6
    assert drive_offset(table, "PIONEER DVD-RW  DVR-221L") == 667
    assert drive_offset(table, "Rebrand DVD-RW DVR-221L") == 667  # same model, one known offset
    assert drive_offset(table, "ACME BROKEN 1") is None
    assert drive_offset(table, "") is None
