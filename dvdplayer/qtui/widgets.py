"""Qt widgets the standard set doesn't have: the video surface, the cover view, status-bar notifications."""

from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStatusBar, QStyle, QWidget

from domain.media import fit_rect

DEFAULT_NOTIFY_S = 5


def theme_icon(name: str, fallback: QStyle.StandardPixmap | None = None, widget: QWidget | None = None) -> QIcon:
    """A QIcon.ThemeIcon (Segoe Fluent/MDL2 glyphs on Windows 10/11, the icon theme on Linux), else a fallback."""
    icon = QIcon.fromTheme(getattr(QIcon.ThemeIcon, name))
    if icon.isNull() and fallback is not None and widget is not None:
        icon = widget.style().standardIcon(fallback)
    return icon


class VideoWidget(QWidget):
    """Black surface for video. native=True: VLC draws into this widget's own window (winId); otherwise the
    widget paints the frames VLC hands over, letterboxed to the display aspect."""

    double_clicked = Signal()

    def __init__(self, native: bool):
        super().__init__()
        self._native = native
        self._image: QImage | None = None
        self._data = b""  # keeps the bytes behind _image alive
        self._aspect = 16 / 9
        self.setMinimumSize(160, 90)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        if native:
            self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
            self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)

    def window_handle(self) -> int:
        return int(self.winId())

    def show_frame(self, data: bytes, width: int, height: int, stride: int, aspect: float) -> None:
        # VLC "RV32" is B,G,R,X in memory: Qt's RGB32 on a little-endian machine.
        self._data = data
        self._image = QImage(data, width, height, stride, QImage.Format.Format_RGB32)
        self._aspect = aspect
        self.update()

    def clear(self) -> None:
        self._image, self._data = None, b""
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._image is not None and not self._native:
            x, y, w, h = fit_rect(self.width(), self.height(), self._aspect)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(QRectF(x, y, w, h), self._image)

    def mouseDoubleClickEvent(self, _event) -> None:  # noqa: N802
        self.double_clicked.emit()


class CoverView(QWidget):
    """The album cover, as large as fits, at its own shape, with rounded corners; a CD icon when there's none."""

    def __init__(self):
        super().__init__()
        self._pixmap: QPixmap | None = None
        self._placeholder = theme_icon("MediaOptical")
        self.setMinimumSize(120, 120)

    def set_path(self, path: Path | None) -> None:
        pixmap = QPixmap(str(path)) if path else None
        self._pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self._pixmap is None:
            side = min(128, self.width(), self.height())
            self._placeholder.paint(painter, (self.width() - side) // 2, (self.height() - side) // 2, side, side)
            return
        aspect = self._pixmap.width() / max(self._pixmap.height(), 1)
        x, y, w, h = fit_rect(self.width(), self.height(), aspect)
        target = QRectF(x, y, w, h)
        clip = QPainterPath()
        clip.addRoundedRect(target, 12, 12)
        painter.setClipPath(clip)
        painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))


def scaled_pixmap(path: Path | None, size: int) -> QPixmap | None:
    if path is None:
        return None
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return None
    return pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)


class Notifier(QWidget):
    """Messages in the status bar, optionally with one action button (e.g. Open Folder, Play Disc)."""

    def __init__(self, status_bar: QStatusBar):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 6, 0)
        self._label = QLabel()
        self._button = QPushButton()
        self._button.setFlat(True)
        self._button.hide()
        self._button.clicked.connect(self._on_clicked)
        layout.addWidget(self._label)
        layout.addWidget(self._button)
        layout.addStretch()
        status_bar.addWidget(self, 1)
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self.clear)
        self._action = None

    def show_message(self, text: str, action_label: str = "", action=None, timeout_s: int | None = None) -> None:
        self._label.setText(text)
        self._action = action
        self._button.setVisible(bool(action_label and action))
        self._button.setText(action_label)
        self._timer.stop()
        if timeout_s != 0:  # 0: stays until the next message or the action
            self._timer.start((timeout_s or DEFAULT_NOTIFY_S) * 1000)

    def clear(self) -> None:
        self._label.clear()
        self._button.hide()
        self._action = None

    def _on_clicked(self) -> None:
        action = self._action
        self.clear()
        if action is not None:
            action()


def dim(label: QLabel) -> QLabel:
    """Secondary text: the palette's placeholder colour, so it follows light/dark."""
    palette = label.palette()
    color = QColor(palette.color(palette.ColorRole.PlaceholderText))
    palette.setColor(palette.ColorRole.WindowText, color)
    label.setPalette(palette)
    return label
