"""Names and lyrics for the inserted audio CD: cache first, then the disc's CD-Text, then the network.

Everything slow runs on worker threads; results are applied on the GTK main loop, and results that arrive
after the disc changed are dropped (generation counter).
"""

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from gi.repository import GLib

from domain.cdcache import (
    SOURCE_CD_TEXT,
    SOURCE_EDITED,
    SOURCE_MUSICBRAINZ,
    SOURCE_NONE,
    CdRecord,
    LyricsEntry,
    known_lyrics,
    lyrics_needed,
    needs_album_lookup,
    record_from_dict,
    record_to_dict,
)
from domain.cdmeta import AlbumInfo, blank_album, musicbrainz_disc_id
from domain.cdtext import parse_cd_info
from domain.lyrics import Lyrics
from domain.rip import CdTrack
from dvdplayer import lrclib, musicbrainz
from dvdplayer.drive import Drive


def cache_dir() -> Path:
    return Path(GLib.get_user_cache_dir()) / "dvd-player" / "cd"


def _load(disc_id: str) -> CdRecord | None:
    try:
        return record_from_dict(json.loads((cache_dir() / f"{disc_id}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _save(disc_id: str, record: CdRecord) -> None:
    path = cache_dir() / f"{disc_id}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record_to_dict(record), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that can't be written just means looking things up again next time


class CdInfo:
    """album is always usable (placeholders until something better arrives); on_change fires on updates."""

    def __init__(self, on_change: Callable[[], None]):
        self._on_change = on_change
        self._gen = 0
        self._lyrics_gen = 0  # a newer fetch (titles edited) supersedes an older one
        self.disc_id = ""
        self.album: AlbumInfo | None = None
        self.source = ""  # "" while resolving, else a domain.cdcache SOURCE_*
        self._record: CdRecord | None = None
        self._durations: dict[int, float] = {}

    @property
    def resolving(self) -> bool:
        return self.album is not None and self.source == ""

    @property
    def named(self) -> bool:
        """True when album holds real names rather than "Track 01" placeholders."""
        return self.source in (SOURCE_EDITED, SOURCE_CD_TEXT, SOURCE_MUSICBRAINZ)

    def load(self, drive: Drive, tracks: list[CdTrack], leadout: int, spans: list[tuple[int, int, int]]) -> None:
        self.clear()
        gen = self._gen
        self.disc_id = musicbrainz_disc_id(tracks[0].number, tracks[-1].number, leadout, [t.start_lba for t in tracks])
        self._durations = {n: (end - start) / 1000 for n, start, end in spans}
        numbers = [n for n, _, _ in spans]
        self.album = blank_album(numbers)
        record = _load(self.disc_id)
        if record and record.album and [t.number for t in record.album.tracks] != numbers:
            record = None  # disc-ID collision or a hand-edited file: start over
        if record and record.album:
            self._record, self.album, self.source = record, record.album, record.source
        if not needs_album_lookup(record, time.time()):
            self.source = self.source or SOURCE_NONE
            self._record = record
            self._fetch_lyrics()
            return

        disc_id, barcode = self.disc_id, drive.mcn()

        def work() -> None:
            # The disc's own CD-Text wins over any online database.
            info, source = parse_cd_info(drive.cd_info_report(), numbers), SOURCE_CD_TEXT
            if info is None:
                info, source = musicbrainz.lookup(disc_id, barcode, len(numbers)), SOURCE_MUSICBRAINZ
            GLib.idle_add(self._album_resolved, gen, info, source if info else SOURCE_NONE)

        threading.Thread(target=work, daemon=True).start()

    def clear(self) -> None:
        self._gen += 1
        self.disc_id, self.album, self.source, self._record = "", None, "", None
        self._durations = {}

    def set_album(self, info: AlbumInfo) -> None:
        """The user's edits: remembered for this disc and preferred over every other source."""
        if not self.disc_id:
            return
        self._album_resolved(self._gen, info, SOURCE_EDITED)

    def lyrics(self, number: int) -> Lyrics | None:
        return self.known_lyrics().get(number)

    def known_lyrics(self) -> dict[int, Lyrics | None]:
        return known_lyrics(self._record, self.album) if self.album else {}

    def _album_resolved(self, gen: int, info: AlbumInfo | None, source: str) -> bool:
        if gen != self._gen:
            return False
        old = self._record
        self.source = source
        if info is not None:
            self.album = info
        self._record = CdRecord(info, source, time.time(), old.lyrics if old else {})
        _save(self.disc_id, self._record)
        self._on_change()
        self._fetch_lyrics()
        return False

    def _fetch_lyrics(self) -> None:
        if not self.named:
            return  # searching LRCLIB for "Track 01" would only find strangers' songs
        self._lyrics_gen += 1
        album, gen = self.album, (self._gen, self._lyrics_gen)
        needed = lyrics_needed(self._record, album, time.time())
        if not needed:
            return
        durations = dict(self._durations)

        def work() -> None:
            for number in needed:
                track = album.track(number)
                found = lrclib.fetch(track.title, track.artist or album.artist, durations.get(number, 0))
                GLib.idle_add(self._lyrics_resolved, gen, number, track.title, found)

        threading.Thread(target=work, daemon=True).start()

    def _lyrics_resolved(self, gen: tuple[int, int], number: int, title: str, found: Lyrics | None) -> bool:
        if gen != (self._gen, self._lyrics_gen) or self._record is None:
            return False
        entries = {**self._record.lyrics, number: LyricsEntry(title, found, time.time())}
        self._record = CdRecord(self._record.album, self._record.source, self._record.checked, entries)
        _save(self.disc_id, self._record)
        self._on_change()
        return False
