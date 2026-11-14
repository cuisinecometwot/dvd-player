"""Names and lyrics for the inserted audio CD: cache first, then the disc's CD-Text, then the network.

Everything slow runs on worker threads; results are applied on the GTK main loop, and results that arrive
after the disc changed are dropped (generation counter).
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from gi.repository import GLib

from domain.cdcache import (
    COVER_ARCHIVE,
    COVER_MISSING,
    COVER_USER,
    SOURCE_CD_TEXT,
    SOURCE_EDITED,
    SOURCE_MUSICBRAINZ,
    SOURCE_NONE,
    CdRecord,
    LyricsEntry,
    cover_file_name,
    known_lyrics,
    lyrics_needed,
    needs_album_lookup,
    needs_cover_lookup,
    record_from_dict,
    record_to_dict,
)
from domain.cdmeta import AlbumInfo, blank_album, cover_extension, musicbrainz_disc_id
from domain.cdtext import parse_cd_info
from domain.lyrics import Lyrics
from domain.rip import CdTrack
from dvdplayer import appdata, coverart, lrclib, musicbrainz, net
from dvdplayer.drive import Drive

log = logging.getLogger(__name__)


def cache_dir() -> Path:
    return appdata.cache_root() / "cd"


def _load(disc_id: str) -> CdRecord | None:
    try:
        return record_from_dict(json.loads((cache_dir() / f"{disc_id}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _write(name: str, data: bytes) -> bool:
    path = cache_dir() / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


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
    """album is always usable (placeholders until something better arrives); on_change fires on updates.

    Progress is exposed through status() so the UI can show that, and what, is being fetched online.
    """

    def __init__(self, on_change: Callable[[], None]):
        self._on_change = on_change
        self._gen = 0
        self._lyrics_gen = 0  # a newer fetch (titles edited) supersedes an older one
        self.disc_id = ""
        self.album: AlbumInfo | None = None
        self.source = ""  # "" while resolving, else a domain.cdcache SOURCE_*
        self._record: CdRecord | None = None
        self._durations: dict[int, float] = {}
        self._drive: Drive | None = None
        self._barcode = ""
        self._tasks: dict[str, str] = {}  # running lookup -> what it is doing, for the UI
        self._failed: set[str] = set()  # services that couldn't be reached during this load
        self._downloaded = False  # anything fetched online during this load (vs. all from the cache)

    @property
    def resolving(self) -> bool:
        return self.album is not None and self.source == ""

    @property
    def named(self) -> bool:
        """True when album holds real names rather than "Track 01" placeholders."""
        return self.source in (SOURCE_EDITED, SOURCE_CD_TEXT, SOURCE_MUSICBRAINZ)

    @property
    def busy(self) -> bool:
        return bool(self._tasks)

    @property
    def failed(self) -> bool:
        return bool(self._failed)

    def load(self, drive: Drive, tracks: list[CdTrack], leadout: int, spans: list[tuple[int, int, int]]) -> None:
        self.clear()
        self._drive = drive
        self.disc_id = musicbrainz_disc_id(tracks[0].number, tracks[-1].number, leadout, [t.start_lba for t in tracks])
        self._durations = {n: (end - start) / 1000 for n, start, end in spans}
        numbers = [n for n, _, _ in spans]
        self.album = blank_album(numbers)
        record = _load(self.disc_id)
        log.info("CD %s: %d tracks, cached: %s", self.disc_id, len(numbers), record.source if record else "nothing")
        if record and record.album and [t.number for t in record.album.tracks] != numbers:
            record = None  # disc-ID collision or a hand-edited file: start over
        if record and record.album:
            self._record, self.album, self.source = record, record.album, record.source
        if needs_album_lookup(record, time.time()):
            self._record = record
            self._lookup_album()
        else:
            self.source = self.source or SOURCE_NONE
            self._record = record
            self._fetch_lyrics()
            self._fetch_cover()

    def retry(self) -> None:
        """Try again whatever couldn't reach the network (the user's Retry button)."""
        if not self.disc_id or self.busy:
            return
        self._failed.clear()
        if self.source in ("", SOURCE_NONE):
            self.source = ""
            self._lookup_album()
        else:
            self._fetch_lyrics()
            self._fetch_cover()
        self._on_change()

    def clear(self) -> None:
        self._gen += 1
        self.disc_id, self.album, self.source, self._record = "", None, "", None
        self._durations, self._drive = {}, None
        self._tasks, self._failed, self._downloaded = {}, set(), False

    def status(self) -> tuple[str, str]:
        """(headline, details) describing where this disc's info comes from, for the sidebar."""
        if self._tasks:
            return "Fetching online…", " · ".join(self._tasks.values())
        if self._failed:
            return "Couldn't reach " + " / ".join(sorted(self._failed)), "Check the connection, then press Retry."
        names = {
            SOURCE_EDITED: "your edits",
            SOURCE_CD_TEXT: "the disc's CD-Text",
            SOURCE_MUSICBRAINZ: "MusicBrainz",
        }.get(self.source, "not found")
        known = self.known_lyrics()
        with_lyrics = sum(1 for lyrics in known.values() if lyrics)
        parts = [f"Names: {names}"]
        if self.named:
            parts.append(f"Lyrics: {with_lyrics} tracks (LRCLIB)" if with_lyrics else "Lyrics: none")
        cover = {COVER_ARCHIVE: "Cover Art Archive", COVER_USER: "your image"}.get(self.cover_source, "none")
        parts.append(f"Cover: {cover}")
        headline = "Downloaded and saved" if self._downloaded else "Saved on this computer"
        return headline, " · ".join(parts)

    # ---------- worker bookkeeping (main thread) ----------

    def _set_task(self, gen: int, key: str, text: str) -> bool:
        if gen == self._gen:
            if text:
                self._tasks[key] = text
            else:
                self._tasks.pop(key, None)
            self._on_change()
        return False

    def _report(self, gen: int, key: str, text: str) -> None:
        """From a worker thread."""
        GLib.idle_add(self._set_task, gen, key, text)

    def _network_failed(self, gen: int, key: str, service: str) -> bool:
        log.warning("%s unreachable while fetching %s", service, key)
        if gen == self._gen:
            self._tasks.pop(key, None)
            self._failed.add(service)
            self._on_change()
        return False

    # ---------- album names ----------

    def _lookup_album(self) -> None:
        drive, gen, disc_id = self._drive, self._gen, self.disc_id
        numbers = [t.number for t in self.album.tracks]
        self._tasks["album"] = "Reading CD-Text from the disc"

        def work() -> None:
            # The disc's own CD-Text wins over any online database.
            info = parse_cd_info(drive.cd_info_report(), numbers)
            if info is not None:
                GLib.idle_add(self._album_resolved, gen, info, SOURCE_CD_TEXT)
                return
            self._report(gen, "album", "Looking up the album on MusicBrainz")
            try:
                info = musicbrainz.lookup(disc_id, drive.mcn(), len(numbers))
            except net.NetworkError:
                GLib.idle_add(self._album_unreachable, gen)
                return
            GLib.idle_add(self._album_resolved, gen, info, SOURCE_MUSICBRAINZ if info else SOURCE_NONE)

        threading.Thread(target=work, daemon=True).start()

    def _album_unreachable(self, gen: int) -> bool:
        if gen == self._gen:
            self.source = SOURCE_NONE  # not saved: next load (or Retry) asks again
            self._network_failed(gen, "album", "MusicBrainz")
        return False

    @property
    def cover_source(self) -> str:
        return self._record.cover_source if self._record else ""

    @property
    def cover_path(self) -> Path | None:
        record = self._record
        if record is None or not record.cover_file:
            return None
        path = cache_dir() / record.cover_file
        return path if path.is_file() else None

    def set_cover(self, source: Path) -> str | None:
        """Use the user's own image for this disc; returns an error message or None."""
        if not self.disc_id:
            return "No disc"
        extension = cover_extension(source.name)
        if extension is None:
            return "Pick a JPEG or PNG image"
        try:
            data = source.read_bytes()
        except OSError as e:
            return f"Can't read the image: {e.strerror or e}"
        name = cover_file_name(self.disc_id, extension)
        old = self._record.cover_file if self._record else ""
        if not _write(name, data):
            return "Can't save the image to the cache folder"
        if old and old != name:
            (cache_dir() / old).unlink(missing_ok=True)
        base = self._record or CdRecord(None, SOURCE_NONE, 0.0)  # checked=0: the album lookup still runs next time
        self._record = replace(base, cover_source=COVER_USER, cover_file=name, cover_checked=time.time())
        _save(self.disc_id, self._record)
        self._on_change()
        return None

    def set_album(self, info: AlbumInfo) -> None:
        """The user's edits: remembered for this disc and preferred over every other source."""
        if not self.disc_id:
            return
        if not info.release_id and self.album is not None:
            info = replace(info, release_id=self.album.release_id)  # keeps the cover-art link
        self._album_resolved(self._gen, info, SOURCE_EDITED)

    def lyrics(self, number: int) -> Lyrics | None:
        return self.known_lyrics().get(number)

    def known_lyrics(self) -> dict[int, Lyrics | None]:
        return known_lyrics(self._record, self.album) if self.album else {}

    def _album_resolved(self, gen: int, info: AlbumInfo | None, source: str) -> bool:
        if gen != self._gen:
            return False
        self._tasks.pop("album", None)
        log.info("CD %s: names from %s: %s", self.disc_id, source or "?", info.title if info else "-")
        if source in (SOURCE_CD_TEXT, SOURCE_MUSICBRAINZ, SOURCE_NONE):
            self._downloaded = self._downloaded or source != SOURCE_CD_TEXT
        old = self._record
        self.source = source
        if info is not None:
            self.album = info
        now = time.time()
        self._record = replace(old, album=info, source=source, checked=now) if old else CdRecord(info, source, now)
        _save(self.disc_id, self._record)
        self._fetch_lyrics()  # register these tasks first, so the UI never flashes "Lyrics: none" in between
        self._fetch_cover()
        self._on_change()
        return False

    # ---------- cover ----------

    def _fetch_cover(self) -> None:
        if not needs_cover_lookup(self._record, time.time()):
            return
        release_id, gen = self._record.album.release_id, self._gen
        self._tasks["cover"] = "Downloading the cover (Cover Art Archive)"

        def work() -> None:
            try:
                data = coverart.fetch_front(release_id)
            except net.NetworkError:
                GLib.idle_add(self._network_failed, gen, "cover", "Cover Art Archive")
                return
            GLib.idle_add(self._cover_resolved, gen, release_id, data)

        threading.Thread(target=work, daemon=True).start()

    def _cover_resolved(self, gen: int, release_id: str, data: bytes | None) -> bool:
        if gen != self._gen:
            return False
        self._tasks.pop("cover", None)
        record = self._record
        if record is None or record.cover_source == COVER_USER:
            self._on_change()
            return False  # the user picked their own while this was downloading
        if record.album is None or record.album.release_id != release_id:
            self._on_change()
            return False
        self._downloaded = True
        name = cover_file_name(self.disc_id, ".jpg")
        if data is not None and _write(name, data):
            self._record = replace(record, cover_source=COVER_ARCHIVE, cover_file=name, cover_checked=time.time())
        else:
            self._record = replace(record, cover_source=COVER_MISSING, cover_checked=time.time())
        _save(self.disc_id, self._record)
        self._on_change()
        return False

    # ---------- lyrics ----------

    def _fetch_lyrics(self) -> None:
        if not self.named:
            return  # searching LRCLIB for "Track 01" would only find strangers' songs
        self._lyrics_gen += 1
        album, gen, lyrics_gen = self.album, self._gen, self._lyrics_gen
        needed = lyrics_needed(self._record, album, time.time())
        if not needed:
            self._tasks.pop("lyrics", None)
            return
        durations = dict(self._durations)
        self._tasks["lyrics"] = f"Downloading lyrics 1/{len(needed)} (LRCLIB)"

        def work() -> None:
            for i, number in enumerate(needed, start=1):
                if (gen, lyrics_gen) != (self._gen, self._lyrics_gen):
                    return  # superseded: titles were edited or the disc changed
                self._report(gen, "lyrics", f"Downloading lyrics {i}/{len(needed)} (LRCLIB)")
                track = album.track(number)
                try:
                    found = lrclib.fetch(track.title, track.artist or album.artist, durations.get(number, 0))
                except net.NetworkError:
                    GLib.idle_add(self._network_failed, gen, "lyrics", "LRCLIB")
                    return  # unreachable: don't hammer it for every remaining track
                GLib.idle_add(self._lyrics_resolved, (gen, lyrics_gen), number, track.title, found)
            self._report(gen, "lyrics", "")

        threading.Thread(target=work, daemon=True).start()

    def _lyrics_resolved(self, gen: tuple[int, int], number: int, title: str, found: Lyrics | None) -> bool:
        if gen != (self._gen, self._lyrics_gen) or self._record is None:
            return False
        self._downloaded = True
        entries = {**self._record.lyrics, number: LyricsEntry(title, found, time.time())}
        self._record = replace(self._record, lyrics=entries)
        _save(self.disc_id, self._record)
        self._on_change()
        return False
