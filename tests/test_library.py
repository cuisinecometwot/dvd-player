from domain.library import (
    Probe,
    album_title_from_folder,
    audio_files,
    build_album,
    find_cover,
    local_lyrics,
    lrc_name,
    media_kind,
    name_parts,
    track_number,
)


def test_media_kind():
    assert media_kind("01 - Song.FLAC") == "audio"
    assert media_kind("Movie - Title 1.mkv") == "video"
    assert media_kind("cover.jpg") is None
    assert media_kind("README") is None


def test_name_parts():
    assert name_parts("03 - Campus mode!! [Solo ver.].flac") == (3, "Campus mode!! [Solo ver.]")
    assert name_parts("12. Song.opus") == (12, "Song")
    assert name_parts("7 Song.mp3") == (7, "Song")
    assert name_parts("Song.flac") == (None, "Song")
    assert name_parts("1999.flac") == (None, "1999")


def test_track_number():
    assert track_number("3") == 3
    assert track_number(" 3/12 ") == 3
    assert track_number("") is None
    assert track_number("A1") is None


def test_lrc_name():
    assert lrc_name("01 - Song.flac") == "01 - Song.lrc"


def test_audio_files_sorted_by_number_and_skip_others():
    names = ["10 - J.flac", "cover.jpg", "02 - B.flac", "01 - A.lrc", "01 - A.flac"]
    assert audio_files(names) == ["01 - A.flac", "02 - B.flac", "10 - J.flac"]


def test_find_cover():
    assert find_cover(["01 - A.flac", "Cover.JPG"]) == "Cover.JPG"
    assert find_cover(["folder.png", "cover.jpg"]) == "cover.jpg"
    assert find_cover(["scan.jpg"]) is None


def test_album_title_from_folder():
    assert album_title_from_folder("Album (Opus)") == "Album"
    assert album_title_from_folder("Album") == "Album"


def test_build_album_from_tags():
    common = {"album": "Al", "album_artist": "X"}
    probes = {
        "02 - B_C.flac": Probe(
            {**common, "title": "B/C", "track": "2", "date": "2026-01-02", "artist": "X feat. Y", "lyrics": "la"},
            200_000,
        ),
        "01 - A.flac": Probe({**common, "title": "A", "track": "1/2", "artist": "X"}, 100_000),
    }
    album = build_album("Al (Opus)", probes)
    assert album.numbers == [1, 2]
    assert album.info.title == "Al" and album.info.artist == "X" and album.info.year == "2026"
    assert album.info.track(2).title == "B/C"  # the tag keeps characters the file name can't
    assert album.info.track(2).artist == "X feat. Y"
    assert album.files == {1: "01 - A.flac", 2: "02 - B_C.flac"}
    assert album.durations == {1: 100_000, 2: 200_000}
    assert album.plain_lyrics == {2: "la"}


def test_build_album_without_tags_uses_names_and_folder():
    album = build_album("My Album (Opus)", {"02 - Two.opus": Probe(), "01 - One.opus": Probe()})
    assert album.info.title == "My Album"
    assert album.info.artist == ""
    assert [(t.number, t.title) for t in album.info.tracks] == [(1, "One"), (2, "Two")]


def test_build_album_renumbers_when_numbers_clash_or_are_missing():
    album = build_album("A", {"b.mp3": Probe(), "a.mp3": Probe()})
    assert album.files == {1: "a.mp3", 2: "b.mp3"}
    two_discs = {"1 - x.flac": Probe({"track": "1"}), "1 - y.flac": Probe({"track": "1"})}
    assert build_album("A", two_discs).numbers == [1, 2]


def test_local_lyrics():
    lrc = "[ti:Song]\n[ar:X]\n[00:01.00]one\n[00:02.00]two\n"
    assert local_lyrics("", lrc).synced == lrc
    assert local_lyrics("", lrc).plain == "one\ntwo"  # headers dropped
    assert local_lyrics("tagged", lrc).plain == "tagged"
    assert local_lyrics("only plain", "").synced == ""
    assert local_lyrics("", "") is None
