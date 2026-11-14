from domain.lyrics import (
    Lyrics,
    base_title,
    is_instrumental_title,
    line_index_at,
    lrc_path_for,
    lrc_text,
    lyrics_tag,
    parse_lrc,
    pick_lyrics,
    search_queries,
    strip_timestamps,
)


def test_instrumental_titles():
    assert is_instrumental_title("理論武装して [Instrumental]")
    assert is_instrumental_title("Song (off vocal ver.)")
    assert is_instrumental_title("Song (カラオケ)")
    assert not is_instrumental_title("Instinct")
    assert not is_instrumental_title("Campus mode!!")


def test_base_title():
    assert base_title("初 [雨夜 燕 Solo ver.]") == "初"
    assert base_title("Song (Live) [Remaster]") == "Song"
    assert base_title("Campus mode!!") == "Campus mode!!"
    assert base_title("(intro)") == "(intro)"  # never strip a title down to nothing


def test_search_queries():
    assert search_queries("初 [Solo ver.]", "初星学園、雨夜燕") == [
        {"track_name": "初 [Solo ver.]", "artist_name": "初星学園"},
        {"track_name": "初", "artist_name": "初星学園"},
        {"track_name": "初"},
    ]
    assert search_queries("Song", "") == [{"track_name": "Song"}, {"track_name": "Song"}]


def _result(title, artist="初星学園", duration=316.0, synced="[00:01.00]a", plain="a", instrumental=False):
    return {
        "trackName": title,
        "artistName": artist,
        "duration": duration,
        "syncedLyrics": synced,
        "plainLyrics": plain,
        "instrumental": instrumental,
    }


def test_pick_prefers_synced_matching_artist_and_close_length():
    results = [
        _result("初", synced=None, plain="plain only"),
        _result("初", artist="Someone Else", synced="[00:01.00]other"),
        _result("初", duration=317.5, synced="[00:01.00]best"),
        _result("初", duration=325.0, synced="[00:01.00]too long"),
    ]
    assert pick_lyrics(results, "初 [雨夜 燕 Solo ver.]", "初星学園、雨夜燕", 316.3).synced == "[00:01.00]best"


def test_pick_rejects_wrong_title_length_and_instrumentals():
    results = [
        _result("別の曲"),
        _result("初", duration=330),
        _result("初", instrumental=True),
        _result("初", synced=None, plain=None),
    ]
    assert pick_lyrics(results, "初", "初星学園", 316) is None


def test_pick_fills_plain_from_synced():
    found = pick_lyrics([_result("Song", synced="[00:01.00]one\n[00:02.50] two", plain=None)], "Song", "", 316)
    assert found == Lyrics(plain="one\ntwo", synced="[00:01.00]one\n[00:02.50] two")


def test_strip_timestamps():
    assert strip_timestamps("[00:21.77]恐れ\n[01:02:03]x\n") == "恐れ\nx"


def test_lrc_output():
    assert lrc_path_for("/m/A/01 - X.flac") == "/m/A/01 - X.lrc"
    text = lrc_text("[00:01.00]a\n", "X", "Y", "Z")
    assert text == "[ti:X]\n[ar:Y]\n[al:Z]\n[00:01.00]a\n"
    assert lyrics_tag(Lyrics("la\nla")) == ["-metadata", "LYRICS=la\nla"]
    assert lyrics_tag(Lyrics("")) == []


def test_parse_lrc_and_line_at():
    lines = parse_lrc("[ti:X]\n[00:21.77]one\n[00:10.5][01:00.00]chorus\nnoise\n[00:30]two")
    assert lines == [(10_500, "chorus"), (21_770, "one"), (30_000, "two"), (60_000, "chorus")]
    assert line_index_at(lines, 0) == -1
    assert line_index_at(lines, 21_770) == 1
    assert line_index_at(lines, 59_999) == 2
    assert line_index_at(lines, 999_999) == 3
