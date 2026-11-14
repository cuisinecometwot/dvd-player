import struct

from domain.imageinfo import PNG_SIGNATURE, image_info
from domain.userdirs import parse_user_dirs


def _png(width: int, height: int) -> bytes:
    return PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)


def _jpeg(width: int, height: int, *, progressive: bool = False) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)
    dht = b"\xff\xc4" + struct.pack(">H", 5) + bytes(3)  # in the SOF range numerically, but carries no size
    sof = b"\xff" + (b"\xc2" if progressive else b"\xc0") + struct.pack(">HBHH", 11, 8, height, width) + bytes(4)
    return b"\xff\xd8" + app0 + dht + b"\xff\xff" + sof + b"\xff\xd9"


def test_png_size():
    assert image_info(_png(500, 375)) == ("image/png", 500, 375)


def test_jpeg_size_skips_other_segments():
    assert image_info(_jpeg(1200, 1100)) == ("image/jpeg", 1200, 1100)
    assert image_info(_jpeg(64, 48, progressive=True)) == ("image/jpeg", 64, 48)


def test_not_an_image():
    assert image_info(b"GIF89a....") is None
    assert image_info(b"\xff\xd8\xff") is None
    assert image_info(b"") is None


def test_parse_user_dirs():
    text = """# written by xdg-user-dirs-update
XDG_DESKTOP_DIR="$HOME/Desktop"
XDG_MUSIC_DIR="$HOME/Nhạc"
XDG_VIDEOS_DIR="/data/videos"
XDG_DOWNLOAD_DIR="$HOME/"
XDG_TEMPLATES_DIR="relative/is/invalid"
"""
    dirs = parse_user_dirs(text, "/home/me")
    assert dirs == {"DESKTOP": "/home/me/Desktop", "MUSIC": "/home/me/Nhạc", "VIDEOS": "/data/videos"}
