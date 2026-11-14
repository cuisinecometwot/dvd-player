"""GTK/libadwaita main window: drive panel + title/chapter list in a sidebar, video and controls in the content.

All decisions live in dvdplayer.session.Session; this file builds widgets, forwards the user's actions to the
session and implements its View.
"""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from domain.cdmeta import AlbumInfo, edited_album  # noqa: E402
from domain.disc import DriveStatus  # noqa: E402
from domain.housekeeping import format_bytes  # noqa: E402
from domain.library import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS  # noqa: E402
from domain.media import Track, format_duration  # noqa: E402
from domain.rip import OPUS_BITRATE_KBPS, RipKind  # noqa: E402
from dvdplayer import appdata, applog  # noqa: E402
from dvdplayer.gtkui.images import load_texture  # noqa: E402
from dvdplayer.gtkui.video_view import VideoView  # noqa: E402
from dvdplayer.ripform import album_status_text, cover_text  # noqa: E402
from dvdplayer.session import (  # noqa: E402
    DRIVE_POLL_S,
    SEEK_STEP_MS,
    TICK_MS,
    VOLUME_STEP,
    Session,
    TitleRow,
    TrackRow,
)

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
        self._session = Session(self, device)
        self._title_markers: dict[int, Gtk.Image] = {}
        self._title_rows: dict[int, Gtk.Widget] = {}
        self._audio: list[Track] = []
        self._subs: list[Track] = []
        self._syncing_tracks = False

        css = Gtk.CssProvider()
        css.load_from_string(LYRICS_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._build_ui()
        self._add_actions(app)
        self.connect("close-request", self._on_close)
        self.connect("notify::fullscreened", self._on_fullscreen_changed)

        self._session.poll_drive()
        GLib.timeout_add_seconds(DRIVE_POLL_S, lambda: self._session.poll_drive() or True)
        GLib.timeout_add(TICK_MS, lambda: self._session.tick() or True)

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
        main_menu = Gio.Menu()
        main_menu.append("Clear Saved Data…", "win.clear-data")
        main_menu.append("Open Log Folder", "win.open-logs")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Main menu", menu_model=main_menu))
        self._eject_btn = Gtk.Button(icon_name="media-eject-symbolic", tooltip_text="Eject")
        self._eject_btn.connect("clicked", lambda *_: self._session.eject_or_close())
        header.pack_end(self._eject_btn)
        self._reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Reload disc (back to the disc)")
        self._reload_btn.connect("clicked", lambda *_: self._session.reload_disc())
        header.pack_end(self._reload_btn)
        self._rip_btn = Gtk.Button(
            child=Adw.ButtonContent(icon_name="document-save-symbolic", label="Rip"),
            tooltip_text="Rip disc to file…",
            sensitive=False,
        )
        self._rip_btn.connect("clicked", lambda *_: self._open_rip_dialog())
        header.pack_end(self._rip_btn)

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
        self._drive_row = self._info_row(drive_group, "Drive", self._session.drive.name)
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
                f"artist and track lengths are sent. Results are kept in {appdata.cache_root_display()}, "
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
        self._retry_btn.connect("clicked", lambda *_: self._session.retry_online())
        self._online_row.add_suffix(self._retry_btn)
        self._online_group.add(self._online_row)
        box.append(self._online_group)

        self._titles_group = Adw.PreferencesGroup(title="Titles", description="Click a title or chapter to play it")
        box.append(self._titles_group)

        self._rip_group = Adw.PreferencesGroup(title="Ripping", visible=False)
        self._rip_row = Adw.ActionRow(title="Ripping…")
        cancel = Gtk.Button(icon_name="process-stop-symbolic", tooltip_text="Cancel rip", valign=Gtk.Align.CENTER)
        cancel.add_css_class("flat")
        cancel.connect("clicked", lambda *_: self._session.cancel_rip())
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
        session = self._session

        def button(icon: str, tip: str, handler) -> Gtk.Button:
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.connect("clicked", lambda *_: handler())
            bar.append(b)
            return b

        self._prev_btn = button("media-skip-backward-symbolic", "Previous chapter", session.skip_back)
        self._play_btn = button("media-playback-start-symbolic", "Play/Pause (Space)", session.toggle_pause)
        self._next_btn = button("media-skip-forward-symbolic", "Next chapter", session.skip_forward)

        self._time_label = Gtk.Label(label="0:00 / 0:00", width_chars=17)
        self._time_label.add_css_class("numeric")
        bar.append(self._time_label)

        self._seek = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True, draw_value=False)
        self._seek.set_range(0, 1)
        self._seek.connect("change-value", lambda _s, _scroll, value: session.seek(int(value)) or False)
        bar.append(self._seek)

        self._audio_dd = Gtk.DropDown(tooltip_text="Audio track", valign=Gtk.Align.CENTER)
        self._audio_dd.connect(
            "notify::selected", lambda dd, _p: self._syncing_tracks or session.select_audio(dd.get_selected())
        )
        bar.append(self._audio_dd)
        self._subs_dd = Gtk.DropDown(tooltip_text="Subtitles", valign=Gtk.Align.CENTER)
        self._subs_dd.connect(
            "notify::selected", lambda dd, _p: self._syncing_tracks or session.select_subtitles(dd.get_selected())
        )
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
        self._volume.connect("value-changed", lambda _b, v: session.set_volume(int(v)))
        bar.append(self._volume)

        button("view-fullscreen-symbolic", "Fullscreen (F)", self._toggle_fullscreen)
        return bar

    # ---------- View: what the session shows ----------

    def show_drive_status(self, status: DriveStatus) -> None:
        self._status_row.set_subtitle(status.label)
        tray_open = status is DriveStatus.TRAY_OPEN
        self._eject_btn.set_icon_name("go-up-symbolic" if tray_open else "media-eject-symbolic")
        self._eject_btn.set_tooltip_text("Close tray" if tray_open else "Eject")

    def show_empty(self, title: str, description: str) -> None:
        self._empty_page.set_title(title)
        self._empty_page.set_description(description)
        self._stack.set_visible_child_name("empty")

    def show_player(self) -> None:
        self._stack.set_visible_child_name("player")

    def set_cd_mode(self, on: bool) -> None:
        self._media_stack.set_visible_child_name("cd" if on else "video")
        self.set_list_heading(
            "Tracks" if on else "Titles", "Click a track to play it" if on else "Click a title or chapter to play it"
        )
        # Audio CDs have a single stereo stream and no subtitles.
        self._audio_dd.set_visible(not on)
        self._subs_dd.set_visible(not on)
        self._lyrics_btn.set_visible(on)
        self._prev_btn.set_tooltip_text("Previous track" if on else "Previous chapter")
        self._next_btn.set_tooltip_text("Next track" if on else "Next chapter")

    def set_list_heading(self, title: str, description: str) -> None:
        self._titles_group.set_title(title)
        self._titles_group.set_description(description)

    def set_disc_text(self, text: str) -> None:
        self._disc_row.set_subtitle(text)

    def set_region_text(self, text: str) -> None:
        self._region_row.set_subtitle(text)

    def set_subtitle(self, text: str) -> None:
        self._window_title.set_subtitle(text)

    def set_rip_enabled(self, enabled: bool) -> None:
        self._rip_btn.set_sensitive(enabled)

    def set_busy(self, busy: bool) -> None:
        for widget in (
            self._rip_btn, self._eject_btn, self._reload_btn, self._open_btn, self._titles_group, self._controls,
        ):  # fmt: skip
            widget.set_sensitive(not busy)

    def notify(self, text: str, action_label: str = "", action=None, timeout_s: int | None = None) -> None:
        toast = Adw.Toast(title=text)
        if timeout_s is not None:
            toast.set_timeout(timeout_s)
        if action_label and action is not None:
            toast.set_button_label(action_label)
            toast.connect("button-clicked", lambda *_: action())
        self._toasts.add_toast(toast)

    def show_title_rows(self, rows: list[TitleRow]) -> None:
        self.clear_rows()
        for t in rows:
            marker = Gtk.Image(icon_name="media-playback-start-symbolic", opacity=0)
            marker.add_css_class("accent")
            self._title_markers[t.index] = marker
            if t.chapters > 1:
                row = Adw.ExpanderRow(title=t.label, subtitle=t.details)
                play = Gtk.Button(
                    icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER, tooltip_text=f"Play {t.label}"
                )
                play.add_css_class("flat")
                play.connect("clicked", lambda _b, i=t.index: self._session.play(i))
                row.add_suffix(play)
                for c in range(t.chapters):
                    ch = Adw.ActionRow(title=f"Chapter {c + 1}", activatable=True)
                    ch.connect("activated", lambda _r, i=t.index, ci=c: self._session.play(i, ci))
                    row.add_row(ch)
            else:
                row = Adw.ActionRow(title=t.label, subtitle=t.details, activatable=True)
                row.connect("activated", lambda _r, i=t.index: self._session.play(i))
            row.add_prefix(marker)
            self._titles_group.add(row)
            self._title_rows[t.index] = row

    def show_track_rows(self, rows: list[TrackRow]) -> None:
        self.clear_rows()
        for t in rows:
            subtitle = format_duration(t.length_ms) if t.length_ms else ""
            row = Adw.ActionRow(title=t.label, subtitle=subtitle, activatable=True)
            row.connect("activated", lambda _r, n=t.number: self._session.play_track(n))
            marker = Gtk.Image(icon_name="media-playback-start-symbolic", opacity=0)
            marker.add_css_class("accent")
            row.add_prefix(marker)
            self._title_markers[t.number] = marker
            self._titles_group.add(row)
            self._title_rows[t.number] = row

    def rename_track_rows(self, labels: dict[int, str]) -> None:
        for number, label in labels.items():
            row = self._title_rows.get(number)
            if row is not None:
                row.set_title(label)

    def clear_rows(self) -> None:
        for row in self._title_rows.values():
            self._titles_group.remove(row)
        self._title_rows, self._title_markers = {}, {}

    def mark_current_row(self, key: int | None) -> None:
        for k, marker in self._title_markers.items():
            marker.set_opacity(1 if k == key else 0)

    def show_cover(self, path: Path | None, thumbnail: bool) -> None:
        # Large enough to stay sharp in fullscreen on a HiDPI screen; the picture scales it down.
        big = load_texture(path, 1600, 1600)
        self._cover_picture.set_paintable(big)
        if big is not None:
            self._cover_frame.set_ratio(big.get_width() / big.get_height())
        self._cover_frame.set_visible(big is not None)
        self._cd_icon.set_visible(big is None)
        small = load_texture(path, 40, 40) if thumbnail else None
        self._disc_thumb.set_paintable(small)
        self._disc_thumb.set_visible(small is not None)

    def show_online(self, visible: bool, headline: str, details: str, busy: bool, failed: bool) -> None:
        self._online_group.set_visible(visible)
        self._online_row.set_title(headline)
        self._online_row.set_subtitle(details)
        self._online_spinner.set_visible(busy)
        self._online_icon.set_visible(not busy)
        self._online_icon.set_from_icon_name("network-offline-symbolic" if failed else "emblem-ok-symbolic")
        self._retry_btn.set_visible(failed and not busy)

    def show_synced_lyrics(self, lines: tuple[str, str, str]) -> None:
        for label, text in zip(self._lyric_labels, lines, strict=True):
            label.set_label(text)
        self._lyrics_stack.set_visible_child_name("synced")

    def show_plain_lyrics(self, text: str) -> None:
        self._plain_lyrics.set_label(text)
        self._lyrics_stack.set_visible_child_name("plain")

    def show_lyrics_note(self, text: str) -> None:
        self._lyrics_note.set_label(text)
        self._lyrics_stack.set_visible_child_name("note")

    def show_time(self, now_ms: int, length_ms: int, move_slider: bool) -> None:
        self._time_label.set_label(f"{format_duration(now_ms)} / {format_duration(length_ms)}")
        self._seek.set_range(0, max(length_ms, 1))
        if move_slider:
            self._seek.set_value(now_ms)

    def show_playing(self, playing: bool) -> None:
        self._play_btn.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")

    def show_track_menus(self, audio: list[Track], subs: list[Track], audio_id: int, subs_id: int) -> None:
        self._syncing_tracks = True
        try:
            if audio != self._audio:
                self._audio = audio
                self._fill_dropdown(self._audio_dd, audio, "No audio")
            if subs != self._subs:
                self._subs = subs
                self._fill_dropdown(self._subs_dd, subs, "No subtitles")
            self._select_track(self._audio_dd, audio, audio_id)
            self._select_track(self._subs_dd, subs, subs_id)
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

    def show_frame(self, data: bytes, width: int, height: int, stride: int, aspect: float) -> None:
        self._video.show_frame(data, width, height, stride, aspect)

    def clear_video(self) -> None:
        self._video.clear()

    def show_rip_progress(self, title: str, fraction: float, text: str) -> None:
        self._rip_row.set_title(title)
        self._rip_row.set_subtitle(text)
        self._rip_bar.set_fraction(fraction)
        self._rip_group.set_visible(True)

    def hide_rip_progress(self) -> None:
        self._rip_group.set_visible(False)

    def show_volume(self, percent: int) -> None:
        self._volume.set_value(percent)

    def open_folder(self, folder: Path) -> None:
        Gio.AppInfo.launch_default_for_uri(folder.as_uri(), None)

    # ---------- actions ----------

    def _add_actions(self, app: Adw.Application) -> None:
        for name, handler, accel in (
            ("open-file", self._choose_file, "<Control>o"),
            ("open-folder", self._choose_folder, "<Control><Shift>o"),
            ("clear-data", self._confirm_clear_data, None),
            ("open-logs", self._open_log_folder, None),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, h=handler: h())
            self.add_action(action)
            if accel:
                app.set_accels_for_action(f"win.{name}", [accel])

    def _confirm_clear_data(self) -> None:
        target = self._session.clearable_data()
        if target is None:
            return
        alert = Adw.AlertDialog(
            heading="Clear saved data?",
            body=f"Deletes {format_bytes(target.size)}: album names (including your edits), covers and lyrics "
            "saved for your CDs, leftover temporary files, and older log files.\n\n"
            "Your rips are not touched. CDs are looked up online again the next time they play.",
        )
        alert.add_response("cancel", "Cancel")
        alert.add_response("clear", "Clear")
        alert.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.connect("response", lambda _a, response: response == "clear" and self._session.clear_data(target))
        alert.present(self)

    def _open_log_folder(self) -> None:
        folder = applog.log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        self.open_folder(folder)

    # ---------- files from disk ----------

    def _choose_file(self) -> None:
        if not self._session.can_open():
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
            self._session.open_path(Path(picked.get_path()))

        Gtk.FileDialog(
            title="Open a Track or Video",
            filters=filters,
            default_filter=media,
            initial_folder=Gio.File.new_for_path(str(self._session.open_dir)),
        ).open(self, None, chosen)

    def _choose_folder(self) -> None:
        if not self._session.can_open():
            return

        def chosen(dialog, result):
            try:
                picked = dialog.select_folder_finish(result)
            except GLib.Error:
                return  # dismissed
            self._session.open_path(Path(picked.get_path()))

        Gtk.FileDialog(
            title="Open an Album Folder", initial_folder=Gio.File.new_for_path(str(self._session.open_dir))
        ).select_folder(self, None, chosen)

    def _on_drop(self, _target, value, _x, _y) -> bool:
        files = value.get_files()
        path = files[0].get_path() if files else None
        if path is None or not self._session.can_open():
            return False
        self._session.open_path(Path(path))
        return True

    # ---------- ripping ----------

    def _open_rip_dialog(self) -> None:
        session = self._session
        choices = session.rip_choices()
        if not choices:
            alert = Adw.AlertDialog(heading="Can't rip this disc", body=session.rip_blocked_reason)
            alert.add_response("ok", "OK")
            alert.present(self)
            return

        audio = any(c.kind is RipKind.AUDIO_TRACKS for c in choices)
        dialog = Adw.Dialog(title="Rip to File", content_width=460, content_height=640 if audio else -1)
        state = {"folder": None}

        what = Adw.ComboRow(title="What", model=Gtk.StringList.new([c.label for c in choices]))
        what.set_selected(session.default_choice(choices))
        folder_row = Adw.ActionRow(title="Save to")
        folder_row.add_css_class("property")
        choose = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        folder_row.add_suffix(choose)

        def current_folder() -> Path:
            return state["folder"] or session.default_rip_folder(choices[what.get_selected()].kind)

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
            dialog.connect("closed", lambda *_: setattr(session, "album_listener", None))

        start = Gtk.Button(label="Start Rip", halign=Gtk.Align.CENTER, margin_bottom=18)
        start.add_css_class("pill")
        start.add_css_class("suggested-action")

        def on_start(*_):
            choice = choices[what.get_selected()]
            album, want_lyrics = None, False
            session.album_listener = None
            dialog.close()
            if album_form and choice.kind is RipKind.AUDIO_TRACKS:
                album, want_lyrics, session.want_opus = album_form[1]()
            session.start_rip(choice, current_folder(), album=album, want_lyrics=want_lyrics)

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
        cd_info = self._session.cd_info
        info = cd_info.album
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
            active=self._session.want_opus,
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
            path = cd_info.cover_path
            cover_thumb.set_paintable(load_texture(path, 48, 48))
            cover_thumb.set_visible(path is not None)
            cover_row.set_subtitle(cover_text(cd_info, cover_error["text"]))

        def on_cover_chosen(file_dialog, result):
            try:
                picked = file_dialog.open_finish(result)
            except GLib.Error:
                return  # dismissed
            cover_error["text"] = cd_info.set_cover(Path(picked.get_path())) or ""
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
        for number, _, _ in self._session.audio_spans:
            track_rows[number] = Adw.EntryRow(title=f"Track {number}")
            tracks_group.add(track_rows[number])
        state = {"edited": False, "filling": False, "named": cd_info.named}

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

        def on_info_changed() -> None:
            # Fill once when names arrive, and never over what the user already typed.
            if cd_info.named and not state["named"] and not state["edited"]:
                fill(cd_info.album)
            state["named"] = cd_info.named
            album_group.set_description(album_status_text(cd_info))
            show_cover()

        fill(info)
        album_group.set_description(album_status_text(cd_info))
        show_cover()
        for row in (album_row, artist_row, year_row, *track_rows.values()):
            row.connect("changed", on_changed)
        self._session.album_listener = on_info_changed

        def read() -> tuple[AlbumInfo, bool, bool]:
            album = edited_album(
                cd_info.album or info,
                album_row.get_text(),
                artist_row.get_text(),
                year_row.get_text(),
                {n: row.get_text() for n, row in track_rows.items()},
            )
            return album, lyrics_row.get_active(), opus_row.get_active()

        return [album_group, tracks_group], read

    # ---------- event handlers ----------

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
        session = self._session
        if keyval == Gdk.KEY_space:
            session.toggle_pause()
        elif keyval in (Gdk.KEY_l, Gdk.KEY_L) and session.cd_mode:
            self._lyrics_btn.set_active(not self._lyrics_btn.get_active())
        elif keyval in (Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_F11):
            self._toggle_fullscreen()
        elif keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.unfullscreen()
        elif keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            session.seek_by(SEEK_STEP_MS if keyval == Gdk.KEY_Right else -SEEK_STEP_MS)
        elif keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            session.change_volume(VOLUME_STEP if keyval == Gdk.KEY_Up else -VOLUME_STEP)
        else:
            return False
        return True

    def _on_close(self, *_args) -> bool:
        self.shutdown()
        return False

    def shutdown(self) -> None:
        self._session.shutdown()
