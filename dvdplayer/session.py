"""What the app does with the drive, the player, files and rips, independent of the GUI toolkit.

A UI (GTK/libadwaita on Linux, Qt on Windows) builds the widgets, forwards the user's actions to Session and
implements View, which Session calls to show things. Everything here runs on the UI thread; background work
comes back through dvdplayer.mainthread.
"""

import logging
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from domain.cd import track_after, track_before
from domain.cdmeta import AlbumInfo, TrackInfo, album_folder_name
from domain.disc import DiscKind, DriveStatus
from domain.housekeeping import format_bytes
from domain.library import media_kind
from domain.lyrics import Lyrics, line_index_at, parse_lrc
from domain.media import Title, Track, apply_disc_info, clamp, format_duration, main_title
from domain.rip import (
    RipKind,
    RipOptions,
    audio_track_spans,
    dvd_title_command,
    rip_options,
    safe_filename,
    unique_name,
)
from dvdplayer import appdata, audiorip, mainthread, userdirs
from dvdplayer.cdinfo import CdInfo
from dvdplayer.drive import Drive
from dvdplayer.localmedia import OpenedAlbum, read_album
from dvdplayer.player import Player
from dvdplayer.ripper import FfmpegJob, ImageJob, RipJob

TICK_MS = 250
DRIVE_POLL_S = 2
SEEK_STEP_MS = 10_000
VOLUME_STEP = 5
MOUNT_WAIT_POLLS = 5  # give the desktop's automount ~10s before playing without IFO info
SEEK_HOLD_S = 0.6  # the slider ignores the playback clock this long after the user moved it

EMPTY_PAGES = {
    DriveStatus.NO_DISC: ("Insert a disc", "Playback starts automatically."),
    DriveStatus.TRAY_OPEN: ("Tray open", "Insert a disc and close the tray."),
    DriveStatus.NOT_READY: ("Reading disc…", ""),
    DriveStatus.NO_INFO: ("Drive not found", "Is the USB drive plugged in?"),
    DriveStatus.DISC_OK: ("Loading disc…", ""),
}
RIP_FOLDERS = {RipKind.DVD_TITLE: userdirs.VIDEOS, RipKind.AUDIO_TRACKS: userdirs.MUSIC,
               RipKind.DISC_IMAGE: userdirs.DOWNLOADS}  # fmt: skip

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TitleRow:
    index: int
    label: str
    details: str  # length · chapters · main
    chapters: int


@dataclass(frozen=True)
class TrackRow:
    number: int
    label: str  # "3. Song" or "Track 3"
    length_ms: int


@dataclass(frozen=True)
class RipChoice:
    label: str
    kind: RipKind
    index: int  # DVD title index; -1 for the other kinds


class View(Protocol):
    """What Session asks the UI to show. Rows are keyed by title index (DVD/video) or track number (CD)."""

    def show_drive_status(self, status: DriveStatus) -> None: ...
    def show_empty(self, title: str, description: str) -> None: ...
    def show_player(self) -> None: ...
    def set_cd_mode(self, on: bool) -> None: ...  # also resets the list heading to Tracks/Titles
    def set_list_heading(self, title: str, description: str) -> None: ...
    def set_disc_text(self, text: str) -> None: ...
    def set_region_text(self, text: str) -> None: ...
    def set_subtitle(self, text: str) -> None: ...  # window subtitle: disc · title · chapter, or album · song
    def set_rip_enabled(self, enabled: bool) -> None: ...
    def set_busy(self, busy: bool) -> None: ...  # a rip runs: lock everything that would touch the drive
    def notify(self, text: str, action_label: str = "", action: Callable[[], None] | None = None,
               timeout_s: int | None = None) -> None: ...  # fmt: skip # timeout 0: stays until dismissed
    def show_title_rows(self, rows: list[TitleRow]) -> None: ...
    def show_track_rows(self, rows: list[TrackRow]) -> None: ...
    def rename_track_rows(self, labels: dict[int, str]) -> None: ...
    def clear_rows(self) -> None: ...
    def mark_current_row(self, key: int | None) -> None: ...
    def show_cover(self, path: Path | None, thumbnail: bool) -> None: ...
    def show_online(self, visible: bool, headline: str, details: str, busy: bool, failed: bool) -> None: ...
    def show_synced_lyrics(self, lines: tuple[str, str, str]) -> None: ...  # previous, current, next
    def show_plain_lyrics(self, text: str) -> None: ...
    def show_lyrics_note(self, text: str) -> None: ...
    def show_time(self, now_ms: int, length_ms: int, move_slider: bool) -> None: ...
    def show_playing(self, playing: bool) -> None: ...
    def show_track_menus(self, audio: list[Track], subs: list[Track], audio_id: int, subs_id: int) -> None: ...
    def show_frame(self, data: bytes, width: int, height: int, stride: int, aspect: float) -> None: ...
    def clear_video(self) -> None: ...
    def show_rip_progress(self, title: str, fraction: float, text: str) -> None: ...
    def hide_rip_progress(self) -> None: ...
    def show_volume(self, percent: int) -> None: ...
    def open_folder(self, folder: Path) -> None: ...


