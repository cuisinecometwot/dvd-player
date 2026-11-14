import base64
import struct

from domain.cdmeta import (
    AlbumInfo,
    TrackInfo,
    album_folder_name,
    blank_album,
    cover_extension,
    ffmetadata,
    flac_tags,
    musicbrainz_disc_id,
    parse_release,
    picture_block,
    track_file_name,
    track_tags,
)


def test_disc_id_matches_reference():
    # Reference TOC and ID from the libdiscid / python-discid test suite (offsets there include the 150 pregap).
    offsets = [150, 17510, 33275, 45910, 57805, 78310, 94650, 109580, 132010, 149160, 165115, 177710, 203325,
               215555, 235590]  # fmt: skip
    disc_id = musicbrainz_disc_id(1, 15, 258725 - 150, [o - 150 for o in offsets])
    assert disc_id == "TqvKjMu7dMliSfmVEBtrL7sBSno-"


def _credit(*names: str) -> list[dict]:
    return [{"name": n, "joinphrase": "、" if i < len(names) - 1 else ""} for i, n in enumerate(names)]


RELEASE = {
    "id": "31d42c4a-8c2f-4915-9152-8f518aba22aa",
    "title": "雨夜 燕 1st Single「理論武装して」",
    "date": "2026-02-25",
    "artist-credit": _credit("初星学園", "雨夜燕"),
    "media": [
        {
            "tracks": [
                {"title": "理論武装して", "artist-credit": _credit("初星学園", "雨夜燕")},
                {"title": "理論武装して [Instrumental]", "artist-credit": _credit("初星学園")},
                {"title": "", "artist-credit": []},
            ]
        }
    ],
}


def test_parse_release():
    info = parse_release(RELEASE, 3)
    assert info.title == "雨夜 燕 1st Single「理論武装して」"
    assert info.artist == "初星学園、雨夜燕"
    assert info.year == "2026"
    assert info.release_id == "31d42c4a-8c2f-4915-9152-8f518aba22aa"
    assert info.track(2) == TrackInfo(2, "理論武装して [Instrumental]", "初星学園")
    # Missing titles and credits fall back to a numbered title and the album artist.
    assert info.track(3) == TrackInfo(3, "Track 03", "初星学園、雨夜燕")


def test_parse_release_rejects_track_count_mismatch():
    assert parse_release(RELEASE, 6) is None


def test_names():
    info = AlbumInfo("A/B: Live", "X", tracks=(TrackInfo(1, 'Who? "Me"'),))
    assert album_folder_name(info) == "A_B_ Live"
    assert track_file_name(info, 1) == "01 - Who_ _Me_.flac"
    assert track_file_name(info, 7) == "07 - Track 07.flac"
    assert album_folder_name(AlbumInfo("..", "X")) == "Unknown Album"


def test_blank_album():
    info = blank_album([1, 2])
    assert info.title == "Unknown Album" and [t.title for t in info.tracks] == ["Track 01", "Track 02"]


def test_flac_tags():
    info = AlbumInfo("Album", "Band", "2026", (TrackInfo(2, "Song", "Singer"),))
    args = flac_tags(info, 2, 6)
    tags = dict(a.split("=", 1) for a in args[1::2])
    assert set(args[0::2]) == {"-metadata"}
    assert tags == {
        "title": "Song",
        "artist": "Singer",
        "album": "Album",
        "album_artist": "Band",
        "date": "2026",
        "track": "2",
        "TRACKTOTAL": "6",
    }
    # Empty values (no year) are left out rather than written as blank tags.
    assert not any(a.startswith("date=") for a in flac_tags(AlbumInfo("A", "B"), 1, 1))


def test_cover_extension():
    assert cover_extension("Front.JPEG") == ".jpg"
    assert cover_extension("a.b.png") == ".png"
    assert cover_extension("scan.webp") is None
    assert cover_extension("noext") is None


def test_ffmetadata_escapes_special_characters():
    text = ffmetadata({"title": "a=b;c#d\\e", "LYRICS": "one\ntwo"})
    assert text == ";FFMETADATA1\ntitle=a\\=b\;c\\#d\\\\e\nLYRICS=one\\\ntwo\n"


def test_picture_block_layout():
    raw = base64.b64decode(picture_block(b"JPEGDATA", "image/jpeg", 672, 1200))
    kind, mime_len = struct.unpack(">II", raw[:8])
    assert (kind, raw[8 : 8 + mime_len]) == (3, b"image/jpeg")
    rest = raw[8 + mime_len :]
    (desc_len,) = struct.unpack(">I", rest[:4])
    width, height, depth, colors, size = struct.unpack(">IIIII", rest[4 + desc_len : 24 + desc_len])
    assert (width, height, size) == (672, 1200, 8) and rest[24 + desc_len :] == b"JPEGDATA"


def test_track_tags_and_opus_name():
    info = AlbumInfo("Album", "Band", tracks=(TrackInfo(1, "Song"),))
    assert track_tags(info, 1, 6) == {
        "title": "Song", "artist": "Band", "album": "Album", "album_artist": "Band", "track": "1", "TRACKTOTAL": "6",
    }  # fmt: skip
    assert track_file_name(info, 1, ".opus") == "01 - Song.opus"
