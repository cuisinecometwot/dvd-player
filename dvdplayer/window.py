"""Main window: drive panel + title/chapter list in a sidebar, video and controls in the content area."""

import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from domain.disc import DiscKind, DriveStatus  # noqa: E402
from domain.media import Title, Track, apply_disc_info, clamp, format_duration, main_title  # noqa: E402
from domain.rip import (  # noqa: E402
    RipKind,
    RipOptions,
    audio_track_commands,
    audio_track_spans,
    dvd_title_command,
    rip_options,
    safe_filename,
    unique_name,
)
from dvdplayer.drive import Drive  # noqa: E402
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


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, device: str):
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
        self._audio_spans: list[tuple[int, int, int]] = []

        self._frame_lock = threading.Lock()
        self._pending_frame = None

        self._build_ui()
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
        self._eject_btn = Gtk.Button(icon_name="media-eject-symbolic", tooltip_text="Eject")
        self._eject_btn.connect("clicked", self._on_eject)
        header.pack_end(self._eject_btn)
        reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload disc")
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
        self._region_row = self._info_row(drive_group, "Disc region", "—")
        box.append(drive_group)

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
        self._empty_page = Adw.StatusPage(icon_name="media-optical-symbolic")
        self._video = VideoView()
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_video_click)
        self._video.add_controller(click)

        player_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        player_box.append(self._video)
        player_box.append(self._build_controls())

        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self._stack.add_named(self._empty_page, "empty")
        self._stack.add_named(player_box, "player")
        return self._stack

    def _build_controls(self) -> Gtk.Widget:
        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=8, margin_end=8)
        self._controls = bar

        def button(icon: str, tip: str, handler) -> Gtk.Button:
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_: handler())
            bar.append(b)
            return b

        button("media-skip-backward-symbolic", "Previous chapter", self._player.previous_chapter)
        self._play_btn = button("media-playback-start-symbolic", "Play/Pause (Space)", self._player.toggle_pause)
        button("media-skip-forward-symbolic", "Next chapter", self._player.next_chapter)

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
            if status is DriveStatus.DISC_OK:
                self._load_pending, self._mount_waits = True, 0
            else:
                self._unload_disc()
            if not self._loaded:
                self._show_empty_page(*EMPTY_PAGES[status])
        if self._load_pending:
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
            tracks, leadout = self._drive.cd_tracks()
            self._audio_spans = audio_track_spans(tracks, leadout)
            label = label or "Audio CD"
            total = self._audio_spans[-1][2] if self._audio_spans else 0
            self._show_empty_page(
                f"Audio CD · {len(self._audio_spans)} tracks · {format_duration(total)}",
                "Use Rip to save the tracks as FLAC files.",
            )
        self._disc_label = label or "Disc"
        self._disc_row.set_subtitle(self._disc_label)
        self._window_title.set_subtitle(self._disc_label)

        if kind is DiscKind.AUDIO:
            self._region_row.set_subtitle("—")
            return
        if mount and not is_video:
            self._region_row.set_subtitle("—")
            self._show_empty_page("Not a video DVD", "This disc has no VIDEO_TS folder. Use Rip to save a disc image.")
            return

        self._region_row.set_subtitle(Drive.region_text(mount) if mount else "Unknown")
        self._disc_info = Drive.title_info(mount) if mount else []
        # dvdsimple skips the disc menu; titles are picked from the sidebar instead.
        self._player.open(f"dvdsimple://{self._drive.device}")
        self._loaded = True
        self._autoplay_main = True
        self._stack.set_visible_child_name("player")

    def _unload_disc(self) -> None:
        if self._loaded:
            self._player.stop()
        self._loaded = self._load_pending = False
        self._titles, self._disc_info = [], []
        self._last_position = None
        self._audio_spans = []
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
            if main is not None and t.index == main.index:
                parts.append("main")
            marker = Gtk.Image(icon_name="media-playback-start-symbolic", visible=False)
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

        if not self._titles:
            raw = self._player.titles()
            if raw:
                self._titles = apply_disc_info(raw, self._disc_info)
                self._build_title_rows()
                main = main_title(self._titles)
                if self._autoplay_main and main and main.index != self._player.current_title():
                    self._player.play_title(main.index)
                self._autoplay_main = False

        self._sync_position()
        playing = self._player.is_playing()
        self._play_btn.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        if self._ticks % 4 == 0:
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
                marker.set_visible(idx == title.index)
            chapter = f" · Chapter {position[1] + 1}" if position[1] >= 0 else ""
            self._window_title.set_subtitle(f"{self._disc_label} · {title.label}{chapter}")

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

        dialog = Adw.Dialog(title="Rip to File", content_width=460)
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

        start = Gtk.Button(label="Start Rip", halign=Gtk.Align.CENTER, margin_bottom=18)
        start.add_css_class("pill")
        start.add_css_class("suggested-action")

        def on_start(*_):
            _, kind, index = choices[what.get_selected()]
            dialog.close()
            self._start_rip(kind, index, current_folder())

        start.connect("clicked", on_start)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page = Adw.PreferencesPage()
        page.add(group)
        body.append(page)
        body.append(start)
        toolbar = Adw.ToolbarView(content=body)
        toolbar.add_top_bar(Adw.HeaderBar())
        dialog.set_child(toolbar)
        dialog.present(self)

    def _start_rip(self, kind: RipKind, title_index: int, folder: Path) -> None:
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
            album = Path(free(base))
            album.mkdir()
            job = FfmpegJob(self._on_rip_progress, self._on_rip_done, [str(album / "Track *.flac")])
            what, self._rip_dest = f"Ripping {len(self._audio_spans)} tracks", album
            steps = audio_track_commands(device, self._audio_spans, str(album / "Track {:02d}.flac"))
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
            job.start(device, out, self._drive.size_bytes())
        else:
            job.start(steps)

    def _set_busy(self, busy: bool) -> None:
        for widget in (self._rip_btn, self._eject_btn, self._reload_btn, self._titles_group, self._controls):
            widget.set_sensitive(not busy)

    def _on_rip_progress(self, fraction: float, text: str) -> bool:
        self._rip_bar.set_fraction(fraction)
        self._rip_row.set_subtitle(text)
        return False

    def _on_rip_done(self, ok: bool, message: str) -> bool:
        self._rip_job = None
        self._rip_group.set_visible(False)
        self._set_busy(False)
        self._rip_btn.set_sensitive(self._status is DriveStatus.DISC_OK and not self._load_pending)
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
