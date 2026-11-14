from domain.cdcache import (
    RETRY_AFTER_S,
    CdRecord,
    LyricsEntry,
    known_lyrics,
    lyrics_needed,
    needs_album_lookup,
    record_from_dict,
    record_to_dict,
)
from domain.cdmeta import AlbumInfo, TrackInfo
from domain.lyrics import Lyrics

ALBUM = AlbumInfo(
    "雨夜 燕 1st Single", "初星学園", "2026",
    (TrackInfo(1, "理論武装して"), TrackInfo(2, "初"), TrackInfo(3, "理論武装して [Instrumental]")),
)  # fmt: skip
NOW = 1_800_000_000.0


def test_album_lookup_only_when_unknown_or_stale_miss():
    assert needs_album_lookup(None, NOW)
    assert not needs_album_lookup(CdRecord(ALBUM, "musicbrainz", NOW - 10 * RETRY_AFTER_S), NOW)
    assert not needs_album_lookup(CdRecord(None, "none", NOW - 60), NOW)
    assert needs_album_lookup(CdRecord(None, "none", NOW - RETRY_AFTER_S - 1), NOW)


def test_lyrics_needed_and_known():
    record = CdRecord(
        ALBUM,
        "musicbrainz",
        NOW,
        {
            1: LyricsEntry("理論武装して", Lyrics("a", "[00:01.00]a"), NOW),
            2: LyricsEntry("初", None, NOW - 60),
        },
    )
    assert lyrics_needed(record, ALBUM, NOW) == []  # instrumental never needs lyrics
    assert lyrics_needed(record, ALBUM, NOW + RETRY_AFTER_S) == [2]  # stale miss
    renamed = ALBUM.with_tracks([TrackInfo(1, "Renamed"), *ALBUM.tracks[1:]])
    assert lyrics_needed(record, renamed, NOW) == [1]
    assert known_lyrics(record, ALBUM) == {1: Lyrics("a", "[00:01.00]a"), 2: None, 3: None}
    assert known_lyrics(record, renamed) == {2: None, 3: None}
    assert lyrics_needed(None, ALBUM, NOW) == [1, 2]


def test_round_trip_and_bad_data():
    record = CdRecord(
        ALBUM, "edited", NOW, {1: LyricsEntry("理論武装して", Lyrics("a", ""), NOW), 2: LyricsEntry("初", None, NOW)}
    )
    assert record_from_dict(record_to_dict(record)) == record
    assert record_from_dict(record_to_dict(CdRecord(None, "none", NOW))) == CdRecord(None, "none", NOW)
    assert record_from_dict({"version": 999}) is None
    assert record_from_dict({"version": 1, "album": {"title": "x"}}) is None
