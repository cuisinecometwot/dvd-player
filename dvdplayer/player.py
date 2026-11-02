"""libvlc wrapper that renders video into memory so GTK4 can draw it (works on Wayland)."""

import ctypes
import threading
from collections.abc import Callable

import vlc

from domain.media import Chapter, Title, Track, display_aspect

TITLE_FLAG_MENU = 0x1

# python-vlc types the chroma argument as c_char_p, which makes it read-only; we must write "RV32".
_FormatCb = ctypes.CFUNCTYPE(
    ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint),
    ctypes.POINTER(ctypes.c_uint),
    ctypes.POINTER(ctypes.c_uint),
    ctypes.POINTER(ctypes.c_uint),
)

FrameHandler = Callable[[bytes, int, int, int], None]


class Player:
    def __init__(self, on_frame: FrameHandler):
        self._on_frame = on_frame
        self._instance = vlc.Instance("--quiet", "--no-xlib")
        self._mp = self._instance.media_player_new()
        self._buf_lock = threading.Lock()
        self._buf = None
        self._width = self._height = self._stride = 0

        # Keep references: ctypes callbacks are freed if garbage-collected while VLC still holds them.
        self._format_raw = _FormatCb(self._setup_format)
        self._format_cb = vlc.CallbackDecorators.VideoFormatCb(ctypes.cast(self._format_raw, ctypes.c_void_p).value)
        self._cleanup_cb = vlc.CallbackDecorators.VideoCleanupCb(lambda _opaque: None)
        self._lock_cb = vlc.CallbackDecorators.VideoLockCb(self._lock_frame)
        self._unlock_cb = vlc.CallbackDecorators.VideoUnlockCb(lambda *_: None)
        self._display_cb = vlc.CallbackDecorators.VideoDisplayCb(self._display_frame)
        self._mp.video_set_format_callbacks(self._format_cb, self._cleanup_cb)
        self._mp.video_set_callbacks(self._lock_cb, self._unlock_cb, self._display_cb, None)

    # --- VLC video thread callbacks ---

    def _setup_format(self, _opaque, chroma, width, height, pitches, lines):
        ctypes.memmove(chroma, b"RV32", 4)
        w, h = width[0], height[0]
        with self._buf_lock:
            self._width, self._height, self._stride = w, h, w * 4
            self._buf = ctypes.create_string_buffer(self._stride * h)
        pitches[0], lines[0] = self._stride, h
        return 1

    def _lock_frame(self, _opaque, planes):
        planes[0] = ctypes.addressof(self._buf)
        return None

    def _display_frame(self, _opaque, _picture):
        with self._buf_lock:
            data = self._buf.raw
            w, h, stride = self._width, self._height, self._stride
        self._on_frame(data, w, h, stride)

    # --- control (main thread) ---

    def open(self, mrl: str) -> None:
        self._mp.set_media(self._instance.media_new(mrl))
        self._mp.play()

    def open_cd_track(self, device: str, track: int) -> None:
        media = self._instance.media_new(f"cdda://{device}")
        # Without a track option VLC treats the CD as a playlist, which a bare MediaPlayer won't walk.
        media.add_option(f":cdda-track={track}")
        self._mp.set_media(media)
        self._mp.play()

    def is_ended(self) -> bool:
        return self._mp.get_state() == vlc.State.Ended

    def stop(self) -> None:
        self._mp.stop()

    def toggle_pause(self) -> None:
        if self._mp.get_state() in (vlc.State.Stopped, vlc.State.Ended):
            self._mp.play()
        else:
            self._mp.pause()

    def is_playing(self) -> bool:
        return bool(self._mp.is_playing())

    def has_media(self) -> bool:
        return self._mp.get_media() is not None and self._mp.get_state() not in (
            vlc.State.NothingSpecial,
            vlc.State.Stopped,
            vlc.State.Error,
        )

    def time_ms(self) -> int:
        return max(0, self._mp.get_time())

    def length_ms(self) -> int:
        return max(0, self._mp.get_length())

    def seek(self, ms: int) -> None:
        self._mp.set_time(int(ms))

    def volume(self) -> int:
        return max(0, self._mp.audio_get_volume())

    def set_volume(self, percent: int) -> None:
        self._mp.audio_set_volume(int(percent))

    def titles(self) -> list[Title]:
        try:
            descs = self._mp.get_full_title_descriptions() or []
            return [
                Title(i, d.duration, bool(d.flags & TITLE_FLAG_MENU), (d.name or b"").decode(errors="replace"))
                for i, d in enumerate(descs)
            ]
        except (ValueError, OSError):  # VLC returns -1 while the disc is still being parsed
            return []

    def chapters(self, title_index: int) -> list[Chapter]:
        try:
            descs = self._mp.get_full_chapter_descriptions(title_index) or []
            return [Chapter(i, d.time_offset, d.duration) for i, d in enumerate(descs)]
        except (ValueError, OSError):
            return []

    def current_title(self) -> int:
        return self._mp.get_title()

    def current_chapter(self) -> int:
        return self._mp.get_chapter()

    def play_title(self, index: int, chapter: int = 0) -> None:
        if not self.is_playing():
            self._mp.play()
        self._mp.set_title(index)
        if chapter:
            self._mp.set_chapter(chapter)

    def next_chapter(self) -> None:
        self._mp.next_chapter()

    def previous_chapter(self) -> None:
        self._mp.previous_chapter()

    def audio_tracks(self) -> list[Track]:
        return _tracks(self._mp.audio_get_track_description())

    def audio_track(self) -> int:
        return self._mp.audio_get_track()

    def set_audio_track(self, track_id: int) -> None:
        self._mp.audio_set_track(track_id)

    def subtitle_tracks(self) -> list[Track]:
        return _tracks(self._mp.video_get_spu_description())

    def subtitle_track(self) -> int:
        return self._mp.video_get_spu()

    def set_subtitle_track(self, track_id: int) -> None:
        self._mp.video_set_spu(track_id)

    def video_aspect(self, width: int, height: int) -> float:
        media = self._mp.get_media()
        if media is not None:
            try:
                for track in media.tracks_get() or []:
                    if track.type == vlc.TrackType.video:
                        v = track.video.contents
                        if v.width == width and v.height == height:
                            return display_aspect(width, height, v.sar_num, v.sar_den)
            except (ValueError, OSError, AttributeError):
                pass
        return display_aspect(width, height)

    def release(self) -> None:
        # VLC's video thread calls back into Python; it must be stopped before the interpreter exits.
        if self._mp is None:
            return
        self._mp.stop()
        self._mp.release()
        self._instance.release()
        self._mp = None


def _tracks(descs) -> list[Track]:
    return [Track(tid, n.decode(errors="replace") if isinstance(n, bytes) else str(n)) for tid, n in descs or []]
