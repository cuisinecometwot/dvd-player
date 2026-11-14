"""Qt main window (the Windows UI): menus and toolbar, a sidebar with the drive and the title/track list, and
video or the CD page with controls. Decisions live in dvdplayer.session.Session; this implements its View."""

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStyle,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from domain.disc import DriveStatus
from domain.housekeeping import format_bytes
from domain.library import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from domain.media import Track, format_duration
from dvdplayer import appdata, applog
from dvdplayer.qtui.ripdialog import RipDialog
from dvdplayer.qtui.widgets import CoverView, Notifier, VideoWidget, dim, scaled_pixmap, theme_icon
from dvdplayer.session import (
    DRIVE_POLL_S,
    SEEK_STEP_MS,
    TICK_MS,
    VOLUME_STEP,
    Session,
    TitleRow,
    TrackRow,
)

ROW_KEY = Qt.ItemDataRole.UserRole  # (title index or track number, chapter index or -1)
SIDEBAR_WIDTH = 320


class MainWindow(QMainWindow):
    def __init__(self, device: str | None, native_video: bool):
        super().__init__()
        self.setWindowTitle("DVD Player")
        self.resize(1180, 720)
        self.setAcceptDrops(True)
        self._native_video = native_video
        self._session = Session(self, device, render_frames=not native_video)
        self._rows: dict[int, QTreeWidgetItem] = {}
        self._current_row: int | None = None
        self._audio: list[Track] = []
        self._subs: list[Track] = []
        self._syncing = False
        self._subtitle = ""
        self._dragging_seek = False
        self._closed = False

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._build_sidebar())
        self._splitter.addWidget(self._build_content())
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([SIDEBAR_WIDTH, 1180 - SIDEBAR_WIDTH])
        self.setCentralWidget(self._splitter)
        self._notifier = Notifier(self.statusBar())
        self._build_shortcuts()

    def start(self) -> None:
        """After show(): the video widget has its native window, so VLC can be given it before any playback."""
        if self._native_video:
            self._session.player.set_window(self._video.window_handle())
        self._session.poll_drive()
        self._poll_timer = QTimer(self, interval=DRIVE_POLL_S * 1000, timeout=self._session.poll_drive)
        self._tick_timer = QTimer(self, interval=TICK_MS, timeout=self._session.tick)
        self._poll_timer.start()
        self._tick_timer.start()

    # ---------- construction ----------

    def _action(self, text: str, icon: str, slot, shortcut: str | None = None) -> QAction:
        action = QAction(theme_icon(icon), text, self)
        action.triggered.connect(lambda _checked=False: slot())
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        return action

    def _build_actions(self) -> None:
        s = self._session
        self._open_file_act = self._action("Open &File…", "DocumentOpen", self._choose_file, "Ctrl+O")
        self._open_folder_act = self._action("Open Album &Folder…", "FolderOpen", self._choose_folder, "Ctrl+Shift+O")
        self._rip_act = self._action("&Rip…", "DocumentSave", self._open_rip_dialog)
        self._rip_act.setEnabled(False)
        self._reload_act = self._action("Re&load Disc", "ViewRefresh", s.reload_disc, "F5")
        self._eject_act = self._action("&Eject", "MediaEject", s.eject_or_close)
        self._clear_act = self._action("&Clear Saved Data…", "EditClear", self._confirm_clear_data)
        self._logs_act = self._action("Open &Log Folder", "FolderOpen", self._open_log_folder)
        self._fullscreen_act = self._action("&Full Screen", "ViewFullscreen", self._toggle_fullscreen, "F11")
        self._sidebar_act = QAction("&Sidebar", self, checkable=True, checked=True)
        self._sidebar_act.toggled.connect(lambda on: self._splitter.widget(0).setVisible(on))
        self._lyrics_act = QAction("L&yrics", self, checkable=True, checked=True)
        self._lyrics_act.setToolTip("Lyrics (L)")
        self._lyrics_act.toggled.connect(lambda on: self._lyrics_stack.setVisible(on))

    def _build_menus(self) -> None:
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")
        file_menu.addActions([self._open_file_act, self._open_folder_act])
        file_menu.addSeparator()
        file_menu.addAction(self._action("E&xit", "ApplicationExit", self.close, "Alt+F4"))
        disc_menu = bar.addMenu("&Disc")
        disc_menu.addActions([self._rip_act, self._reload_act, self._eject_act])
        view_menu = bar.addMenu("&View")
        view_menu.addActions([self._sidebar_act, self._lyrics_act, self._fullscreen_act])
        tools_menu = bar.addMenu("&Tools")
        tools_menu.addActions([self._clear_act, self._logs_act])

    def _build_toolbar(self) -> None:
        self._toolbar = QToolBar("Main", movable=False, floatable=False)
        self._toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toolbar.setIconSize(QSize(18, 18))
        open_button = QToolButton(text="Open", icon=theme_icon("DocumentOpen"))
        open_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        open_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        open_button.addActions([self._open_folder_act, self._open_file_act])
        open_button.setToolTip("Play a ripped album or video from disk")
        self._open_button = open_button
        self._toolbar.addWidget(open_button)
        self._toolbar.addSeparator()
        self._toolbar.addActions([self._rip_act, self._reload_act, self._eject_act])
        self.addToolBar(self._toolbar)

    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 4, 8)

        drive_box = QGroupBox("Drive")
        form = QFormLayout(drive_box)
        self._drive_label = QLabel(self._session.drive.name, wordWrap=True)
        self._status_label = QLabel("—")
        self._disc_label = QLabel("—", wordWrap=True)
        self._disc_thumb = QLabel()
        self._disc_thumb.hide()
        disc_row = QHBoxLayout()
        disc_row.addWidget(self._disc_thumb)
        disc_row.addWidget(self._disc_label, 1)
        self._region_label = QLabel("—")
        form.addRow("Drive:", self._drive_label)
        form.addRow("Status:", self._status_label)
        form.addRow("Disc:", disc_row)
        form.addRow("Region:", self._region_label)
        for label in (self._drive_label, self._disc_label):
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(drive_box)

        self._rip_box = QGroupBox("Ripping")
        rip_layout = QVBoxLayout(self._rip_box)
        self._rip_title = QLabel(wordWrap=True)
        self._rip_bar = QProgressBar(textVisible=False)
        self._rip_text = dim(QLabel())
        cancel = QPushButton(theme_icon("ProcessStop"), "Cancel")
        cancel.clicked.connect(self._session.cancel_rip)
        rip_row = QHBoxLayout()
        rip_row.addWidget(self._rip_text, 1)
        rip_row.addWidget(cancel)
        rip_layout.addWidget(self._rip_title)
        rip_layout.addWidget(self._rip_bar)
        rip_layout.addLayout(rip_row)
        self._rip_box.hide()
        layout.addWidget(self._rip_box)

        self._online_box = QGroupBox("Album info")
        online = QVBoxLayout(self._online_box)
        self._online_headline = QLabel(wordWrap=True)
        bold = QFont(self._online_headline.font())
        bold.setBold(True)
        self._online_headline.setFont(bold)
        self._online_details = dim(QLabel(wordWrap=True))
        self._online_busy = QProgressBar(minimum=0, maximum=0, textVisible=False, maximumHeight=6)
        self._retry_button = QPushButton(theme_icon("ViewRefresh"), "Retry")
        self._retry_button.clicked.connect(self._session.retry_online)
        online.addWidget(self._online_headline)
        online.addWidget(self._online_details)
        online.addWidget(self._online_busy)
        online.addWidget(self._retry_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._online_box.setToolTip(
            "Names, lyrics and covers come from the disc's CD-Text when it has any, otherwise from MusicBrainz, "
            "LRCLIB and the Cover Art Archive. Only the disc ID, barcode, song titles, artist and track lengths "
            f"are sent. Results are kept in {appdata.cache_root_display()}, so a disc is only looked up once."
        )
        self._online_box.hide()
        layout.addWidget(self._online_box)

        self._list_box = QGroupBox("Titles")
        list_layout = QVBoxLayout(self._list_box)
        self._list_hint = dim(QLabel("Click a title or chapter to play it", wordWrap=True))
        self._tree = QTreeWidget(headerHidden=True, columnCount=2, rootIsDecorated=True, uniformRowHeights=True)
        self._tree.itemClicked.connect(self._on_row_clicked)
        self._tree.itemActivated.connect(self._on_row_clicked)
        list_layout.addWidget(self._list_hint)
        list_layout.addWidget(self._tree, 1)
        layout.addWidget(self._list_box, 1)
        panel.setMinimumWidth(240)
        return panel

    def _build_content(self) -> QWidget:
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        icon = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(theme_icon("MediaOptical").pixmap(96, 96))
        self._empty_title = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        title_font = QFont(self._empty_title.font())
        title_font.setPointSizeF(title_font.pointSizeF() * 1.8)
        title_font.setBold(True)
        self._empty_title.setFont(title_font)
        self._empty_text = dim(QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True))
        buttons = QHBoxLayout()
        buttons.addStretch()
        for action in (self._open_folder_act, self._open_file_act):
            button = QPushButton(action.icon(), action.text().replace("&", ""))
            button.clicked.connect(action.trigger)
            buttons.addWidget(button)
        buttons.addStretch()
        for widget in (icon, self._empty_title, self._empty_text):
            empty_layout.addWidget(widget)
        empty_layout.addSpacing(12)
        empty_layout.addLayout(buttons)
        empty_layout.addStretch()

        self._video = VideoWidget(self._native_video)
        self._video.double_clicked.connect(self._toggle_fullscreen)
        self._media_stack = QStackedWidget()
        self._media_stack.addWidget(self._video)
        self._media_stack.addWidget(self._build_cd_page())

        player = QWidget()
        player_layout = QVBoxLayout(player)
        player_layout.setContentsMargins(0, 0, 0, 0)
        player_layout.setSpacing(0)
        player_layout.addWidget(self._media_stack, 1)
        player_layout.addWidget(self._build_controls())

        self._stack = QStackedWidget()
        self._stack.addWidget(empty)
        self._stack.addWidget(player)
        return self._stack

    def _build_cd_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)
        self._cover = CoverView()
        layout.addWidget(self._cover, 1)

        self._lyrics_stack = QStackedWidget()
        synced = QWidget()
        synced_layout = QVBoxLayout(synced)
        self._lyric_labels = []
        base = QFont(self.font())
        for scale, bold, lines in ((1.4, False, 1), (2.4, True, 2), (1.4, False, 1)):
            label = QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
            font = QFont(base)
            font.setPointSizeF(base.pointSizeF() * scale)
            font.setBold(bold)
            label.setFont(font)
            # Fixed heights keep the cover from jumping as lines change length.
            label.setFixedHeight(label.fontMetrics().lineSpacing() * lines + 4)
            if not bold:
                dim(label)
            synced_layout.addWidget(label)
            self._lyric_labels.append(label)
        self._lyrics_stack.addWidget(synced)
        self._plain_lyrics = QLabel(alignment=Qt.AlignmentFlag.AlignHCenter, wordWrap=True)
        self._plain_lyrics.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        plain_font = QFont(base)
        plain_font.setPointSizeF(base.pointSizeF() * 1.4)
        self._plain_lyrics.setFont(plain_font)
        plain = QScrollArea(widgetResizable=True, minimumHeight=220)
        plain.setWidget(self._plain_lyrics)
        plain.setFrameShape(QScrollArea.Shape.NoFrame)
        self._lyrics_stack.addWidget(plain)
        self._lyrics_note = dim(QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True))
        self._lyrics_stack.addWidget(self._lyrics_note)
        layout.addWidget(self._lyrics_stack)
        return page

    def _build_controls(self) -> QWidget:
        bar = QWidget()
        self._controls = bar
        row = QHBoxLayout(bar)
        row.setContentsMargins(8, 6, 8, 6)
        s = self._session

        def button(icon: str, tip: str, slot, fallback=None) -> QToolButton:
            b = QToolButton(icon=theme_icon(icon, fallback, self), toolTip=tip, autoRaise=True)
            b.setIconSize(QSize(20, 20))
            b.clicked.connect(lambda _checked=False: slot())
            row.addWidget(b)
            return b

        sp = QStyle.StandardPixmap
        self._prev_button = button("MediaSkipBackward", "Previous chapter", s.skip_back, sp.SP_MediaSkipBackward)
        self._play_button = button("MediaPlaybackStart", "Play/Pause (Space)", s.toggle_pause, sp.SP_MediaPlay)
        self._next_button = button("MediaSkipForward", "Next chapter", s.skip_forward, sp.SP_MediaSkipForward)
        self._play_icon = theme_icon("MediaPlaybackStart", sp.SP_MediaPlay, self)
        self._pause_icon = theme_icon("MediaPlaybackPause", sp.SP_MediaPause, self)

        self._time_label = QLabel("0:00 / 0:00")
        self._time_label.setMinimumWidth(self._time_label.fontMetrics().horizontalAdvance("00:00:00 / 00:00:00"))
        row.addWidget(self._time_label)
        self._seek = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=1)
        self._seek.sliderPressed.connect(lambda: setattr(self, "_dragging_seek", True))
        self._seek.sliderReleased.connect(self._on_seek_released)
        self._seek.actionTriggered.connect(self._on_seek_action)
        row.addWidget(self._seek, 1)

        self._audio_combo = QComboBox(toolTip="Audio track", minimumContentsLength=10)
        self._audio_combo.activated.connect(lambda i: self._syncing or s.select_audio(i))
        self._subs_combo = QComboBox(toolTip="Subtitles", minimumContentsLength=10)
        self._subs_combo.activated.connect(lambda i: self._syncing or s.select_subtitles(i))
        for combo, empty in ((self._audio_combo, "No audio"), (self._subs_combo, "No subtitles")):
            combo.addItem(empty)
            combo.setEnabled(False)
            row.addWidget(combo)
        self._lyrics_button = QToolButton(autoRaise=True)
        self._lyrics_button.setDefaultAction(self._lyrics_act)
        self._lyrics_button.hide()
        row.addWidget(self._lyrics_button)

        volume_icon = QLabel()
        volume_icon.setPixmap(theme_icon("AudioVolumeHigh", sp.SP_MediaVolume, self).pixmap(18, 18))
        row.addWidget(volume_icon)
        self._volume = QSlider(Qt.Orientation.Horizontal, minimum=0, maximum=100, value=100, maximumWidth=110)
        self._volume.setToolTip("Volume (↑/↓)")
        self._volume.valueChanged.connect(s.set_volume)
        row.addWidget(self._volume)
        button("ViewFullscreen", "Full screen (F)", self._toggle_fullscreen, sp.SP_TitleBarMaxButton)
        return bar

    def _build_shortcuts(self) -> None:
        s = self._session
        keys = {
            "Space": s.toggle_pause,
            "F": self._toggle_fullscreen,
            "Escape": self._leave_fullscreen,
            "Left": lambda: s.seek_by(-SEEK_STEP_MS),
            "Right": lambda: s.seek_by(SEEK_STEP_MS),
            "Up": lambda: s.change_volume(VOLUME_STEP),
            "Down": lambda: s.change_volume(-VOLUME_STEP),
            "L": lambda: s.cd_mode and self._lyrics_act.toggle(),
        }
        for key, slot in keys.items():
            # Window-wide, so focus on the seek slider or a list doesn't swallow Space and the arrows.
            QShortcut(QKeySequence(key), self, slot, context=Qt.ShortcutContext.WindowShortcut)

    # ---------- View: what the session shows ----------

    def show_drive_status(self, status: DriveStatus) -> None:
        self._status_label.setText(status.label)
        tray_open = status is DriveStatus.TRAY_OPEN
        self._eject_act.setText("Close &Tray" if tray_open else "&Eject")

    def show_empty(self, title: str, description: str) -> None:
        self._empty_title.setText(title)
        self._empty_text.setText(description)
        self._stack.setCurrentIndex(0)

    def show_player(self) -> None:
        self._stack.setCurrentIndex(1)

    def set_cd_mode(self, on: bool) -> None:
        self._media_stack.setCurrentIndex(1 if on else 0)
        self.set_list_heading(
            "Tracks" if on else "Titles", "Click a track to play it" if on else "Click a title or chapter to play it"
        )
        self._audio_combo.setVisible(not on)
        self._subs_combo.setVisible(not on)
        self._lyrics_button.setVisible(on)
        self._prev_button.setToolTip("Previous track" if on else "Previous chapter")
        self._next_button.setToolTip("Next track" if on else "Next chapter")

    def set_list_heading(self, title: str, description: str) -> None:
        self._list_box.setTitle(title)
        self._list_hint.setText(description)

    def set_disc_text(self, text: str) -> None:
        self._disc_label.setText(text)

    def set_region_text(self, text: str) -> None:
        self._region_label.setText(text)

    def set_subtitle(self, text: str) -> None:
        self._subtitle = text
        self.setWindowTitle(f"{text} – DVD Player" if text else "DVD Player")

    def set_rip_enabled(self, enabled: bool) -> None:
        self._rip_act.setEnabled(enabled)

    def set_busy(self, busy: bool) -> None:
        for action in (self._rip_act, self._eject_act, self._reload_act, self._open_file_act, self._open_folder_act):
            action.setEnabled(not busy)
        for widget in (self._open_button, self._tree, self._controls):
            widget.setEnabled(not busy)

    def notify(self, text: str, action_label: str = "", action=None, timeout_s: int | None = None) -> None:
        self._notifier.show_message(text, action_label, action, timeout_s)

    def show_title_rows(self, rows: list[TitleRow]) -> None:
        self.clear_rows()
        for t in rows:
            item = QTreeWidgetItem([t.label, t.details])
            item.setData(0, ROW_KEY, (t.index, -1))
            item.setToolTip(0, f"Play {t.label}")
            if t.chapters > 1:
                for c in range(t.chapters):
                    child = QTreeWidgetItem([f"Chapter {c + 1}", ""])
                    child.setData(0, ROW_KEY, (t.index, c))
                    item.addChild(child)
            self._tree.addTopLevelItem(item)
            self._rows[t.index] = item
        self._fit_columns()

    def show_track_rows(self, rows: list[TrackRow]) -> None:
        self.clear_rows()
        for t in rows:
            item = QTreeWidgetItem([t.label, format_duration(t.length_ms) if t.length_ms else ""])
            item.setData(0, ROW_KEY, (t.number, -1))
            self._tree.addTopLevelItem(item)
            self._rows[t.number] = item
        self._fit_columns()

    def _fit_columns(self) -> None:
        self._tree.resizeColumnToContents(1)
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, self._tree.header().ResizeMode.Stretch)

    def rename_track_rows(self, labels: dict[int, str]) -> None:
        for number, label in labels.items():
            item = self._rows.get(number)
            if item is not None:
                item.setText(0, label)

    def clear_rows(self) -> None:
        self._tree.clear()
        self._rows, self._current_row = {}, None

    def mark_current_row(self, key: int | None) -> None:
        play = theme_icon("MediaPlaybackStart", QStyle.StandardPixmap.SP_MediaPlay, self)
        for k, item in self._rows.items():
            font = item.font(0)
            font.setBold(k == key)
            item.setFont(0, font)
            item.setIcon(0, play if k == key else QIcon())
        self._current_row = key

    def show_cover(self, path: Path | None, thumbnail: bool) -> None:
        self._cover.set_path(path)
        small = scaled_pixmap(path, 40) if thumbnail else None
        if small is not None:
            self._disc_thumb.setPixmap(small)
        self._disc_thumb.setVisible(small is not None)

    def show_online(self, visible: bool, headline: str, details: str, busy: bool, failed: bool) -> None:
        self._online_box.setVisible(visible)
        self._online_headline.setText(headline)
        self._online_details.setText(details)
        self._online_busy.setVisible(busy)
        self._retry_button.setVisible(failed and not busy)

    def show_synced_lyrics(self, lines: tuple[str, str, str]) -> None:
        for label, text in zip(self._lyric_labels, lines, strict=True):
            label.setText(text)
        self._lyrics_stack.setCurrentIndex(0)

    def show_plain_lyrics(self, text: str) -> None:
        self._plain_lyrics.setText(text)
        self._lyrics_stack.setCurrentIndex(1)

    def show_lyrics_note(self, text: str) -> None:
        self._lyrics_note.setText(text)
        self._lyrics_stack.setCurrentIndex(2)

    def show_time(self, now_ms: int, length_ms: int, move_slider: bool) -> None:
        self._time_label.setText(f"{format_duration(now_ms)} / {format_duration(length_ms)}")
        self._seek.setMaximum(max(length_ms, 1))
        if move_slider and not self._dragging_seek:
            self._seek.blockSignals(True)
            self._seek.setValue(now_ms)
            self._seek.blockSignals(False)

    def show_playing(self, playing: bool) -> None:
        self._play_button.setIcon(self._pause_icon if playing else self._play_icon)

    def show_track_menus(self, audio: list[Track], subs: list[Track], audio_id: int, subs_id: int) -> None:
        self._syncing = True
        try:
            for combo, tracks, current, attr, empty in (
                (self._audio_combo, audio, audio_id, "_audio", "No audio"),
                (self._subs_combo, subs, subs_id, "_subs", "No subtitles"),
            ):
                if tracks != getattr(self, attr):
                    setattr(self, attr, tracks)
                    combo.clear()
                    combo.addItems([t.name for t in tracks] or [empty])
                    combo.setEnabled(bool(tracks))
                for i, t in enumerate(tracks):
                    if t.id == current and combo.currentIndex() != i:
                        combo.setCurrentIndex(i)
        finally:
            self._syncing = False

    def show_frame(self, data: bytes, width: int, height: int, stride: int, aspect: float) -> None:
        self._video.show_frame(data, width, height, stride, aspect)

    def clear_video(self) -> None:
        self._video.clear()

    def show_rip_progress(self, title: str, fraction: float, text: str) -> None:
        self._rip_title.setText(title)
        self._rip_text.setText(text)
        self._rip_bar.setValue(int(fraction * 1000))
        self._rip_bar.setMaximum(1000)
        self._rip_box.show()

    def hide_rip_progress(self) -> None:
        self._rip_box.hide()

    def show_volume(self, percent: int) -> None:
        self._volume.setValue(percent)

    def open_folder(self, folder: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---------- user actions ----------

    def _on_row_clicked(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        key = item.data(0, ROW_KEY)
        if not key:
            return
        number, chapter = key
        if self._session.cd_mode:
            self._session.play_track(number)
        else:
            self._session.play(number, max(chapter, 0))

    def _on_seek_action(self, _action) -> None:
        # Clicks on the groove and keyboard paging move the slider without a press/release pair.
        if not self._dragging_seek:
            QTimer.singleShot(0, lambda: self._session.seek(self._seek.sliderPosition()))

    def _on_seek_released(self) -> None:
        self._dragging_seek = False
        self._session.seek(self._seek.value())

    def _choose_file(self) -> None:
        if not self._session.can_open():
            return
        patterns = " ".join(f"*{e}" for e in sorted(AUDIO_EXTENSIONS | VIDEO_EXTENSIONS))
        path, _ = QFileDialog.getOpenFileName(
            self, "Open a Track or Video", str(self._session.open_dir), f"Music and videos ({patterns})"
        )
        if path:
            self._session.open_path(Path(path))

    def _choose_folder(self) -> None:
        if not self._session.can_open():
            return
        path = QFileDialog.getExistingDirectory(self, "Open an Album Folder", str(self._session.open_dir))
        if path:
            self._session.open_path(Path(path))

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if urls and self._session.can_open():
            self._session.open_path(Path(urls[0].toLocalFile()))
            event.acceptProposedAction()

    def _open_rip_dialog(self) -> None:
        choices = self._session.rip_choices()
        if not choices:
            QMessageBox.information(self, "Can't rip this disc", self._session.rip_blocked_reason)
            return
        RipDialog(self, self._session, choices).open()

    def _confirm_clear_data(self) -> None:
        target = self._session.clearable_data()
        if target is None:
            return
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            "Clear saved data?",
            f"Deletes {format_bytes(target.size)}: album names (including your edits), covers and lyrics saved "
            "for your CDs, leftover temporary files, and older log files.",
            parent=self,
        )
        box.setInformativeText("Your rips are not touched. CDs are looked up online again the next time they play.")
        clear = box.addButton("Clear", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is clear:
            self._session.clear_data(target)

    def _open_log_folder(self) -> None:
        folder = applog.log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        self.open_folder(folder)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self._leave_fullscreen()
            return
        self._was_maximized = self.isMaximized()
        for widget in (self.menuBar(), self._toolbar, self.statusBar(), self._splitter.widget(0)):
            widget.hide()
        self.showFullScreen()

    def _leave_fullscreen(self) -> None:
        if not self.isFullScreen():
            return
        for widget in (self.menuBar(), self._toolbar, self.statusBar()):
            widget.show()
        self._splitter.widget(0).setVisible(self._sidebar_act.isChecked())
        if getattr(self, "_was_maximized", False):
            self.showMaximized()
        else:
            self.showNormal()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for timer in (getattr(self, "_poll_timer", None), getattr(self, "_tick_timer", None)):
            if timer is not None:
                timer.stop()
        self._session.shutdown()
