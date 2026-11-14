"""What is remembered per audio CD (keyed by disc ID), and when it is worth asking the network again."""

from dataclasses import dataclass, field

from domain.cdmeta import AlbumInfo, TrackInfo
from domain.lyrics import Lyrics, is_instrumental_title

CACHE_VERSION = 2  # 2: release ID + cover art
RETRY_AFTER_S = 7 * 24 * 3600  # misses are retried weekly: MusicBrainz/LRCLIB keep growing

# Where album info came from, best first. The disc's own CD-Text beats any lookup; the user's edits beat both.
SOURCE_EDITED = "edited"
SOURCE_CD_TEXT = "cd-text"
SOURCE_MUSICBRAINZ = "musicbrainz"
SOURCE_NONE = "none"

COVER_UNKNOWN = ""
COVER_ARCHIVE = "coverartarchive"
COVER_USER = "user"  # picked by the user; never replaced by a lookup
COVER_MISSING = "none"


@dataclass(frozen=True)
class LyricsEntry:
    title: str  # the track title the lookup used; a renamed track gets looked up again
    lyrics: Lyrics | None  # None: looked up, nothing found
    checked: float


@dataclass(frozen=True)
class CdRecord:
    album: AlbumInfo | None
    source: str
    checked: float
    lyrics: dict[int, LyricsEntry] = field(default_factory=dict)
    cover_source: str = COVER_UNKNOWN
    cover_file: str = ""  # file name inside the cache directory
    cover_checked: float = 0.0


def needs_cover_lookup(record: CdRecord | None, now: float) -> bool:
    """Cover art is only found through a MusicBrainz release, so CD-Text/hand-typed albums never look."""
    if record is None or record.album is None or not record.album.release_id:
        return False
    if record.cover_source == COVER_UNKNOWN:
        return True
    return record.cover_source == COVER_MISSING and now - record.cover_checked > RETRY_AFTER_S


def cover_file_name(disc_id: str, extension: str) -> str:
    return f"{disc_id}-cover{extension}"


def needs_album_lookup(record: CdRecord | None, now: float) -> bool:
    if record is None or record.album is None:
        return record is None or now - record.checked > RETRY_AFTER_S
    return False


def lyrics_needed(record: CdRecord | None, album: AlbumInfo, now: float) -> list[int]:
    """Track numbers whose lyrics are unknown, stale misses, or were looked up under an old title."""
    entries = record.lyrics if record else {}
    needed = []
    for track in album.tracks:
        if is_instrumental_title(track.title):
            continue
        entry = entries.get(track.number)
        if entry is None or entry.title != track.title:
            needed.append(track.number)
        elif entry.lyrics is None and now - entry.checked > RETRY_AFTER_S:
            needed.append(track.number)
    return needed


def known_lyrics(record: CdRecord | None, album: AlbumInfo) -> dict[int, Lyrics | None]:
    """Lyrics already settled for the current titles (None = none exist); unsettled tracks are absent."""
    entries = record.lyrics if record else {}
    known: dict[int, Lyrics | None] = {}
    for track in album.tracks:
        entry = entries.get(track.number)
        if is_instrumental_title(track.title):
            known[track.number] = None
        elif entry is not None and entry.title == track.title:
            known[track.number] = entry.lyrics
    return known


def record_to_dict(record: CdRecord) -> dict:
    album = record.album
    return {
        "version": CACHE_VERSION,
        "source": record.source,
        "checked": record.checked,
        "album": None
        if album is None
        else {
            "title": album.title,
            "artist": album.artist,
            "year": album.year,
            "tracks": [{"number": t.number, "title": t.title, "artist": t.artist} for t in album.tracks],
            "release_id": album.release_id,
        },
        "cover": {"source": record.cover_source, "file": record.cover_file, "checked": record.cover_checked},
        "lyrics": {
            str(n): {
                "title": e.title,
                "checked": e.checked,
                "plain": e.lyrics.plain if e.lyrics else None,
                "synced": e.lyrics.synced if e.lyrics else None,
            }
            for n, e in record.lyrics.items()
        },
    }


def record_from_dict(data: dict) -> CdRecord | None:
    """None for anything unreadable or from another cache version: it is only a cache, so just refetch."""
    try:
        if data.get("version") != CACHE_VERSION:
            return None
        raw_album = data.get("album")
        album = None
        if raw_album is not None:
            album = AlbumInfo(
                title=str(raw_album["title"]),
                artist=str(raw_album["artist"]),
                year=str(raw_album.get("year", "")),
                tracks=tuple(
                    TrackInfo(int(t["number"]), str(t["title"]), str(t.get("artist", ""))) for t in raw_album["tracks"]
                ),
                release_id=str(raw_album.get("release_id", "")),
            )
        lyrics = {}
        for n, e in (data.get("lyrics") or {}).items():
            found = e.get("plain") is not None or e.get("synced") is not None
            lyrics[int(n)] = LyricsEntry(
                title=str(e["title"]),
                lyrics=Lyrics(e.get("plain") or "", e.get("synced") or "") if found else None,
                checked=float(e["checked"]),
            )
        cover = data.get("cover") or {}
        return CdRecord(
            album,
            str(data["source"]),
            float(data["checked"]),
            lyrics,
            cover_source=str(cover.get("source", COVER_UNKNOWN)),
            cover_file=str(cover.get("file", "")),
            cover_checked=float(cover.get("checked", 0.0)),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
