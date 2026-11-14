"""GTK4 widget that draws the latest decoded video frame, letterboxed."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk  # noqa: E402

from domain.media import fit_rect  # noqa: E402

_BLACK = Gdk.RGBA()
_BLACK.parse("black")


class VideoView(Gtk.Widget):
    __gtype_name__ = "DvdVideoView"

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self._texture = None
        self._aspect = 16 / 9

    def show_frame(self, data: bytes, width: int, height: int, stride: int, aspect: float) -> None:
        # VLC "RV32" is little-endian 0x00RRGGBB, i.e. B,G,R,X in memory.
        self._texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.B8G8R8X8, GLib.Bytes.new(data), stride)
        self._aspect = aspect
        self.queue_draw()

    def clear(self) -> None:
        self._texture = None
        self.queue_draw()

    def do_measure(self, orientation, _for_size):
        minimum = 160 if orientation == Gtk.Orientation.HORIZONTAL else 90
        natural = 854 if orientation == Gtk.Orientation.HORIZONTAL else 480
        return minimum, natural, -1, -1

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        snapshot.append_color(_BLACK, Graphene.Rect().init(0, 0, w, h))
        if self._texture is not None:
            x, y, rw, rh = fit_rect(w, h, self._aspect)
            snapshot.append_scaled_texture(self._texture, Gsk.ScalingFilter.LINEAR, Graphene.Rect().init(x, y, rw, rh))
