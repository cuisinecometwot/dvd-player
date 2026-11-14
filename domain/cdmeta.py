"""Audio CD metadata: MusicBrainz disc ID, release parsing, output names and FLAC tags."""

import base64
import hashlib
import re
import struct
from dataclasses import dataclass, field, replace

from domain.rip import safe_filename

LEAD_IN_FRAMES = 150  # MusicBrainz offsets count the 2 s pregap


@dataclass(frozen=True)
class TrackInfo:
    number: int
    title: str
    artist: str = ""


@dataclass(frozen=True)
class AlbumInfo:
    title: str
    artist: str
    year: str = ""
    tracks: tuple[TrackInfo, ...] = field(default_factory=tuple)
    release_id: str = ""  # MusicBrainz release MBID; the key for cover art

    def track(self, number: int) -> TrackInfo:
        found = next((t for t in self.tracks if t.number == number), None)
        return found or TrackInfo(number, f"Track {number:02d}")

    def with_tracks(self, tracks: list[TrackInfo]) -> "AlbumInfo":
        return replace(self, tracks=tuple(tracks))


def musicbrainz_disc_id(first: int, last: int, leadout_lba: int, track_lbas: list[int]) -> str:
    """Disc ID as defined by MusicBrainz: SHA-1 over the hex TOC, in a URL-safe base64 variant."""
    offsets = [leadout_lba + LEAD_IN_FRAMES] + [lba + LEAD_IN_FRAMES for lba in track_lbas]
    offsets += [0] * (100 - len(offsets))
    toc = f"{first:02X}{last:02X}" + "".join(f"{o:08X}" for o in offsets)
    digest = base64.b64encode(hashlib.sha1(toc.encode("ascii")).digest())
    return digest.decode("ascii").replace("+", ".").replace("/", "_").replace("=", "-")


def credit_text(credits: list[dict]) -> str:
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in credits or [])


def parse_release(release: dict, track_count: int) -> AlbumInfo | None:
    """AlbumInfo from a MusicBrainz release (inc=artist-credits+recordings), or None if no medium fits."""
    medium = next((m for m in release.get("media", []) if len(m.get("tracks", [])) == track_count), None)
    if medium is None:
        return None
    album_artist = credit_text(release.get("artist-credit", []))
    tracks = [
        TrackInfo(
            number=i,
            title=t.get("title") or f"Track {i:02d}",
            artist=credit_text(t.get("artist-credit", [])) or album_artist,
        )
        for i, t in enumerate(medium["tracks"], start=1)
    ]
    return AlbumInfo(
        title=release.get("title", ""),
        artist=album_artist,
        year=(release.get("date") or "")[:4],
        tracks=tuple(tracks),
        release_id=release.get("id", ""),
    )


def blank_album(track_numbers: list[int]) -> AlbumInfo:
    return AlbumInfo(
        title="Unknown Album",
        artist="Unknown Artist",
        tracks=tuple(TrackInfo(n, f"Track {n:02d}") for n in track_numbers),
    )


COVER_EXTENSIONS = {".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png"}


def cover_extension(filename: str) -> str | None:
    """Normalized extension for a usable cover image (what FLAC players accept), else None."""
    dot = filename.rfind(".")
    return COVER_EXTENSIONS.get(filename[dot:].lower()) if dot >= 0 else None


def album_folder_name(info: AlbumInfo) -> str:
    return safe_filename(info.title, "Unknown Album")


def track_file_name(info: AlbumInfo, number: int, extension: str = ".flac") -> str:
    return f"{number:02d} - {safe_filename(info.track(number).title, f'Track {number:02d}')}{extension}"


def track_tags(info: AlbumInfo, number: int, total: int) -> dict[str, str]:
    """Tag name -> value for one track, as ffmpeg names them (empty values left out)."""
    track = info.track(number)
    tags = {
        "title": track.title,
        "artist": track.artist or info.artist,
        "album": info.title,
        "album_artist": info.artist,
        "date": info.year,
        # ffmpeg maps "track" to the Vorbis TRACKNUMBER; players expect the total in TRACKTOTAL, not "n/total".
        "track": str(number),
        "TRACKTOTAL": str(total),
    }
    return {key: value for key, value in tags.items() if value}


def metadata_args(tags: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in tags.items():
        args += ["-metadata", f"{key}={value}"]
    return args


def flac_tags(info: AlbumInfo, number: int, total: int) -> list[str]:
    return metadata_args(track_tags(info, number, total))


_FFMETADATA_SPECIAL = re.compile(r"([=;#\\\n])")


def _ffmetadata_escape(text: str) -> str:
    return _FFMETADATA_SPECIAL.sub(lambda m: "\\" + m.group(1), text)


def ffmetadata(tags: dict[str, str]) -> str:
    """ffmpeg's metadata-file format; used when a value (an embedded picture) is too long for argv."""
    lines = [f"{_ffmetadata_escape(k)}={_ffmetadata_escape(v)}\n" for k, v in tags.items()]
    return ";FFMETADATA1\n" + "".join(lines)


PICTURE_FRONT_COVER = 3


def picture_block(data: bytes, mime: str, width: int, height: int, description: str = "Cover (front)") -> str:
    """Base64 FLAC PICTURE block: how Ogg/Opus files carry cover art (METADATA_BLOCK_PICTURE tag)."""
    mime_b, desc_b = mime.encode("ascii"), description.encode("utf-8")
    block = (
        struct.pack(">II", PICTURE_FRONT_COVER, len(mime_b)) + mime_b
        + struct.pack(">I", len(desc_b)) + desc_b
        + struct.pack(">IIIII", width, height, 24, 0, len(data)) + data
    )  # fmt: skip
    return base64.b64encode(block).decode("ascii")


def edited_album(current: AlbumInfo, title: str, artist: str, year: str, track_titles: dict[int, str]) -> AlbumInfo:
    """The album as typed in the rip dialog; blanks fall back to Unknown Album/Artist and Track NN."""
    artist = artist.strip() or "Unknown Artist"
    tracks = []
    for number, text in track_titles.items():
        old = current.track(number)
        # Per-track artists from MusicBrainz are kept unless they just repeated the album artist.
        track_artist = old.artist if old.artist and old.artist != current.artist else artist
        tracks.append(TrackInfo(number, text.strip() or f"Track {number:02d}", track_artist))
    return AlbumInfo(title=title.strip() or "Unknown Album", artist=artist, year=year.strip(), tracks=tuple(tracks))
