from domain.accuraterip import TrackResult
from domain.cdmeta import AlbumInfo, TrackInfo
from domain.riplog import TrackLog, cue_sheet, rip_log

ALBUM = AlbumInfo('Say "Hi"', "Band", "2001", (TrackInfo(1, "One"), TrackInfo(2, "Two", "Guest")))


def test_cue_sheet_one_file_per_track():
    cue = cue_sheet(ALBUM, {2: "02 - Two.flac", 1: "01 - One.flac"}, catalog="4988001234567", cddb_id="1f02580c")
    lines = cue.split("\r\n")
    assert "REM DISCID 1F02580C" in lines and "CATALOG 4988001234567" in lines
    assert "TITLE \"Say 'Hi'\"" in lines  # CUE can't escape quotes
    assert lines.index('FILE "01 - One.flac" WAVE') < lines.index('FILE "02 - Two.flac" WAVE')
    assert '    PERFORMER "Guest"' in lines and lines.count("    INDEX 01 00:00:00") == 2


def test_cue_sheet_puts_the_hidden_track_in_track_one_pregap():
    cue = cue_sheet(ALBUM, {1: "01 - One.flac"}, htoa_file="00 - Hidden track.flac").split("\r\n")
    i = cue.index('FILE "00 - Hidden track.flac" WAVE')
    assert cue[i + 1] == "  TRACK 01 AUDIO" and cue[i + 4] == "    INDEX 00 00:00:00"
    assert cue[i + 5] == 'FILE "01 - One.flac" WAVE' and cue[i + 6] == "    INDEX 01 00:00:00"


def test_rip_log_lists_every_track():
    text = rip_log(
        when="2026-11-15 10:00", drive="MATSHITA DVD-RAM UJ8B0AW", offset=102, offset_source="AccurateRip",
        reader="cd-paranoia", disc_id="abc", accuraterip_id="", info=ALBUM,
        tracks=[TrackLog(1, "01 - One.flac", 0xDEADBEEF, 1, 2, TrackResult(7, 2, True)),
                TrackLog(2, "02 - Two.flac", 0xFF, 3, 4, None)],
        htoa="none", verdict="1 of 2 tracks verified",
    )  # fmt: skip
    assert "+102 samples" in text and "DEADBEEF" in text and "confidence 7, v2" in text
    assert "AccurateRip unreachable" in text and text.rstrip().endswith("1 of 2 tracks verified")