class Session:
    def __init__(self, view: View, device: str | None, render_frames: bool = True):
        """render_frames: VLC hands decoded frames to view.show_frame; otherwise the UI gives VLC a window."""
        self.view = view
        self.drive = Drive(device)
        self.player = Player(self._frame_from_vlc if render_frames else None)
        self.cd_info = CdInfo(self._on_cd_info_changed)
        self.album_listener: Callable[[], None] | None = None  # set by the rip dialog while it's open
        self.want_opus = False  # remembered between rips in this session
        self.open_dir = userdirs.special_dir(userdirs.MUSIC)

        self._status: DriveStatus | None = None
        self._loaded = False
        self._load_pending = False
        self._mount_waits = 0
        self._disc_label = ""
        self._disc_info: list[tuple[int, float]] = []
        self._titles: list[Title] = []
        self._autoplay_main = False
        self._audio: list[Track] = []
        self._subs: list[Track] = []
        self._seek_hold_until = 0.0
        self._ticks = 0
        self._volume = 100
        self._last_position: tuple[int, int] | None = None
        self._rip_options = RipOptions((), "No disc")
        self._rip_job: RipJob | None = None
        self._rip_title = ""
        self._rip_dest: Path | None = None
        self._rip_work_dir: Path | None = None  # temp files for the running rip (Opus metadata, WAVs)
        self.audio_spans: list[tuple[int, int, int]] = []
        self._cd_toc: tuple[list, int] | None = None  # (tracks, lead-out) of the audio CD, to reload its info
        self.cd_mode = False
        self._cd_track: int | None = None
        self._lyrics_lines: list[tuple[int, str]] = []
        self._lyrics_index = -2  # line currently shown; -2 forces a redraw
        self._cover_key: tuple | None = None  # (path, mtime) of the cover on screen
        # Files from disk (e.g. earlier rips) instead of the disc; the drive keeps being watched meanwhile.
        self._local_album: OpenedAlbum | None = None
        self._local_video: Path | None = None
        self._opening: Path | None = None  # album folder being read in the background
        self._open_gen = 0
        self._local_work_dir: Path | None = None  # embedded cover extracted from the tracks
        self._frame_lock = threading.Lock()
        self._pending_frame = None
        log.info("drive %s (%s)", self.drive.device, self.drive.name)

    # ---------- state the UI reads ----------

    @property
    def status(self) -> DriveStatus | None:
        return self._status

    @property
    def ripping(self) -> bool:
        return self._rip_job is not None

    @property
    def titles(self) -> list[Title]:
        return self._titles

    # ---------- disc lifecycle ----------

    def poll_drive(self) -> None:
        """Every DRIVE_POLL_S seconds (and once at startup)."""
        status = self.drive.status()
        if status is not self._status:
            log.info("drive status: %s", status.name)
            self._status = status
            self.view.show_drive_status(status)
            local = self._local_active()
            if status is DriveStatus.DISC_OK:
                self._load_pending, self._mount_waits = True, 0
                if local:
                    self.view.notify("Disc inserted", "Play Disc", self.reload_disc, timeout_s=8)
            elif local:
                self._load_pending = False  # a file is playing; changes in the drive don't interrupt it
            else:
                self._unload_disc()
            if not self._loaded and not local:
                self.view.show_empty(*EMPTY_PAGES[status])
        if self._load_pending and not self._local_active():
            self._try_load_disc()

    def _try_load_disc(self) -> None:
        kind = self.drive.disc_kind()
        label, mount = self.drive.label_and_mount()
        # Audio CDs have no filesystem, so there is nothing to wait for.
        if kind is not DiscKind.AUDIO and not mount and self._mount_waits < MOUNT_WAIT_POLLS:
            self._mount_waits += 1
            return
        self._load_pending = False
        is_video = Drive.is_dvd_video(mount)
        protected = self.drive.is_copy_protected()
        log.info("disc %s, label %r, mount %r, video DVD %s, CSS %s", kind.name, label, mount, is_video, protected)
        self._rip_options = rip_options(kind, is_video, protected)
        self.view.set_rip_enabled(self._rip_job is None)

        if kind is DiscKind.AUDIO:
            label = label or "Audio CD"
        self._disc_label = label or "Disc"
        self.view.set_disc_text(self._disc_label)
        self.view.set_subtitle(self._disc_label)

        if kind is DiscKind.AUDIO:
            self.view.set_region_text("—")
            self._load_audio_cd()
            return
        if mount and not is_video:
            self.view.set_region_text("—")
            self.view.show_empty("Not a video DVD", "This disc has no VIDEO_TS folder. Use Rip to save a disc image.")
            return

        self.view.set_region_text(Drive.region_text(mount) if mount else "Unknown")
        self._disc_info = Drive.title_info(mount) if mount else []
        # dvdsimple skips the disc menu; titles are picked from the sidebar instead.
        self.player.open(f"dvdsimple://{self.drive.vlc_path}")
        self._loaded = True
        self._autoplay_main = True
        self.view.show_player()

    def _load_audio_cd(self) -> None:
        tracks, leadout = self.drive.cd_tracks()
        self.audio_spans = audio_track_spans(tracks, leadout)
        self._cd_toc = (tracks, leadout)
        log.info("audio CD: %d audio tracks of %d", len(self.audio_spans), len(tracks))
        if not self.audio_spans:
            self.view.show_empty("No audio tracks", "The disc's table of contents couldn't be read.")
            return
        self._set_cd_mode(True)
        self.cd_info.load(self.drive, tracks, leadout, self.audio_spans)
        self._build_cd_rows()
        self._refresh_cover()
        self._refresh_online_status()
        self._loaded = True
        self.view.show_player()
        self.play_track(self.audio_spans[0][0])

    def _on_cd_info_changed(self) -> None:
        """New names or lyrics for the current disc (from cache, CD-Text, the network or the user)."""
        if self._local_album is not None:
            return  # a late callback from the disc that was playing before
        album = self.cd_info.album
        if album is not None and self.cd_info.named:
            self.view.rename_track_rows({n: f"{n}. {album.track(n).title}" for n, _, _ in self.audio_spans})
        if self._cd_track is not None:
            self._show_cd_track(self._cd_track)
        self._refresh_cover()
        self._refresh_online_status()
        if self.album_listener:
            self.album_listener()

    def _refresh_online_status(self) -> None:
        info = self.cd_info
        headline, details = info.status()
        self.view.show_online(self.cd_mode and self._local_album is None, headline, details, info.busy, info.failed)

    def retry_online(self) -> None:
        self.cd_info.retry()

    def _set_cd_mode(self, on: bool) -> None:
        self.cd_mode = on
        self._cd_track = None
        self.view.set_cd_mode(on)

    def _build_cd_rows(self) -> None:
        names = self._album_names()
        rows = []
        for number in self._track_numbers():
            label = f"{number}. {names.track(number).title}" if names else f"Track {number}"
            rows.append(TrackRow(number, label, self._track_length(number)))
        self.view.show_track_rows(rows)

    # Track lists come from the audio CD or from an album folder on disk; these hide which.

    def _track_numbers(self) -> list[int]:
        if self._local_album is not None:
            return self._local_album.album.numbers
        return [s[0] for s in self.audio_spans]

    def _album_names(self) -> AlbumInfo | None:
        """Real names, or None while the disc only has "Track N" placeholders."""
        if self._local_album is not None:
            return self._local_album.album.info
        info = self.cd_info.album
        return info if self.cd_info.named and info is not None else None

    def _track_length(self, number: int | None) -> int:
        if self._local_album is not None:
            return self._local_album.album.durations.get(number, 0)
        span = next((s for s in self.audio_spans if s[0] == number), None)
        return span[2] - span[1] if span else 0

    def _track_lyrics(self, number: int) -> Lyrics | None:
        if self._local_album is not None:
            return self._local_album.lyrics.get(number)
        return self.cd_info.lyrics(number)

    def _cover_path(self) -> Path | None:
        if self._local_album is not None:
            return self._local_album.cover
        return self.cd_info.cover_path if self.cd_mode else None

    def _refresh_cover(self) -> None:
        path = self._cover_path()
        try:
            key = (path, path.stat().st_mtime_ns) if path else None
        except OSError:
            key = None
        if key == self._cover_key:
            return  # lyrics updates also land here; don't decode the image each time
        self._cover_key = key
        # The thumbnail belongs to the disc row, so a local album's cover stays off it.
        self.view.show_cover(path if key else None, thumbnail=self._local_album is None)

    def play_track(self, number: int) -> None:
        self._cd_track = number
        if self._local_album is not None:
            self.player.open(self._local_album.path(number).as_uri())
        else:
            self.player.open_cd_track(self.drive.vlc_path, number)
        self._show_cd_track(number)

    def _show_cd_track(self, number: int) -> None:
        info = self._album_names()
        track = info.track(number) if info else TrackInfo(number, f"Track {number}")
        album = " · ".join(p for p in (info.title, track.artist or info.artist) if p) if info else self._disc_label
        self.view.mark_current_row(number)
        self.view.set_subtitle(f"{album} · {track.title}")
        self._show_lyrics(number)

    def _show_lyrics(self, number: int) -> None:
        lyrics = self._track_lyrics(number)
        self._lyrics_lines = parse_lrc(lyrics.synced) if lyrics and lyrics.synced else []
        self._lyrics_index = -2
        if self._lyrics_lines:
            self._update_lyric_line(self.player.time_ms())
        elif lyrics and lyrics.plain:
            self.view.show_plain_lyrics(lyrics.plain)
        elif self._local_album is not None:
            self.view.show_lyrics_note("")
        else:
            info = self.cd_info
            unsettled = info.resolving or (info.named and number not in info.known_lyrics())
            if unsettled and info.busy:
                note = "Looking for lyrics…"
            elif unsettled and info.failed:
                note = "Lyrics couldn't be downloaded — see Album info in the sidebar"
            else:
                note = ""
            self.view.show_lyrics_note(note)

    def _update_lyric_line(self, now_ms: int) -> None:
        index = line_index_at(self._lyrics_lines, now_ms)
        if index == self._lyrics_index:
            return
        self._lyrics_index = index
        lines = self._lyrics_lines
        prev, cur, nxt = (lines[i][1] if 0 <= i < len(lines) else "" for i in (index - 1, index, index + 1))
        self.view.show_synced_lyrics((prev, cur, nxt))

    def skip_back(self) -> None:
        if not self.cd_mode:
            self.player.previous_chapter()
        elif self._cd_track is not None:
            target = track_before(self._track_numbers(), self._cd_track, self.player.time_ms())
            if target == self._cd_track:
                self.player.seek(0)
            else:
                self.play_track(target)

    def skip_forward(self) -> None:
        if not self.cd_mode:
            self.player.next_chapter()
        elif self._cd_track is not None:
            target = track_after(self._track_numbers(), self._cd_track)
            if target is not None:
                self.play_track(target)

    def _tick_cd(self) -> None:
        if self.player.is_ended() and self._cd_track is not None:
            target = track_after(self._track_numbers(), self._cd_track)
            if target is not None:
                self.play_track(target)
            else:
                self.player.stop()  # end of disc: stay on the last track, ready to replay
        length = self.player.length_ms() or self._track_length(self._cd_track)
        now = self.player.time_ms()
        self.view.show_time(now, length, time.monotonic() > self._seek_hold_until)
        if self._lyrics_lines:
            self._update_lyric_line(now)

    def _unload_disc(self) -> None:
        if self._loaded:
            self.player.stop()
        self._set_cd_mode(False)  # also resets the list heading a local album or video changed
        self._loaded = self._load_pending = False
        self._open_gen += 1  # drop an album folder still being read
        self._local_album = self._local_video = self._opening = None
        if self._local_work_dir is not None:
            shutil.rmtree(self._local_work_dir, ignore_errors=True)
            self._local_work_dir = None
        self._titles, self._disc_info = [], []
        self._last_position = None
        self.audio_spans, self._cd_toc = [], None
        self.cd_info.clear()
        self._lyrics_lines = []
        self._refresh_cover()
        self._refresh_online_status()
        self._rip_options = RipOptions((), "No disc")
        self.view.set_rip_enabled(False)
        self.view.clear_rows()
        self._set_tracks([], [])
        self.view.clear_video()
        self.view.set_disc_text("—")
        self.view.set_region_text("—")
        self.view.set_subtitle("")

    def reload_disc(self) -> None:
        self._unload_disc()
        self._status = None
        self.poll_drive()

    def _local_active(self) -> bool:
        return self._local_album is not None or self._local_video is not None or self._opening is not None

    def eject_or_close(self) -> None:
        if self._status is DriveStatus.TRAY_OPEN:
            self.drive.close_tray()
            return
        if not self._local_active():  # a file keeps playing while the disc comes out
            self._unload_disc()
            self.view.show_empty("Ejecting…", "")
        self.drive.eject()

    # ---------- files from disk ----------

    def can_open(self) -> bool:
        if self._rip_job is not None:
            self.view.notify("Wait for the rip to finish")
            return False
        return True

    def open_path(self, path: Path) -> None:
        """A folder or an audio file opens the album (from that track); a video file plays on its own."""
        kind = "folder" if path.is_dir() else media_kind(path.name)
        log.info("open %s (%s)", path, kind)
        if kind is None:
            self.view.notify(f"Can't play {path.name}")
            return
        if kind == "video":
            self._open_video(path)
        else:
            self._open_album(path if kind == "folder" else path.parent, None if kind == "folder" else path.name)

    def _leave_current(self, folder: Path) -> None:
        self._unload_disc()
        self.open_dir = folder.parent if folder.is_dir() and folder.parent != folder else folder
        self.view.set_rip_enabled(False)  # Rip works on the disc, not on files
        if self._status is DriveStatus.DISC_OK:
            self.view.set_disc_text("Not playing · Reload to play it")

    def _open_album(self, folder: Path, start_file: str | None) -> None:
        self._leave_current(folder)
        self._opening = folder
        gen = self._open_gen
        work_dir = self._local_work_dir = Path(tempfile.mkdtemp(prefix="dvd-player-album-"))
        self.view.show_empty(f"Opening {folder.name}…", "")

        def work() -> None:
            opened = read_album(folder, work_dir)
            mainthread.call(self._album_opened, gen, folder, start_file, opened)

        threading.Thread(target=work, daemon=True).start()

    def _album_opened(self, gen: int, folder: Path, start_file: str | None, opened: OpenedAlbum | None) -> None:
        if gen != self._open_gen:
            return
        self._opening = None
        if opened is None:
            self.view.show_empty("No music here", f"{folder} has no audio files. Use Reload to go back to the disc.")
            return
        self._local_album = opened
        info = opened.album.info
        self._disc_label = info.title
        self._set_cd_mode(True)
        self.view.set_list_heading(info.title, f"{home_relative(folder)} · click a track to play it")
        self._build_cd_rows()
        self._refresh_cover()
        self._refresh_online_status()
        self._loaded = True
        self.view.show_player()
        files = opened.album.files
        start = next((n for n, name in files.items() if name == start_file), opened.album.numbers[0])
        self.play_track(start)

    def _open_video(self, path: Path) -> None:
        self._leave_current(path.parent)
        self._local_video = path
        self._disc_label = path.stem
        self.view.set_subtitle(path.stem)
        self.view.set_list_heading("Titles", f"{home_relative(path.parent)} · click a chapter to play it")
        self.player.open(path.as_uri())
        self._loaded = True
        self.view.show_player()

    # ---------- titles ----------

    def _build_title_rows(self) -> None:
        main = main_title(self._titles)
        rows = []
        for t in self._titles:
            chapters = len(self.player.chapters(t.index))
            parts = [format_duration(t.duration_ms) if t.duration_ms else "length unknown"]
            parts.append(f"{chapters} chapters" if chapters != 1 else "1 chapter")
            if main is not None and t.index == main.index and len(self._titles) > 1:
                parts.append("main")
            rows.append(TitleRow(t.index, t.label, " · ".join(parts), chapters))
        self.view.show_title_rows(rows)

    def play(self, title: int, chapter: int = 0) -> None:
        self._autoplay_main = False
        self.player.play_title(title, chapter)

    # ---------- periodic sync ----------

    def tick(self) -> None:
        """Every TICK_MS."""
        self._ticks += 1
        if not self._loaded:
            return
        if self.cd_mode:
            self._tick_cd()
        elif not self._titles:
            raw = self.player.titles()
            if self._local_video is not None:
                # One title named after the file; some formats report none, so fall back to the length.
                length = self.player.length_ms()
                raw = raw or ([Title(0, length)] if length else [])
                raw = [replace(t, name=self._local_video.stem) for t in raw] if len(raw) == 1 else raw
            if raw:
                self._titles = apply_disc_info(raw, self._disc_info)
                log.info("%d titles", len(self._titles))
                self._build_title_rows()
                main = main_title(self._titles)
                if self._autoplay_main and main and main.index != self.player.current_title():
                    self.player.play_title(main.index)
                self._autoplay_main = False

        if not self.cd_mode:
            self._sync_position()
        self.player.sync_volume()
        self.view.show_playing(self.player.is_playing())
        if self._ticks % 4 == 0 and not self.cd_mode:
            self._set_tracks(self.player.audio_tracks(), self.player.subtitle_tracks())

    def _current_title(self) -> Title | None:
        cur = self.player.current_title()
        return self._titles[cur] if 0 <= cur < len(self._titles) else None

    def _sync_position(self) -> None:
        title = self._current_title()
        length = self.player.length_ms() or (title.duration_ms if title else 0)
        now = self.player.time_ms()
        self.view.show_time(now, length, time.monotonic() > self._seek_hold_until)
        position = (self.player.current_title(), self.player.current_chapter())
        if position != self._last_position and title is not None:
            self._last_position = position
            self.view.mark_current_row(title.index)
            chapter = f" · Chapter {position[1] + 1}" if position[1] >= 0 else ""
            name = title.label if self._local_video else f"{self._disc_label} · {title.label}"
            self.view.set_subtitle(f"{name}{chapter}")

    def _set_tracks(self, audio: list[Track], subs: list[Track]) -> None:
        self._audio, self._subs = audio, subs
        loaded = self._loaded
        self.view.show_track_menus(
            audio, subs, self.player.audio_track() if loaded else -1, self.player.subtitle_track() if loaded else -1
        )

    # ---------- playback controls ----------

    def toggle_pause(self) -> None:
        self.player.toggle_pause()

    def seek(self, ms: int) -> None:
        """From the slider: the clock doesn't move it back for a moment, so it doesn't jump under the pointer."""
        self._seek_hold_until = time.monotonic() + SEEK_HOLD_S
        self.player.seek(int(ms))

    def seek_by(self, step_ms: int) -> None:
        length = self.player.length_ms()
        self.player.seek(clamp(self.player.time_ms() + step_ms, 0, length or 1 << 31))

    def set_volume(self, percent: int) -> None:
        self._volume = clamp(int(percent), 0, 100)
        self.player.set_volume(self._volume)

    def change_volume(self, step: int) -> None:
        self.set_volume(self._volume + step)
        self.view.show_volume(self._volume)

    def select_audio(self, position: int) -> None:
        """position in the list last given to show_track_menus."""
        if 0 <= position < len(self._audio):
            self.player.set_audio_track(self._audio[position].id)

    def select_subtitles(self, position: int) -> None:
        if 0 <= position < len(self._subs):
            self.player.set_subtitle_track(self._subs[position].id)

    # ---------- video frames (VLC thread -> UI thread) ----------

    def _frame_from_vlc(self, data: bytes, width: int, height: int, stride: int) -> None:
        with self._frame_lock:
            already_queued = self._pending_frame is not None
            self._pending_frame = (data, width, height, stride)
        if not already_queued:
            mainthread.call(self._present_frame)

    def _present_frame(self) -> None:
        with self._frame_lock:
            frame, self._pending_frame = self._pending_frame, None
        if frame is not None and self._loaded:
            data, width, height, stride = frame
            title = self._current_title()
            aspect = title.aspect if title and title.aspect else self.player.video_aspect(width, height)
            self.view.show_frame(data, width, height, stride, aspect)

    # ---------- ripping ----------

    @property
    def rip_blocked_reason(self) -> str:
        return self._rip_options.blocked_reason

    def rip_choices(self) -> list[RipChoice]:
        choices = []
        for kind in self._rip_options.kinds:
            if kind is RipKind.DVD_TITLE:
                for t in self._titles or self.player.titles():
                    length = f" · {format_duration(t.duration_ms)}" if t.duration_ms else ""
                    choices.append(RipChoice(f"{t.label}{length} → MKV", kind, t.index))
            elif kind is RipKind.AUDIO_TRACKS:
                choices.append(RipChoice(f"All {len(self.audio_spans)} tracks → FLAC", kind, -1))
            else:
                choices.append(RipChoice("Whole disc image → ISO", kind, -1))
        return choices

    def default_choice(self, choices: list[RipChoice]) -> int:
        """The main title preselected, else the first choice."""
        main = main_title(self._titles)
        for i, choice in enumerate(choices):
            if choice.kind is RipKind.DVD_TITLE and main and choice.index == main.index:
                return i
        return 0

    @staticmethod
    def default_rip_folder(kind: RipKind) -> Path:
        return userdirs.special_dir(RIP_FOLDERS[kind])

    def start_rip(
        self, choice: RipChoice, folder: Path, *, album: AlbumInfo | None = None, want_lyrics: bool = False
    ) -> None:
        """album: the names edited in the rip dialog (audio CDs), remembered for the disc."""
        if album is not None and choice.kind is RipKind.AUDIO_TRACKS:
            self.cd_info.set_album(album)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.view.notify(f"Can't use that folder: {e.strerror}")
            return

        def free(name: str) -> str:
            return str(folder / unique_name(name, lambda n: (folder / n).exists()))

        kind, title_index = choice.kind, choice.index
        log.info("rip %s (title %d) to %s", kind.name, title_index, folder)
        base = safe_filename(self._disc_label)
        device = self.drive.device
        if kind is RipKind.DVD_TITLE:
            title = next((t for t in self._titles if t.index == title_index), None)
            out = free(f"{base} - {title.label if title else f'Title {title_index + 1}'}.mkv")
            job = FfmpegJob(self._on_rip_progress, self._on_rip_done, [out])
            what, self._rip_dest = f"Ripping {title.label if title else 'title'}", Path(out)
            steps = [(dvd_title_command(device, title_index + 1, out), title.duration_ms if title else 0)]
        elif kind is RipKind.AUDIO_TRACKS:
            info = self.cd_info.album
            album_dir = Path(free(album_folder_name(info)))
            album_dir.mkdir()
            opus_dir = Path(free(f"{album_dir.name} (Opus)")) if self.want_opus else None
            if opus_dir:
                opus_dir.mkdir()
            self._rip_work_dir = Path(tempfile.mkdtemp(prefix="dvd-player-rip-"))
            cover = self._copy_cover(album_dir)
            steps, partial = audiorip.plan(
                device, info, list(self.audio_spans), album_dir, cover, self.cd_info.known_lyrics(),
                want_lyrics, opus_dir, self._rip_work_dir,
            )  # fmt: skip
            job = FfmpegJob(self._on_rip_progress, self._on_rip_done, partial)
            copies = " + Opus" if opus_dir else ""
            what, self._rip_dest = f"Ripping {len(self.audio_spans)} tracks{copies}", album_dir
        else:
            out = free(f"{base}.iso")
            job = ImageJob(self._on_rip_progress, self._on_rip_done, [out])
            what, self._rip_dest = "Copying disc image", Path(out)

        # The drive can't serve VLC and the rip at the same time without crawling.
        self.player.stop()
        self.view.clear_video()
        self.view.set_busy(True)
        self._rip_job, self._rip_title = job, what
        self.view.show_rip_progress(what, 0.0, "starting…")
        if isinstance(job, ImageJob):
            job.start(self.drive.raw_path, out, self.drive.size_bytes())
        else:
            job.start(steps)

    def cancel_rip(self) -> None:
        if self._rip_job is not None:
            self._rip_job.cancel()

    def _copy_cover(self, album: Path) -> str:
        """cover.jpg/png in the album folder (what music players look for); "" when there is none."""
        source = self.cd_info.cover_path
        if source is None:
            return ""
        target = album / f"cover{source.suffix}"
        try:
            shutil.copyfile(source, target)
        except OSError:
            return ""
        return str(target)

    def _on_rip_progress(self, fraction: float, text: str) -> None:
        self.view.show_rip_progress(self._rip_title, fraction, text)

    def _on_rip_done(self, ok: bool, message: str) -> None:
        self._rip_job = None
        if self._rip_work_dir is not None:
            shutil.rmtree(self._rip_work_dir, ignore_errors=True)
            self._rip_work_dir = None
        self.view.hide_rip_progress()
        self.view.set_busy(False)
        self.view.set_rip_enabled(
            self._status is DriveStatus.DISC_OK and not self._load_pending and not self._local_active()
        )
        if ok and self._rip_dest is not None:
            folder = self._rip_dest if self._rip_dest.is_dir() else self._rip_dest.parent
            self.view.notify(f"Saved {self._rip_dest.name}", "Open Folder", lambda: self.view.open_folder(folder), 0)
        else:
            self.view.notify(message, timeout_s=8)

    # ---------- saved data ----------

    def clearable_data(self) -> appdata.Clearable | None:
        """What Clear Saved Data would delete, or None (after telling the user why) when there's nothing to do."""
        if self._rip_job is not None:
            self.view.notify("Wait for the rip to finish")
            return None
        target = appdata.clearable({p for p in (self._rip_work_dir, self._local_work_dir) if p})
        if not target.paths:
            self.view.notify("Nothing to clear")
            return None
        return target

    def clear_data(self, target: appdata.Clearable) -> None:
        freed = appdata.clear(target)
        log.info("cleared saved data: %d bytes in %d items", freed, len(target.paths))
        self.view.notify(f"Cleared {format_bytes(freed)}")
        if self._cd_toc and self._local_album is None and self.cd_mode:
            # The disc in the drive lost its saved names and cover: look them up again right away.
            self.cd_info.load(self.drive, *self._cd_toc, self.audio_spans)
            self._on_cd_info_changed()

    # ---------- shutdown ----------

    def shutdown(self) -> None:
        """Stop VLC and any rip; safe to call twice (window close, then app shutdown)."""
        if self._rip_job is not None:
            self._rip_job.cancel()
        self._loaded = False
        self.player.release()
        if self._local_work_dir is not None:
            shutil.rmtree(self._local_work_dir, ignore_errors=True)


def home_relative(path: Path) -> str:
    if sys.platform == "win32":
        return str(path)
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)
