"""Main window: drive panel + title/chapter list in a sidebar, video and controls in the content area."""

import shutil
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from domain.cd import track_after, track_before  # noqa: E402
from domain.cdcache import (  # noqa: E402
    COVER_USER,
    SOURCE_CD_TEXT,
    SOURCE_EDITED,
    SOURCE_MUSICBRAINZ,
)
from domain.cdmeta import AlbumInfo, TrackInfo, album_folder_name  # noqa: E402
from domain.disc import DiscKind, DriveStatus  # noqa: E402
from domain.library import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS, media_kind  # noqa: E402
from domain.lyrics import Lyrics, line_index_at, parse_lrc  # noqa: E402
from domain.media import Title, Track, apply_disc_info, clamp, format_duration, main_title  # noqa: E402
from domain.rip import (  # noqa: E402
    OPUS_BITRATE_KBPS,
    RipKind,
    RipOptions,
    audio_track_spans,
    dvd_title_command,
    rip_options,
    safe_filename,
    unique_name,
)
from dvdplayer import audiorip  # noqa: E402
from dvdplayer.cdinfo import CdInfo, app_cache_dir  # noqa: E402
from dvdplayer.drive import Drive  # noqa: E402
from dvdplayer.images import load_texture  # noqa: E402
from dvdplayer.localmedia import OpenedAlbum, read_album  # noqa: E402
from dvdplayer.player import Player  # noqa: E402
from dvdplayer.ripper import FfmpegJob, ImageJob, RipJob  # noqa: E402
from dvdplayer.video_view import VideoView  # noqa: E402

TICK_MS = 250
DRIVE_POLL_S = 2
SEEK_STEP_MS = 10_000
VOLUME_STEP = 5
MOUNT_WAIT_POLLS = 5  # give GNOME automount ~10s before playing without IFO info

EMPTY_PAGES = {
    DriveStatus.NO_DISC: ("Insert a disc", "Playback starts automatically."),
    DriveStatus.TRAY_OPEN: ("Tray open", "Insert a disc and close the tray."),
    DriveStatus.NOT_READY: ("Reading disc…", ""),
    DriveStatus.NO_INFO: ("Drive not found", "Is the USB drive plugged in?"),
    DriveStatus.DISC_OK: ("Loading disc…", ""),
}


LYRICS_CSS = """
.lyric-current { font-size: 26pt; font-weight: 800; }
.lyric-side { font-size: 15pt; opacity: 0.5; }
.lyric-plain { font-size: 15pt; }
"""


