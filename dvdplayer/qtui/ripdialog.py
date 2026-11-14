"""Qt "Rip to File" dialog: what and where, plus the album form (names, cover, lyrics, Opus) for audio CDs."""

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from domain.cdmeta import edited_album
from domain.rip import OPUS_BITRATE_KBPS, RipKind
from dvdplayer.qtui.widgets import dim, scaled_pixmap
from dvdplayer.ripform import album_status_text, cover_text
from dvdplayer.session import RipChoice, Session


class RipDialog(QDialog):
    def __init__(self, parent: QWidget, session: Session, choices: list[RipChoice]):
        super().__init__(parent)
        self.setWindowTitle("Rip to File")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._session = session
        self._choices = choices
        self._folder: Path | None = None
        audio = any(c.kind is RipKind.AUDIO_TRACKS for c in choices)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._what = QComboBox()
        self._what.addItems([c.label for c in choices])
        self._what.setCurrentIndex(session.default_choice(choices))
        self._what.currentIndexChanged.connect(self._refresh_folder)
        self._folder_edit = QLineEdit(readOnly=True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self._folder_edit, 1)
        folder_row.addWidget(browse)
        form.addRow("What:", self._what)
        form.addRow("Save to:", folder_row)
        layout.addLayout(form)
        layout.addWidget(
            dim(QLabel("Copies streams as-is (no re-encoding), so quality is identical to the disc. "
                       "Playback stops while ripping.", wordWrap=True))
        )  # fmt: skip

        self._album_form = self._build_album_form(layout) if audio else None
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        start = buttons.addButton("Start Rip", QDialogButtonBox.ButtonRole.AcceptRole)
        start.setDefault(True)
        buttons.accepted.connect(self._start)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(lambda _result: setattr(session, "album_listener", None))
        self._refresh_folder()
        self.resize(520, 700 if audio else 220)

    def _current(self) -> RipChoice:
        return self._choices[self._what.currentIndex()]

    def _current_folder(self) -> Path:
        return self._folder or self._session.default_rip_folder(self._current().kind)

    def _refresh_folder(self) -> None:
        self._folder_edit.setText(str(self._current_folder()))

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Save rips to", str(self._current_folder()))
        if path:
            self._folder = Path(path)
            self._refresh_folder()

    # ---------- audio CD: album form ----------

    def _build_album_form(self, layout: QVBoxLayout):
        cd_info = self._session.cd_info
        info = cd_info.album
        album_box = QGroupBox("Album")
        album_layout = QVBoxLayout(album_box)
        self._status = dim(QLabel(wordWrap=True))
        album_layout.addWidget(self._status)
        fields = QFormLayout()
        self._album_edit, self._artist_edit, self._year_edit = QLineEdit(), QLineEdit(), QLineEdit(maxLength=4)
        fields.addRow("Album:", self._album_edit)
        fields.addRow("Artist:", self._artist_edit)
        fields.addRow("Year:", self._year_edit)
        album_layout.addLayout(fields)

        self._lyrics_check = QCheckBox("Download lyrics", checked=True)
        album_layout.addWidget(self._lyrics_check)
        album_layout.addWidget(
            dim(QLabel("From LRCLIB: saved in the FLAC tags, plus a synced .lrc file when available", wordWrap=True))
        )
        self._opus_check = QCheckBox("Also make Opus copies", checked=self._session.want_opus)
        album_layout.addWidget(self._opus_check)
        album_layout.addWidget(
            dim(QLabel(f"Small files for phones: Opus {OPUS_BITRATE_KBPS} kb/s (≈1.2 MB/min, sounds the same), "
                       "with tags, lyrics and cover, in a separate “(Opus)” folder. The FLACs stay lossless.",
                       wordWrap=True))
        )  # fmt: skip

        cover_row = QHBoxLayout()
        self._cover_thumb = QLabel(fixedSize=QSize(48, 48))
        self._cover_label = dim(QLabel(wordWrap=True))
        choose_cover = QPushButton("Choose…")
        choose_cover.clicked.connect(self._choose_cover)
        cover_row.addWidget(QLabel("Cover:"))
        cover_row.addWidget(self._cover_thumb)
        cover_row.addWidget(self._cover_label, 1)
        cover_row.addWidget(choose_cover)
        album_layout.addLayout(cover_row)
        layout.addWidget(album_box)

        tracks_box = QGroupBox("Tracks")
        tracks_form = QFormLayout()
        self._track_edits: dict[int, QLineEdit] = {}
        for number, _, _ in self._session.audio_spans:
            self._track_edits[number] = QLineEdit()
            tracks_form.addRow(f"Track {number}:", self._track_edits[number])
        inner = QWidget()
        inner.setLayout(tracks_form)
        scroll = QScrollArea(widgetResizable=True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        QVBoxLayout(tracks_box).addWidget(scroll)
        layout.addWidget(tracks_box, 1)

        self._edited = False
        self._named = cd_info.named
        self._cover_error = ""
        self._fill(info)
        for edit in (self._album_edit, self._artist_edit, self._year_edit, *self._track_edits.values()):
            edit.textEdited.connect(self._on_edited)  # only the user's typing, not _fill()
        self._on_info_changed()
        self._session.album_listener = self._on_info_changed
        return True

    def _fill(self, src) -> None:
        self._album_edit.setText(src.title)
        self._artist_edit.setText(src.artist)
        self._year_edit.setText(src.year)
        for number, edit in self._track_edits.items():
            edit.setText(src.track(number).title)

    def _on_edited(self, _text: str) -> None:
        self._edited = True

    def _on_info_changed(self) -> None:
        cd_info = self._session.cd_info
        # Fill once when names arrive, and never over what the user already typed.
        if cd_info.named and not self._named and not self._edited:
            self._fill(cd_info.album)
        self._named = cd_info.named
        self._status.setText(album_status_text(cd_info))
        self._show_cover()

    def _show_cover(self) -> None:
        cd_info = self._session.cd_info
        pixmap = scaled_pixmap(cd_info.cover_path, 48)
        if pixmap is not None:
            self._cover_thumb.setPixmap(pixmap)
        else:
            self._cover_thumb.clear()
        self._cover_label.setText(cover_text(cd_info, self._cover_error))

    def _choose_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a cover image", "", "Images (*.jpg *.jpeg *.png)")
        if path:
            self._cover_error = self._session.cd_info.set_cover(Path(path)) or ""
            self._show_cover()

    # ---------- start ----------

    def _start(self) -> None:
        choice = self._current()
        session = self._session
        session.album_listener = None
        album, want_lyrics = None, False
        if self._album_form and choice.kind is RipKind.AUDIO_TRACKS:
            album = edited_album(
                session.cd_info.album,
                self._album_edit.text(),
                self._artist_edit.text(),
                self._year_edit.text(),
                {n: e.text() for n, e in self._track_edits.items()},
            )
            want_lyrics = self._lyrics_check.isChecked()
            session.want_opus = self._opus_check.isChecked()
        folder = self._current_folder()
        self.accept()
        session.start_rip(choice, folder, album=album, want_lyrics=want_lyrics)
