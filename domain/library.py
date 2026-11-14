"""Albums and videos on disk (typically our own rips): which files make up an album, in what order, named how."""

import re
from collections import Counter
from dataclasses import dataclass, field

from domain.cdmeta import AlbumInfo, TrackInfo
from domain.lyrics import Lyrics, parse_lrc

AUDIO_EXTENSIONS = frozenset({".flac", ".opus", ".ogg", ".oga", ".mp3", ".m4a", ".wav"})
VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".m4v", ".webm", ".avi", ".mov", ".mpg", ".mpeg", ".ts"})
COVER_STEMS = ("cover", "folder", "front", "albumart")
COVER_EXTENSIONS = (".jpg", ".jpeg", ".png")
OPUS_FOLDER_SUFFIX = " (Opus)"  # what the rip names the phone copies' folder

_NUMBERED = re.compile(r"^(\d{1,3})(?:\s*[-._]\s*|\s+)(.+)$")


def _split(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    return (name[:dot], name[dot:].lower()) if dot > 0 else (name, "")


def media_kind(name: str) -> str | None:
    """ "audio", "video" or None for a file name."""
    extension = _split(name)[1]
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return None


def name_parts(name: str) -> tuple[int | None, str]:
    """'03 - Song.flac' -> (3, 'Song'); names without a leading number keep their whole stem."""
    stem = _split(name)[0]
    m = _NUMBERED.match(stem)
    return (int(m.group(1)), m.group(2).strip()) if m else (None, stem)


def track_number(value: str) -> int | None:
    """Tag value '3' or '3/12' -> 3."""
    head = value.strip().partition("/")[0].strip()
    return int(head) if head.isdigit() else None


def lrc_name(name: str) -> str:
    return _split(name)[0] + ".lrc"


def audio_files(names: list[str]) -> list[str]:
    return sorted((n for n in names if media_kind(n) == "audio"), key=lambda n: (name_parts(n)[0] or 0, n.casefold()))


def find_cover(names: list[str]) -> str | None:
    """The folder's cover image (cover.jpg as written by the rip, or the usual alternatives)."""
    by_lower = {n.lower(): n for n in names}
    for stem in COVER_STEMS:
        for extension in COVER_EXTENSIONS:
            if stem + extension in by_lower:
                return by_lower[stem + extension]
    return None


def album_title_from_folder(folder_name: str) -> str:
    if folder_name.endswith(OPUS_FOLDER_SUFFIX):
        return folder_name[: -len(OPUS_FOLDER_SUFFIX)]
    return folder_name


@dataclass(frozen=True)
class Probe:
    """What was read from one audio file: tags (keys lower-cased) and its length."""

    tags: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0


@dataclass(frozen=True)
class LocalAlbum:
    info: AlbumInfo
    files: dict[int, str]  # track number -> file name in the folder
    durations: dict[int, int] = field(default_factory=dict)
    plain_lyrics: dict[int, str] = field(default_factory=dict)  # from the LYRICS tag

    @property
    def numbers(self) -> list[int]:
        return [t.number for t in self.info.tracks]


def _most_common(values: list[str]) -> str:
    present = [v for v in values if v]
    return Counter(present).most_common(1)[0][0] if present else ""


def build_album(folder_name: str, probes: dict[str, Probe]) -> LocalAlbum:
    """Album from its audio files' tags, falling back to the 'NN - Title' names our rips use."""
    names = audio_files(list(probes))

    def order_key(name: str) -> tuple[int, str]:
        tags = probes[name].tags
        number = track_number(tags.get("track", "")) or name_parts(name)[0] or 0
        return number, name.casefold()

    names.sort(key=order_key)
    numbers = [order_key(n)[0] for n in names]
    if 0 in numbers or len(set(numbers)) != len(numbers):
        numbers = list(range(1, len(names) + 1))  # missing or repeated numbers: play in file order

    all_tags = [probes[n].tags for n in names]
    album_artist = _most_common([t.get("album_artist", "") for t in all_tags]) or _most_common(
        [t.get("artist", "") for t in all_tags]
    )
    tracks, files, durations, plain = [], {}, {}, {}
    for number, name in zip(numbers, names, strict=True):
        probe = probes[name]
        title = probe.tags.get("title") or name_parts(name)[1]
        tracks.append(TrackInfo(number, title, probe.tags.get("artist", "")))
        files[number] = name
        durations[number] = probe.duration_ms
        if probe.tags.get("lyrics"):
            plain[number] = probe.tags["lyrics"]
    info = AlbumInfo(
        title=_most_common([t.get("album", "") for t in all_tags]) or album_title_from_folder(folder_name),
        artist=album_artist,
        year=_most_common([t.get("date", "")[:4] for t in all_tags]),
        tracks=tuple(tracks),
    )
    return LocalAlbum(info, files, durations, plain)


def local_lyrics(plain: str, lrc: str) -> Lyrics | None:
    """Lyrics from a track's LYRICS tag and the .lrc beside it (either may be empty)."""
    lines = parse_lrc(lrc)
    synced = lrc if lines else ""
    # parse_lrc drops the [ti:]/[ar:] headers our .lrc files start with.
    plain = plain.strip() or "\n".join(text for _, text in lines).strip()
    return Lyrics(plain=plain, synced=synced) if plain or synced else None