def _text_height(label: Gtk.Label, lines: int) -> int:
    """Pixel height of `lines` lines in the label's current font (styles apply once it is mapped)."""
    return label.create_pango_layout("\n".join(["Ag漢"] * lines)).get_pixel_size()[1]


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, device: str | None):
        super().__init__(application=app, title="DVD Player", default_width=1180, default_height=720)
        self._drive = Drive(device)
        self._player = Player(self._frame_from_vlc)

        self._status: DriveStatus | None = None
        self._loaded = False
        self._load_pending = False
        self._mount_waits = 0
        self._disc_label = ""
        self._disc_info: list[tuple[int, float]] = []
        self._titles: list[Title] = []
        self._autoplay_main = False
        self._title_markers: dict[int, Gtk.Image] = {}
        self._title_rows: list[Gtk.Widget] = []
        self._audio: list[Track] = []
        self._subs: list[Track] = []
        self._syncing_tracks = False
        self._seek_hold_until = 0.0
        self._ticks = 0
        self._last_position: tuple[int, int] | None = None
        self._rip_options = RipOptions((), "No disc")
        self._rip_job: RipJob | None = None
        self._rip_dest: Path | None = None
        self._rip_work_dir: Path | None = None  # temp files for the running rip (Opus metadata)
        self._want_opus = False  # remembered between rips in this session
        self._audio_spans: list[tuple[int, int, int]] = []
        self._cd_mode = False
        self._cd_track: int | None = None
        self._cd_info = CdInfo(self._on_cd_info_changed)
        self._album_listener = None  # set while the rip dialog is open
        self._lyrics_lines: list[tuple[int, str]] = []
        self._lyrics_index = -2  # line currently shown; -2 forces a redraw
        self._cover_key: tuple | None = None  # (path, mtime) of the cover on screen
        # Files from disk (e.g. earlier rips) instead of the disc; the drive keeps being watched meanwhile.
        self._local_album: OpenedAlbum | None = None
        self._local_video: Path | None = None
        self._opening: Path | None = None  # album folder being read in the background
        self._open_gen = 0
        self._local_work_dir: Path | None = None  # embedded cover extracted from the tracks
        self._open_dir = Path(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC) or GLib.get_home_dir())

        self._frame_lock = threading.Lock()
        self._pending_frame = None

        css = Gtk.CssProvider()
        css.load_from_string(LYRICS_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._build_ui()
        self._add_open_actions(app)
        self.connect("close-request", self._on_close)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)

        self._poll_drive()
        GLib.timeout_add_seconds(DRIVE_POLL_S, self._poll_drive)
        GLib.timeout_add(TICK_MS, self._tick)

    # ---------- UI construction ----------

    def _build_ui(self) -> None:
        self._window_title = Adw.WindowTitle(title="DVD Player")
        header = Adw.HeaderBar(title_widget=self._window_title)

        sidebar_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", active=True, tooltip_text="Toggle sidebar")
        header.pack_start(sidebar_btn)
        open_menu = Gio.Menu()
        open_menu.append("Open Album Folder…", "win.open-folder")
        open_menu.append("Open File…", "win.open-file")
        self._open_btn = Gtk.MenuButton(
            icon_name="document-open-symbolic",
            tooltip_text="Play a ripped album or video from disk",
            menu_model=open_menu,
        )
        header.pack_start(self._open_btn)
        self._eject_btn = Gtk.Button(icon_name="media-eject-symbolic", tooltip_text="Eject")
        self._eject_btn.connect("clicked", self._on_eject)
        header.pack_end(self._eject_btn)
        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload disc (back to the disc)")
        reload_btn.connect("clicked", lambda *_: self._reload_disc())
        header.pack_end(reload_btn)
        self._rip_btn = Gtk.Button(
            child=Adw.ButtonContent(icon_name="document-save-symbolic", label="Rip"),
            tooltip_text="Rip disc to file…",
            sensitive=False,
        )
        self._rip_btn.connect("clicked", lambda *_: self._open_rip_dialog())
        header.pack_end(self._rip_btn)
        self._reload_btn = reload_btn

        self._split = Adw.OverlaySplitView(sidebar=self._build_sidebar(), content=self._build_content())
        self._split.set_min_sidebar_width(280)
        self._split.set_max_sidebar_width(360)
        sidebar_btn.bind_property("active", self._split, "show-sidebar", GObject.BindingFlags.BIDIRECTIONAL)

        self._toasts = Adw.ToastOverlay(child=self._split)
        self._toolbar_view = Adw.ToolbarView(content=self._toasts)
        self._toolbar_view.add_top_bar(header)
        self.set_content(self._toolbar_view)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

    def _build_sidebar(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )

        drive_group = Adw.PreferencesGroup(title="Drive")
        self._drive_row = self._info_row(drive_group, "Drive", self._drive.name)
        self._status_row = self._info_row(drive_group, "Status", "—")
        self._disc_row = self._info_row(drive_group, "Disc", "—")
        self._disc_thumb = Gtk.Picture(visible=False, valign=Gtk.Align.CENTER)
        self._disc_thumb.add_css_class("card")
        self._disc_thumb.set_overflow(Gtk.Overflow.HIDDEN)
        self._disc_row.add_prefix(self._disc_thumb)
        self._region_row = self._info_row(drive_group, "Disc region", "—")
        box.append(drive_group)

        self._online_group = Adw.PreferencesGroup(title="Album info", visible=False)
        self._online_row = Adw.ActionRow(
            title="—",
            subtitle_selectable=True,
            tooltip_text=(
                "Names, lyrics and covers come from the disc's CD-Text when it has any, otherwise from "
                "MusicBrainz, LRCLIB and the Cover Art Archive. Only the disc ID, barcode, song titles, "
                "artist and track lengths are sent. Results are kept in "
                f"{app_cache_dir() if sys.platform == 'win32' else '~/.cache/dvd-player'}, "
                "so a disc is only looked up once."
            ),
        )
        # Adw.Spinner needs libadwaita 1.6; Ubuntu 24.04 ships 1.5.
        self._online_spinner = (
            Adw.Spinner(visible=False) if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True, visible=False)
        )
        self._online_icon = Gtk.Image(icon_name="emblem-ok-symbolic")
        self._online_row.add_prefix(self._online_spinner)
        self._online_row.add_prefix(self._online_icon)
        self._retry_btn = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Retry", valign=Gtk.Align.CENTER, visible=False
        )
        self._retry_btn.add_css_class("flat")
        self._retry_btn.connect("clicked", lambda *_: self._cd_info.retry())
        self._online_row.add_suffix(self._retry_btn)
        self._online_group.add(self._online_row)
        box.append(self._online_group)

        self._titles_group = Adw.PreferencesGroup(title="Titles", description="Click a title or chapter to play it")
        box.append(self._titles_group)

        self._rip_group = Adw.PreferencesGroup(title="Ripping", visible=False)
        self._rip_row = Adw.ActionRow(title="Ripping…")
        cancel = Gtk.Button(icon_name="process-stop-symbolic", tooltip_text="Cancel rip", valign=Gtk.Align.CENTER)
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda *_: self._rip_job and self._rip_job.cancel())
        self._rip_row.add_suffix(cancel)
        self._rip_group.add(self._rip_row)
        self._rip_bar = Gtk.ProgressBar(margin_top=8)
        self._rip_group.add(self._rip_bar)
        box.insert_child_after(self._rip_group, drive_group)

        return Gtk.ScrolledWindow(child=box, hscrollbar_policy=Gtk.PolicyType.NEVER)

    @staticmethod
    def _info_row(group: Adw.PreferencesGroup, title: str, value: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=value, subtitle_selectable=True)
        row.add_css_class("property")
        group.add(row)
        return row

    def _build_content(self) -> Gtk.Widget:
        open_buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        for label, action in (("Open Album Folder…", "win.open-folder"), ("Open File…", "win.open-file")):
            b = Gtk.Button(label=label, action_name=action)
            b.add_css_class("pill")
            open_buttons.append(b)
        self._empty_page = Adw.StatusPage(icon_name="media-optical-symbolic", child=open_buttons)
        self._video = VideoView()
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_video_click)
        self._video.add_controller(click)

        self._cd_page = self._build_cd_page()
        self._media_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self._media_stack.add_named(self._video, "video")
        self._media_stack.add_named(self._cd_page, "cd")

        player_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        player_box.append(self._media_stack)
        player_box.append(self._build_controls())

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._empty_page, "empty")
        self._stack.add_named(player_box, "player")
        return self._stack

    def _build_cd_page(self) -> Gtk.Widget:
        """Cover filling the free space, big lyrics under it; track details live in the window title."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(24)
        self._cover_picture = Gtk.Picture(content_fit=Gtk.ContentFit.FILL, can_shrink=True)
        self._cover_picture.add_css_class("card")
        self._cover_picture.set_overflow(Gtk.Overflow.HIDDEN)
        # The frame keeps the picture at the image's own shape, so the rounded card hugs the artwork.
        self._cover_frame = Gtk.AspectFrame(child=self._cover_picture, obey_child=False, vexpand=True, visible=False)
        self._cd_icon = Gtk.Image(icon_name="media-optical-cd-audio-symbolic", pixel_size=128, vexpand=True)
        self._cd_icon.add_css_class("dim-label")
        for widget in (self._cover_frame, self._cd_icon, self._build_lyrics_view()):
            box.append(widget)
        return box

    def _refresh_cover(self) -> None:
        path = self._cover_path()
        try:
            key = (path, path.stat().st_mtime_ns) if path else None
        except OSError:
            key = None
        if key == self._cover_key:
            return  # lyrics updates also land here; don't decode the image each time
        self._cover_key = key
        # Large enough to stay sharp in fullscreen on a HiDPI screen; the picture scales it down.
        big = load_texture(path, 1600, 1600) if key else None
        self._cover_picture.set_paintable(big)
        if big is not None:
            self._cover_frame.set_ratio(big.get_width() / big.get_height())
        self._cover_frame.set_visible(big is not None)
        self._cd_icon.set_visible(big is None)
        # The thumbnail belongs to the disc row, so a local album's cover stays off it.
        small = load_texture(path, 40, 40) if key and self._local_album is None else None
        self._disc_thumb.set_paintable(small)
        self._disc_thumb.set_visible(small is not None)

    def _build_lyrics_view(self) -> Gtk.Widget:
        self._lyrics_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=False)
        synced = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._lyric_labels = []
        # Previous / current / next. Fixed line counts keep the cover from jumping as lines change length.
        for css, lines in (("lyric-side", 1), ("lyric-current", 2), ("lyric-side", 1)):
            label = Gtk.Label(
                wrap=True, lines=lines, ellipsize=Pango.EllipsizeMode.END, justify=Gtk.Justification.CENTER
            )
            label.add_css_class(css)
            label.connect("map", lambda w, n=lines: w.set_size_request(-1, _text_height(w, n)))
            synced.append(label)
            self._lyric_labels.append(label)
        self._lyrics_stack.add_named(synced, "synced")
        self._plain_lyrics = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, selectable=True)
        self._plain_lyrics.add_css_class("lyric-plain")
        plain = Gtk.ScrolledWindow(child=self._plain_lyrics, min_content_height=220)
        plain.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._lyrics_stack.add_named(plain, "plain")
        self._lyrics_note = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._lyrics_note.add_css_class("dim-label")
        self._lyrics_stack.add_named(self._lyrics_note, "note")
        return self._lyrics_stack

    def _build_controls(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=8, margin_end=8)
        self._controls = bar

        def button(icon: str, tip: str, handler) -> Gtk.Button:
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_: handler())
            bar.append(b)
            return b

        self._prev_btn = button("media-skip-backward-symbolic", "Previous chapter", self._skip_back)
        self._play_btn = button("media-playback-start-symbolic", "Play/Pause (Space)", self._player.toggle_pause)
        self._next_btn = button("media-skip-forward-symbolic", "Next chapter", self._skip_forward)

        self._time_label = Gtk.Label(label="0:00 / 0:00", width_chars=17)
        self._time_label.add_css_class("numeric")
        bar.append(self._time_label)

        self._seek = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False)
        self._seek.set_range(0, 1)
        self._seek.connect("change-value", self._on_seek)
        bar.append(self._seek)

        self._audio_dd = Gtk.DropDown(tooltip_text="Audio track", valign=Gtk.Align.CENTER)
        self._audio_dd.connect("notify::selected", self._on_audio_selected)
        bar.append(self._audio_dd)
        self._subs_dd = Gtk.DropDown(tooltip_text="Subtitles", valign=Gtk.Align.CENTER)
        self._subs_dd.connect("notify::selected", self._on_subs_selected)
        bar.append(self._subs_dd)
        self._lyrics_btn = Gtk.ToggleButton(
            icon_name="media-view-subtitles-symbolic", tooltip_text="Lyrics (L)", active=True, visible=False
        )
        self._lyrics_btn.add_css_class("flat")
        self._lyrics_btn.set_valign(Gtk.Align.CENTER)
        self._lyrics_btn.connect("toggled", lambda b: self._lyrics_stack.set_visible(b.get_active()))
        bar.append(self._lyrics_btn)

        self._volume = Gtk.ScaleButton.new(
            0,
            100,
            VOLUME_STEP,
            [
                "audio-volume-muted-symbolic",
                "audio-volume-high-symbolic",
                "audio-volume-low-symbolic",
                "audio-volume-medium-symbolic",
            ],
        )
        self._volume.set_value(100)
        self._volume.set_valign(Gtk.Align.CENTER)
        self._volume.connect("value-changed", lambda _b, v: self._player.set_volume(int(v)))
        bar.append(self._volume)

        button("view-fullscreen-symbolic", "Fullscreen (F)", self._toggle_fullscreen)
        return bar

    # ---------- disc lifecycle ----------

    def _poll_drive(self) -> bool:
        status = self._drive.status()
        if status is not self._status:
            self._status = status
            self._status_row.set_subtitle(status.label)
            self._eject_btn.set_icon_name(
                "go-up-symbolic" if status is DriveStatus.TRAY_OPEN else "media-eject-symbolic"
            )
            self._eject_btn.set_tooltip_text("Close tray" if status is DriveStatus.TRAY_OPEN else "Eject")
            local = self._local_active()
            if status is DriveStatus.DISC_OK:
                self._load_pending, self._mount_waits = True, 0
                if local:
                    toast = Adw.Toast(title="Disc inserted", button_label="Play Disc", timeout=8)
                    toast.connect("button-clicked", lambda *_: self._reload_disc())
                    self._toasts.add_toast(toast)
            elif local:
                self._load_pending = False  # a file is playing; changes in the drive don't interrupt it
            else:
                self._unload_disc()
            if not self._loaded and not local:
                self._show_empty_page(*EMPTY_PAGES[status])
        if self._load_pending and not self._local_active():
            self._try_load_disc()
        return True

    def _try_load_disc(self) -> None:
        kind = self._drive.disc_kind()
        label, mount = self._drive.label_and_mount()
        # Audio CDs have no filesystem, so there is nothing to wait for.
        if kind is not DiscKind.AUDIO and not mount and self._mount_waits < MOUNT_WAIT_POLLS:
            self._mount_waits += 1
            return
        self._load_pending = False
        is_video = Drive.is_dvd_video(mount)
        self._rip_options = rip_options(kind, is_video, self._drive.is_copy_protected())
        self._rip_btn.set_sensitive(self._rip_job is None)

        if kind is DiscKind.AUDIO:
            label = label or "Audio CD"
        self._disc_label = label or "Disc"
        self._disc_row.set_subtitle(self._disc_label)
        self._window_title.set_subtitle(self._disc_label)

        if kind is DiscKind.AUDIO:
            self._region_row.set_subtitle("—")
            self._load_audio_cd()
            return
        if mount and not is_video:
            self._region_row.set_subtitle("—")
            self._show_empty_page("Not a video DVD", "This disc has no VIDEO_TS folder. Use Rip to save a disc image.")
            return

        self._region_row.set_subtitle(Drive.region_text(mount) if mount else "Unknown")
        self._disc_info = Drive.title_info(mount) if mount else []
        # dvdsimple skips the disc menu; titles are picked from the sidebar instead.
        self._player.open(f"dvdsimple://{self._drive.vlc_path}")
        self._loaded = True
        self._autoplay_main = True
        self._stack.set_visible_child_name("player")

    def _load_audio_cd(self) -> None:
        tracks, leadout = self._drive.cd_tracks()
        self._audio_spans = audio_track_spans(tracks, leadout)
        if not self._audio_spans:
            self._show_empty_page("No audio tracks", "The disc's table of contents couldn't be read.")
            return
        self._set_cd_mode(True)
        self._cd_info.load(self._drive, tracks, leadout, self._audio_spans)
        self._build_cd_rows()
        self._refresh_cover()
        self._refresh_online_status()
        self._loaded = True
        self._stack.set_visible_child_name("player")
        self._play_cd_track(self._audio_spans[0][0])

    def _on_cd_info_changed(self) -> None:
        """New names or lyrics for the current disc (from cache, CD-Text, the network or the user)."""
        if self._local_album is not None:
            return  # a late callback from the disc that was playing before
        album = self._cd_info.album
        if album is not None and self._cd_info.named:
            for row, (number, _, _) in zip(self._title_rows, self._audio_spans, strict=False):
                row.set_title(f"{number}. {album.track(number).title}")
        if self._cd_track is not None:
            self._show_cd_track(self._cd_track)
        self._refresh_cover()
        self._refresh_online_status()
        if self._album_listener:
            self._album_listener()

    def _refresh_online_status(self) -> None:
        info = self._cd_info
        self._online_group.set_visible(self._cd_mode and self._local_album is None)
        headline, details = info.status()
        self._online_row.set_title(headline)
        self._online_row.set_subtitle(details)
        self._online_spinner.set_visible(info.busy)
        self._online_icon.set_visible(not info.busy)
        self._online_icon.set_from_icon_name("network-offline-symbolic" if info.failed else "emblem-ok-symbolic")
        self._retry_btn.set_visible(info.failed and not info.busy)

    def _set_cd_mode(self, on: bool) -> None:
        self._cd_mode = on
        self._cd_track = None
        self._media_stack.set_visible_child_name("cd" if on else "video")
        self._titles_group.set_title("Tracks" if on else "Titles")
        self._titles_group.set_description("Click a track to play it" if on else "Click a title or chapter to play it")
        # Audio CDs have a single stereo stream and no subtitles.
        self._audio_dd.set_visible(not on)
        self._subs_dd.set_visible(not on)
        self._lyrics_btn.set_visible(on)
        self._prev_btn.set_tooltip_text("Previous track" if on else "Previous chapter")
        self._next_btn.set_tooltip_text("Next track" if on else "Next chapter")

    def _build_cd_rows(self) -> None:
        self._clear_title_rows()
        names = self._album_names()
        for number in self._track_numbers():
            title = f"{number}. {names.track(number).title}" if names else f"Track {number}"
            length = self._track_length(number)
            row = Adw.ActionRow(title=title, subtitle=format_duration(length) if length else "", activatable=True)
            row.connect("activated", lambda _r, n=number: self._play_cd_track(n))
            marker = Gtk.Image(icon_name="media-playback-start-symbolic", opacity=0)
            marker.add_css_class("accent")
            row.add_prefix(marker)
            self._title_markers[number] = marker
            self._titles_group.add(row)
            self._title_rows.append(row)

    # Track lists come from the audio CD or from an album folder on disk; these hide which.

    def _track_numbers(self) -> list[int]:
        if self._local_album is not None:
            return self._local_album.album.numbers
        return [s[0] for s in self._audio_spans]

    def _album_names(self) -> AlbumInfo | None:
        """Real names, or None while the disc only has "Track N" placeholders."""
        if self._local_album is not None:
            return self._local_album.album.info
        info = self._cd_info.album
        return info if self._cd_info.named and info is not None else None

    def _track_length(self, number: int | None) -> int:
        if self._local_album is not None:
            return self._local_album.album.durations.get(number, 0)
        span = next((s for s in self._audio_spans if s[0] == number), None)
        return span[2] - span[1] if span else 0

    def _track_lyrics(self, number: int) -> Lyrics | None:
        if self._local_album is not None:
            return self._local_album.lyrics.get(number)
        return self._cd_info.lyrics(number)

    def _cover_path(self) -> Path | None:
        if self._local_album is not None:
            return self._local_album.cover
        return self._cd_info.cover_path if self._cd_mode else None

    def _play_cd_track(self, number: int) -> None:
        self._cd_track = number
        if self._local_album is not None:
            self._player.open(self._local_album.path(number).as_uri())
        else:
            self._player.open_cd_track(self._drive.vlc_path, number)
        self._show_cd_track(number)

    def _show_cd_track(self, number: int) -> None:
        info = self._album_names()
        track = info.track(number) if info else TrackInfo(number, f"Track {number}")
        album = " · ".join(p for p in (info.title, track.artist or info.artist) if p) if info else self._disc_label
        for n, marker in self._title_markers.items():
            marker.set_opacity(1 if n == number else 0)
        self._window_title.set_subtitle(f"{album} · {track.title}")
        self._show_lyrics(number)

    def _show_lyrics(self, number: int) -> None:
        lyrics = self._track_lyrics(number)
        self._lyrics_lines = parse_lrc(lyrics.synced) if lyrics and lyrics.synced else []
        self._lyrics_index = -2
        if self._lyrics_lines:
            self._lyrics_stack.set_visible_child_name("synced")
            self._update_lyric_line(self._player.time_ms())
        elif lyrics and lyrics.plain:
            self._plain_lyrics.set_label(lyrics.plain)
            self._lyrics_stack.set_visible_child_name("plain")
        elif self._local_album is not None:
            self._lyrics_note.set_label("")
            self._lyrics_stack.set_visible_child_name("note")
        else:
            info = self._cd_info
            unsettled = info.resolving or (info.named and number not in info.known_lyrics())
            if unsettled and info.busy:
                note = "Looking for lyrics…"
            elif unsettled and info.failed:
                note = "Lyrics couldn't be downloaded — see Album info in the sidebar"
            else:
                note = ""
            self._lyrics_note.set_label(note)
            self._lyrics_stack.set_visible_child_name("note")

    def _update_lyric_line(self, now_ms: int) -> None:
        index = line_index_at(self._lyrics_lines, now_ms)
        if index == self._lyrics_index:
            return
        self._lyrics_index = index
        for offset, label in zip((-1, 0, 1), self._lyric_labels, strict=True):
            i = index + offset
            label.set_label(self._lyrics_lines[i][1] if 0 <= i < len(self._lyrics_lines) else "")

    def _skip_back(self) -> None:
        if not self._cd_mode:
            self._player.previous_chapter()
        elif self._cd_track is not None:
            target = track_before(self._track_numbers(), self._cd_track, self._player.time_ms())
            if target == self._cd_track:
                self._player.seek(0)
            else:
                self._play_cd_track(target)

    def _skip_forward(self) -> None:
        if not self._cd_mode:
            self._player.next_chapter()
        elif self._cd_track is not None:
            target = track_after(self._track_numbers(), self._cd_track)
            if target is not None:
                self._play_cd_track(target)

    def _tick_cd(self) -> None:
        if self._player.is_ended() and self._cd_track is not None:
            target = track_after(self._track_numbers(), self._cd_track)
            if target is not None:
                self._play_cd_track(target)
            else:
                self._player.stop()  # end of disc: stay on the last track, ready to replay
        length = self._player.length_ms() or self._track_length(self._cd_track)
        now = self._player.time_ms()
        self._time_label.set_label(f"{format_duration(now)} / {format_duration(length)}")
        self._seek.set_range(0, max(length, 1))
        if time.monotonic() > self._seek_hold_until:
            self._seek.set_value(now)
        if self._lyrics_lines:
            self._update_lyric_line(now)

    def _unload_disc(self) -> None:
        if self._loaded:
            self._player.stop()
        self._set_cd_mode(False)  # also resets the sidebar heading a local album or video changed
        self._loaded = self._load_pending = False
        self._open_gen += 1  # drop an album folder still being read
        self._local_album = self._local_video = self._opening = None
        if self._local_work_dir is not None:
            shutil.rmtree(self._local_work_dir, ignore_errors=True)
            self._local_work_dir = None
        self._titles, self._disc_info = [], []
        self._last_position = None
        self._audio_spans = []
        self._cd_info.clear()
        self._lyrics_lines = []
        self._refresh_cover()
        self._refresh_online_status()
        self._rip_options = RipOptions((), "No disc")
        self._rip_btn.set_sensitive(False)
        self._clear_title_rows()
        self._set_tracks([], [])
        self._video.clear()
        self._disc_row.set_subtitle("—")
        self._region_row.set_subtitle("—")
        self._window_title.set_subtitle("")

    def _reload_disc(self) -> None:
        self._unload_disc()
        self._status = None
        self._poll_drive()

    def _local_active(self) -> bool:
        return self._local_album is not None or self._local_video is not None or self._opening is not None

    # ---------- files from disk ----------

    def _add_open_actions(self, app: Adw.Application) -> None:
        for name, handler, accel in (
            ("open-file", self._choose_file, "<Control>o"),
            ("open-folder", self._choose_folder, "<Control><Shift>o"),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, h=handler: h())
            self.add_action(action)
            app.set_accels_for_action(f"win.{name}", [accel])

    def _can_open(self) -> bool:
        if self._rip_job is not None:
            self._toasts.add_toast(Adw.Toast(title="Wait for the rip to finish"))
            return False
        return True

    def _choose_file(self) -> None:
        if not self._can_open():
            return
        media = Gtk.FileFilter(name="Music and videos")
        for extension in sorted(AUDIO_EXTENSIONS | VIDEO_EXTENSIONS):
            media.add_suffix(extension.lstrip("."))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(media)

        def chosen(dialog, result):
            try:
                picked = dialog.open_finish(result)
            except GLib.Error:
                return  # dismissed
            self._open_path(Path(picked.get_path()))

        Gtk.FileDialog(
            title="Open a Track or Video",
            filters=filters,
            default_filter=media,
            initial_folder=Gio.File.new_for_path(str(self._open_dir)),
        ).open(self, None, chosen)

    def _choose_folder(self) -> None:
        if not self._can_open():
            return

        def chosen(dialog, result):
            try:
                picked = dialog.select_folder_finish(result)
            except GLib.Error:
                return  # dismissed
            self._open_path(Path(picked.get_path()))

        Gtk.FileDialog(
            title="Open an Album Folder", initial_folder=Gio.File.new_for_path(str(self._open_dir))
        ).select_folder(self, None, chosen)

    def _on_drop(self, _target, value, _x, _y) -> bool:
        files = value.get_files()
        path = files[0].get_path() if files else None
        if path is None or not self._can_open():
            return False
        self._open_path(Path(path))
        return True

    def _open_path(self, path: Path) -> None:
        """A folder or an audio file opens the album (from that track); a video file plays on its own."""
        kind = "folder" if path.is_dir() else media_kind(path.name)
        if kind is None:
            self._toasts.add_toast(Adw.Toast(title=f"Can't play {path.name}"))
            return
        if kind == "video":
            self._open_video(path)
        else:
            self._open_album(path if kind == "folder" else path.parent, None if kind == "folder" else path.name)

    def _leave_current(self, folder: Path) -> None:
        self._unload_disc()
        self._open_dir = folder.parent if folder.is_dir() and folder.parent != folder else folder
        self._rip_btn.set_sensitive(False)  # Rip works on the disc, not on files
        if self._status is DriveStatus.DISC_OK:
            self._disc_row.set_subtitle("Not playing · Reload to play it")

    def _open_album(self, folder: Path, start_file: str | None) -> None:
        self._leave_current(folder)
        self._opening = folder
        gen = self._open_gen
        work_dir = self._local_work_dir = Path(tempfile.mkdtemp(prefix="dvd-player-album-"))
        self._show_empty_page(f"Opening {folder.name}…", "")

        def work() -> None:
            opened = read_album(folder, work_dir)
            GLib.idle_add(self._album_opened, gen, folder, start_file, opened)

        threading.Thread(target=work, daemon=True).start()

    def _album_opened(self, gen: int, folder: Path, start_file: str | None, opened: OpenedAlbum | None) -> bool:
        if gen != self._open_gen:
            return False
        self._opening = None
        if opened is None:
            self._show_empty_page("No music here", f"{folder} has no audio files. Use Reload to go back to the disc.")
            return False
        self._local_album = opened
        info = opened.album.info
        self._disc_label = info.title
        self._set_cd_mode(True)
        self._titles_group.set_title(info.title)
        self._titles_group.set_description(f"{_home_relative(folder)} · click a track to play it")
        self._build_cd_rows()
        self._refresh_cover()
        self._refresh_online_status()
        self._loaded = True
        self._stack.set_visible_child_name("player")
        files = opened.album.files
        start = next((n for n, name in files.items() if name == start_file), opened.album.numbers[0])
        self._play_cd_track(start)
        return False

    def _open_video(self, path: Path) -> None:
        self._leave_current(path.parent)
        self._local_video = path
        self._disc_label = path.stem
        self._window_title.set_subtitle(path.stem)
        self._titles_group.set_description(f"{_home_relative(path.parent)} · click a chapter to play it")
        self._player.open(path.as_uri())
        self._loaded = True
        self._stack.set_visible_child_name("player")

    def _show_empty_page(self, title: str, description: str) -> None:
        self._empty_page.set_title(title)
        self._empty_page.set_description(description)
        self._stack.set_visible_child_name("empty")

    # ---------- title list ----------

    def _clear_title_rows(self) -> None:
        for row in self._title_rows:
            self._titles_group.remove(row)
        self._title_rows, self._title_markers = [], {}

    def _build_title_rows(self) -> None:
        self._clear_title_rows()
        main = main_title(self._titles)
        for t in self._titles:
            chapters = self._player.chapters(t.index)
            parts = [format_duration(t.duration_ms) if t.duration_ms else "length unknown"]
            parts.append(f"{len(chapters)} chapters" if len(chapters) != 1 else "1 chapter")
            if main is not None and t.index == main.index and len(self._titles) > 1:
                parts.append("main")
            marker = Gtk.Image(icon_name="media-playback-start-symbolic", opacity=0)
            marker.add_css_class("accent")
            self._title_markers[t.index] = marker

            if len(chapters) > 1:
                row = Adw.ExpanderRow(title=t.label, subtitle=" · ".join(parts))
                play = Gtk.Button(
                    icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER, tooltip_text=f"Play {t.label}"
                )
                play.add_css_class("flat")
                play.connect("clicked", lambda _b, i=t.index: self._play(i))
                row.add_suffix(play)
                for c in chapters:
                    ch = Adw.ActionRow(title=f"Chapter {c.index + 1}", activatable=True)
                    ch.connect("activated", lambda _r, i=t.index, ci=c.index: self._play(i, ci))
                    row.add_row(ch)
            else:
                row = Adw.ActionRow(title=t.label, subtitle=" · ".join(parts), activatable=True)
                row.connect("activated", lambda _r, i=t.index: self._play(i))
            row.add_prefix(marker)
            self._titles_group.add(row)
            self._title_rows.append(row)

    def _play(self, title: int, chapter: int = 0) -> None:
        self._autoplay_main = False
        self._player.play_title(title, chapter)

    # ---------- periodic UI sync ----------

    def _tick(self) -> bool:
        self._ticks += 1
        if not self._loaded:
            return True

        if self._cd_mode:
            self._tick_cd()
        elif not self._titles:
            raw = self._player.titles()
            if self._local_video is not None:
                # One title named after the file; some formats report none, so fall back to the length.
                length = self._player.length_ms()
                raw = raw or ([Title(0, length)] if length else [])
                raw = [replace(t, name=self._local_video.stem) for t in raw] if len(raw) == 1 else raw
            if raw:
                self._titles = apply_disc_info(raw, self._disc_info)
                self._build_title_rows()
                main = main_title(self._titles)
                if self._autoplay_main and main and main.index != self._player.current_title():
                    self._player.play_title(main.index)
                self._autoplay_main = False

        if not self._cd_mode:
            self._sync_position()
        self._player.sync_volume()
        playing = self._player.is_playing()
        self._play_btn.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        if self._ticks % 4 == 0 and not self._cd_mode:
            self._set_tracks(self._player.audio_tracks(), self._player.subtitle_tracks())
        return True

    def _current_title(self) -> Title | None:
        cur = self._player.current_title()
        return self._titles[cur] if 0 <= cur < len(self._titles) else None

    def _sync_position(self) -> None:
        title = self._current_title()
        length = self._player.length_ms() or (title.duration_ms if title else 0)
        now = self._player.time_ms()
        self._time_label.set_label(f"{format_duration(now)} / {format_duration(length)}")
        self._seek.set_range(0, max(length, 1))
        if time.monotonic() > self._seek_hold_until:
            self._seek.set_value(now)

        position = (self._player.current_title(), self._player.current_chapter())
        if position != self._last_position and title is not None:
            self._last_position = position
            for idx, marker in self._title_markers.items():
                marker.set_opacity(1 if idx == title.index else 0)
            chapter = f" · Chapter {position[1] + 1}" if position[1] >= 0 else ""
            name = title.label if self._local_video else f"{self._disc_label} · {title.label}"
            self._window_title.set_subtitle(f"{name}{chapter}")

    def _set_tracks(self, audio: list[Track], subs: list[Track]) -> None:
        self._syncing_tracks = True
        try:
            if audio != self._audio:
                self._audio = audio
                self._fill_dropdown(self._audio_dd, audio, "No audio")
            if subs != self._subs:
                self._subs = subs
                self._fill_dropdown(self._subs_dd, subs, "No subtitles")
            if self._loaded:
                self._select_track(self._audio_dd, audio, self._player.audio_track())
                self._select_track(self._subs_dd, subs, self._player.subtitle_track())
        finally:
            self._syncing_tracks = False

    @staticmethod
    def _fill_dropdown(dd: Gtk.DropDown, tracks: list[Track], empty_label: str) -> None:
        dd.set_model(Gtk.StringList.new([t.name for t in tracks] or [empty_label]))
        dd.set_sensitive(bool(tracks))

    @staticmethod
    def _select_track(dd: Gtk.DropDown, tracks: list[Track], track_id: int) -> None:
        for i, t in enumerate(tracks):
            if t.id == track_id and dd.get_selected() != i:
                dd.set_selected(i)

    # ---------- video frames (VLC thread -> GTK main loop) ----------

    def _frame_from_vlc(self, data: bytes, width: int, height: int, stride: int) -> None:
        with self._frame_lock:
            already_queued = self._pending_frame is not None
            self._pending_frame = (data, width, height, stride)
        if not already_queued:
            GLib.idle_add(self._present_frame)

    def _present_frame(self) -> bool:
        with self._frame_lock:
            frame, self._pending_frame = self._pending_frame, None
        if frame is not None and self._loaded:
            data, width, height, stride = frame
            title = self._current_title()
            aspect = title.aspect if title and title.aspect else self._player.video_aspect(width, height)
            self._video.show_frame(data, width, height, stride, aspect)
        return False

    # ---------- ripping ----------

    def _rip_choices(self) -> list[tuple[str, RipKind, int]]:
        choices = []
        for kind in self._rip_options.kinds:
            if kind is RipKind.DVD_TITLE:
                for t in self._titles or self._player.titles():
                    length = f" · {format_duration(t.duration_ms)}" if t.duration_ms else ""
                    choices.append((f"{t.label}{length} → MKV", kind, t.index))
            elif kind is RipKind.AUDIO_TRACKS:
                choices.append((f"All {len(self._audio_spans)} tracks → FLAC", kind, -1))
            else:
                choices.append(("Whole disc image → ISO", kind, -1))
        return choices

    @staticmethod
    def _default_folder(kind: RipKind) -> Path:
        special = {
            RipKind.DVD_TITLE: GLib.UserDirectory.DIRECTORY_VIDEOS,
            RipKind.AUDIO_TRACKS: GLib.UserDirectory.DIRECTORY_MUSIC,
            RipKind.DISC_IMAGE: GLib.UserDirectory.DIRECTORY_DOWNLOAD,
        }[kind]
        return Path(GLib.get_user_special_dir(special) or GLib.get_home_dir())

    def _open_rip_dialog(self) -> None:
        choices = self._rip_choices()
        if not choices:
            alert = Adw.AlertDialog(heading="Can't rip this disc", body=self._rip_options.blocked_reason)
            alert.add_response("ok", "OK")
            alert.present(self)
            return

        audio = any(kind is RipKind.AUDIO_TRACKS for _, kind, _ in choices)
        dialog = Adw.Dialog(title="Rip to File", content_width=460, content_height=640 if audio else -1)
        state = {"folder": None}

        what = Adw.ComboRow(title="What", model=Gtk.StringList.new([c[0] for c in choices]))
        main = main_title(self._titles)
        for i, (_, kind, index) in enumerate(choices):
            if kind is RipKind.DVD_TITLE and main and index == main.index:
                what.set_selected(i)
        folder_row = Adw.ActionRow(title="Save to")
        folder_row.add_css_class("property")
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        folder_row.add_suffix(choose)

        def current_folder() -> Path:
            return state["folder"] or self._default_folder(choices[what.get_selected()][1])

        def refresh(*_):
            folder_row.set_subtitle(str(current_folder()))

        def on_folder_chosen(file_dialog, result):
            try:
                folder = file_dialog.select_folder_finish(result)
            except GLib.Error:
                return  # dismissed
            state["folder"] = Path(folder.get_path())
            refresh()

        choose.connect(
            "clicked",
            lambda *_: Gtk.FileDialog(
                title="Save rips to", initial_folder=Gio.File.new_for_path(str(current_folder()))
            ).select_folder(self, None, on_folder_chosen),
        )
        what.connect("notify::selected", refresh)
        refresh()

        group = Adw.PreferencesGroup(
            description="Copies streams as-is (no re-encoding), so quality is identical to the disc. "
            "Playback stops while ripping."
        )
        group.add(what)
        group.add(folder_row)

        album_form = self._album_form() if audio else None
        if album_form:
            dialog.connect("closed", lambda *_: setattr(self, "_album_listener", None))

        start = Gtk.Button(label="Start Rip", halign=Gtk.Align.CENTER, margin_bottom=18)
        start.add_css_class("pill")
        start.add_css_class("suggested-action")

        def on_start(*_):
            _, kind, index = choices[what.get_selected()]
            want_lyrics = False
            self._album_listener = None
            dialog.close()
            if album_form and kind is RipKind.AUDIO_TRACKS:
                album, want_lyrics, self._want_opus = album_form[1]()
                self._cd_info.set_album(album)
            self._start_rip(kind, index, current_folder(), want_lyrics)

        start.connect("clicked", on_start)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page = Adw.PreferencesPage()
        page.add(group)
        if album_form:
            for extra in album_form[0]:
                page.add(extra)
        body.append(page)
        body.append(start)
        toolbar = Adw.ToolbarView(content=body)
        toolbar.add_top_bar(Adw.HeaderBar())
        dialog.set_child(toolbar)
        dialog.present(self)

    def _album_form(self):
        """Editable album/track fields for an audio CD rip: (groups, read_values)."""
        info = self._cd_info.album
        album_group = Adw.PreferencesGroup(title="Album")
        album_row = Adw.EntryRow(title="Album")
        artist_row = Adw.EntryRow(title="Artist")
        year_row = Adw.EntryRow(title="Year", input_purpose=Gtk.InputPurpose.DIGITS)
        for row in (album_row, artist_row, year_row):
            album_group.add(row)
        lyrics_row = Adw.SwitchRow(
            title="Download lyrics",
            subtitle="From LRCLIB: saved in the FLAC tags, plus a synced .lrc file when available",
            active=True,
        )
        album_group.add(lyrics_row)
        opus_row = Adw.SwitchRow(
            title="Also make Opus copies",
            subtitle=f"Small files for phones: Opus {OPUS_BITRATE_KBPS} kb/s (≈1.2 MB/min, sounds the same), "
            "with tags, lyrics and cover, in a separate “(Opus)” folder. The FLACs stay lossless.",
            active=self._want_opus,
        )
        album_group.add(opus_row)
        cover_row = Adw.ActionRow(title="Cover")
        cover_thumb = Gtk.Picture(valign=Gtk.Align.CENTER)
        cover_thumb.add_css_class("card")
        cover_thumb.set_overflow(Gtk.Overflow.HIDDEN)
        cover_row.add_prefix(cover_thumb)
        choose_cover = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        cover_row.add_suffix(choose_cover)
        album_group.add(cover_row)
        cover_error = {"text": ""}

        def show_cover() -> None:
            path = self._cd_info.cover_path
            cover_thumb.set_paintable(load_texture(path, 48, 48))
            cover_thumb.set_visible(path is not None)
            source = self._cd_info.cover_source
            if cover_error["text"]:
                text = cover_error["text"]
            elif path is None:
                text = "None found — choose an image" if not self._cd_info.resolving else "Looking…"
            elif source == COVER_USER:
                text = "Your image · embedded in each FLAC and saved as cover.jpg/png"
            else:
                text = "From the Cover Art Archive · embedded in each FLAC and saved as cover.jpg"
            cover_row.set_subtitle(text)

        def on_cover_chosen(file_dialog, result):
            try:
                picked = file_dialog.open_finish(result)
            except GLib.Error:
                return  # dismissed
            cover_error["text"] = self._cd_info.set_cover(Path(picked.get_path())) or ""
            show_cover()

        def pick_cover(*_):
            images = Gtk.FileFilter(name="Images (JPEG, PNG)")
            for mime in ("image/jpeg", "image/png"):
                images.add_mime_type(mime)
            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(images)
            Gtk.FileDialog(title="Choose a cover image", filters=filters, default_filter=images).open(
                self, None, on_cover_chosen
            )

        choose_cover.connect("clicked", pick_cover)
        tracks_group = Adw.PreferencesGroup(title="Tracks")
        track_rows = {}
        for number, _, _ in self._audio_spans:
            track_rows[number] = Adw.EntryRow(title=f"Track {number}")
            tracks_group.add(track_rows[number])
        state = {"edited": False, "filling": False, "named": self._cd_info.named}

        def fill(src: AlbumInfo) -> None:
            state["filling"] = True
            album_row.set_text(src.title)
            artist_row.set_text(src.artist)
            year_row.set_text(src.year)
            for number, row in track_rows.items():
                row.set_text(src.track(number).title)
            state["filling"] = False

        def on_changed(*_):
            if not state["filling"]:
                state["edited"] = True

        def show_status() -> None:
            if self._cd_info.resolving:
                text = "Looking up the disc (CD-Text, then MusicBrainz)…"
            else:
                text = {
                    SOURCE_EDITED: "Your earlier edits for this disc. Change anything before ripping.",
                    SOURCE_CD_TEXT: "From the disc's own CD-Text. Edit anything before ripping.",
                    SOURCE_MUSICBRAINZ: "Found on MusicBrainz. Edit anything before ripping.",
                }.get(self._cd_info.source, "Not found on the disc or online: type the details in.")
            album_group.set_description(text + " Edits are remembered for this disc.")

        def on_info_changed() -> None:
            # Fill once when names arrive, and never over what the user already typed.
            if self._cd_info.named and not state["named"] and not state["edited"]:
                fill(self._cd_info.album)
            state["named"] = self._cd_info.named
            show_status()
            show_cover()

        fill(info)
        show_status()
        show_cover()
        for row in (album_row, artist_row, year_row, *track_rows.values()):
            row.connect("changed", on_changed)
        self._album_listener = on_info_changed

        def read() -> tuple[AlbumInfo, bool, bool]:
            current = self._cd_info.album or info
            artist = artist_row.get_text().strip() or "Unknown Artist"
            tracks = []
            for number, row in track_rows.items():
                old = current.track(number)
                # Per-track artists from MusicBrainz are kept unless they just repeated the album artist.
                track_artist = old.artist if old.artist and old.artist != current.artist else artist
                tracks.append(TrackInfo(number, row.get_text().strip() or f"Track {number:02d}", track_artist))
            album = AlbumInfo(
                title=album_row.get_text().strip() or "Unknown Album",
                artist=artist,
                year=year_row.get_text().strip(),
                tracks=tuple(tracks),
            )
            return album, lyrics_row.get_active(), opus_row.get_active()

        return [album_group, tracks_group], read

    def _start_rip(self, kind: RipKind, title_index: int, folder: Path, want_lyrics: bool = False) -> None:
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._toasts.add_toast(Adw.Toast(title=f"Can't use that folder: {e.strerror}"))
            return

        def free(name: str) -> str:
            return str(folder / unique_name(name, lambda n: (folder / n).exists()))

        base = safe_filename(self._disc_label)
        device = self._drive.device
        if kind is RipKind.DVD_TITLE:
            title = next((t for t in self._titles if t.index == title_index), None)
            out = free(f"{base} - {title.label if title else f'Title {title_index + 1}'}.mkv")
            job = FfmpegJob(self._on_rip_progress, self._on_rip_done, [out])
            what, self._rip_dest = f"Ripping {title.label if title else 'title'}", Path(out)
            steps = [(dvd_title_command(device, title_index + 1, out), title.duration_ms if title else 0)]
        elif kind is RipKind.AUDIO_TRACKS:
            info = self._cd_info.album
            album = Path(free(album_folder_name(info)))
            album.mkdir()
            opus_dir = Path(free(f"{album.name} (Opus)")) if self._want_opus else None
            if opus_dir:
                opus_dir.mkdir()
            self._rip_work_dir = Path(tempfile.mkdtemp(prefix="dvd-player-rip-"))
            cover = self._copy_cover(album)
            steps, partial = audiorip.plan(
                device, info, list(self._audio_spans), album, cover, self._cd_info.known_lyrics(),
                want_lyrics, opus_dir, self._rip_work_dir,
            )  # fmt: skip
            job = FfmpegJob(self._on_rip_progress, self._on_rip_done, partial)
            copies = " + Opus" if opus_dir else ""
            what, self._rip_dest = f"Ripping {len(self._audio_spans)} tracks{copies}", album
        else:
            out = free(f"{base}.iso")
            job = ImageJob(self._on_rip_progress, self._on_rip_done, [out])
            what, self._rip_dest = "Copying disc image", Path(out)

        # The drive can't serve VLC and the rip at the same time without crawling.
        self._player.stop()
        self._video.clear()
        self._set_busy(True)
        self._rip_job = job
        self._rip_row.set_title(what)
        self._rip_row.set_subtitle("starting…")
        self._rip_bar.set_fraction(0)
        self._rip_group.set_visible(True)
        if isinstance(job, ImageJob):
            job.start(self._drive.raw_path, out, self._drive.size_bytes())
        else:
            job.start(steps)

    def _copy_cover(self, album: Path) -> str:
        """cover.jpg/png in the album folder (what music players look for); "" when there is none."""
        source = self._cd_info.cover_path
        if source is None:
            return ""
        target = album / f"cover{source.suffix}"
        try:
            shutil.copyfile(source, target)
        except OSError:
            return ""
        return str(target)

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self._rip_btn, self._eject_btn, self._reload_btn, self._open_btn, self._titles_group, self._controls,
        ):  # fmt: skip
            widget.set_sensitive(not busy)

    def _on_rip_progress(self, fraction: float, text: str) -> bool:
        self._rip_bar.set_fraction(fraction)
        self._rip_row.set_subtitle(text)
        return False

    def _on_rip_done(self, ok: bool, message: str) -> bool:
        self._rip_job = None
        if self._rip_work_dir is not None:
            shutil.rmtree(self._rip_work_dir, ignore_errors=True)
            self._rip_work_dir = None
        self._rip_group.set_visible(False)
        self._set_busy(False)
        self._rip_btn.set_sensitive(
            self._status is DriveStatus.DISC_OK and not self._load_pending and not self._local_active()
        )
        if ok and self._rip_dest is not None:
            toast = Adw.Toast(title=f"Saved {self._rip_dest.name}", button_label="Open Folder", timeout=0)
            folder = self._rip_dest if self._rip_dest.is_dir() else self._rip_dest.parent
            toast.connect("button-clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None))
        else:
            toast = Adw.Toast(title=message, timeout=8)
        self._toasts.add_toast(toast)
        return False

    # ---------- event handlers ----------

    def _on_seek(self, _scale, _scroll, value: float) -> bool:
        self._seek_hold_until = time.monotonic() + 0.6
        self._player.seek(int(value))
        return False

    def _on_audio_selected(self, dd, _pspec) -> None:
        i = dd.get_selected()
        if not self._syncing_tracks and 0 <= i < len(self._audio):
            self._player.set_audio_track(self._audio[i].id)

    def _on_subs_selected(self, dd, _pspec) -> None:
        i = dd.get_selected()
        if not self._syncing_tracks and 0 <= i < len(self._subs):
            self._player.set_subtitle_track(self._subs[i].id)

    def _on_eject(self, *_args) -> None:
        if self._status is DriveStatus.TRAY_OPEN:
            self._drive.close_tray()
            return
        if not self._local_active():  # a file keeps playing while the disc comes out
            self._unload_disc()
            self._show_empty_page("Ejecting…", "")
        self._drive.eject()

    def _on_video_click(self, _gesture, n_press: int, _x, _y) -> None:
        if n_press == 2:
            self._toggle_fullscreen()

    def _toggle_fullscreen(self) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_fullscreen_changed(self, *_args) -> None:
        fullscreen = self.is_fullscreen()
        self._toolbar_view.set_reveal_top_bars(not fullscreen)
        self._split.set_show_sidebar(not fullscreen)

    def _on_key(self, _ctrl, keyval: int, _keycode: int, _state) -> bool:
        if keyval == Gdk.KEY_space:
            self._player.toggle_pause()
        elif keyval in (Gdk.KEY_l, Gdk.KEY_L) and self._cd_mode:
            self._lyrics_btn.set_active(not self._lyrics_btn.get_active())
        elif keyval in (Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_F11):
            self._toggle_fullscreen()
        elif keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.unfullscreen()
        elif keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            step = SEEK_STEP_MS if keyval == Gdk.KEY_Right else -SEEK_STEP_MS
            length = self._player.length_ms()
            self._player.seek(clamp(self._player.time_ms() + step, 0, length or 1 << 31))
        elif keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            step = VOLUME_STEP if keyval == Gdk.KEY_Up else -VOLUME_STEP
            self._volume.set_value(clamp(int(self._volume.get_value()) + step, 0, 100))
        else:
            return False
        return True

    def _on_close(self, *_args) -> bool:
        self.shutdown()
        return False

    def shutdown(self) -> None:
        """Stop VLC and any rip; safe to call twice (window close, then app shutdown)."""
        if self._rip_job is not None:
            self._rip_job.cancel()
        self._loaded = False
        self._player.release()
        if self._local_work_dir is not None:
            shutil.rmtree(self._local_work_dir, ignore_errors=True)


def _home_relative(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)
