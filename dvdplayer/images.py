"""Load cover images as small textures, so widgets size to the thumbnail rather than the full scan."""

from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402


def load_texture(path: Path | None, max_width: int, max_height: int) -> Gdk.Texture | None:
    if path is None:
        return None
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), max_width, max_height, True)
    except GLib.Error:
        return None
    fmt = Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(
        pixbuf.get_width(), pixbuf.get_height(), fmt, pixbuf.read_pixel_bytes(), pixbuf.get_rowstride()
    )
