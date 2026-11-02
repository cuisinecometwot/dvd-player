from domain.ifo import (
    SECTOR,
    TitleEntry,
    parse_dvd_time,
    parse_title_table,
    parse_vts_aspect,
    parse_vts_title_durations,
)


def put(buf: bytearray, off: int, value: int, size: int) -> None:
    buf[off : off + size] = value.to_bytes(size, "big")


def make_vmg(entries: list[tuple[int, int, int]]) -> bytes:
    buf = bytearray(SECTOR * 2)
    put(buf, 0xC4, 1, 4)
    base = SECTOR
    put(buf, base, len(entries), 2)
    for i, (chapters, vts, vts_title) in enumerate(entries):
        e = base + 8 + 12 * i
        put(buf, e + 2, chapters, 2)
        buf[e + 6], buf[e + 7] = vts, vts_title
    return bytes(buf)


def make_vts(times: list[bytes], wide: bool) -> bytes:
    buf = bytearray(SECTOR * 3)
    put(buf, 0x200, (3 if wide else 0) << 10, 2)
    ptt, pgcit = SECTOR, SECTOR * 2
    put(buf, 0xC8, 1, 4)
    put(buf, 0xCC, 2, 4)
    put(buf, ptt, len(times), 2)
    put(buf, pgcit, len(times), 2)
    for i, raw in enumerate(times):
        ptt_off = 8 + 4 * len(times) + 4 * i
        put(buf, ptt + 8 + 4 * i, ptt_off, 4)
        put(buf, ptt + ptt_off, i + 1, 2)  # first chapter uses PGC i+1
        pgc_off = 8 + 8 * len(times) + 16 * i
        put(buf, pgcit + 8 + 8 * i + 4, pgc_off, 4)
        buf[pgcit + pgc_off + 4 : pgcit + pgc_off + 8] = raw
    return bytes(buf)


def test_parse_dvd_time_ntsc_and_pal():
    assert parse_dvd_time(bytes([0x01, 0x23, 0x45, 0xC0])) == (3600 + 23 * 60 + 45) * 1000
    assert parse_dvd_time(bytes([0x00, 0x00, 0x01, 0x40 | 0x12])) == 1000 + 480  # 12 frames @ 25fps


def test_title_table():
    vmg = make_vmg([(7, 1, 1), (1, 2, 1)])
    assert parse_title_table(vmg) == [TitleEntry(1, 7, 1, 1), TitleEntry(2, 1, 2, 1)]


def test_vts_durations_and_aspect():
    vts = make_vts([bytes([0x00, 0x34, 0x42, 0xC0]), bytes([0x00, 0x08, 0x59, 0xC0])], wide=True)
    assert parse_vts_title_durations(vts) == {1: (34 * 60 + 42) * 1000, 2: (8 * 60 + 59) * 1000}
    assert parse_vts_aspect(vts) == 16 / 9
    assert parse_vts_aspect(make_vts([], wide=False)) == 4 / 3
