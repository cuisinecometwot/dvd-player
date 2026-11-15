"""libvlc wrapper that renders video into memory so GTK4 can draw it (works on Wayland)."""

import ctypes
import ctypes.util
import logging
import sys
import threading
from collections.abc import Callable

import vlc

from domain.media import Chapter, Title, Track, display_aspect

TITLE_FLAG_MENU = 0x1
# --no-xlib only exists in Linux builds; libvlc refuses to start on an unknown option.
_VLC_ARGS = ("--quiet", "--no-xlib") if sys.platform.startswith("linux") else ("--quiet",)

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

log = logging.getLogger(__name__)
vlc_log = logging.getLogger("vlc")
LIBVLC_WARNING = 3  # libvlc_log_level: DEBUG=0, NOTICE=2, WARNING=3, ERROR=4
LOG_LINE_BYTES = 1024


def _c_formatter():
    """vsnprintf for libvlc's (format, va_list) log messages; None if the C runtime can't be reached."""
    try:
        if sys.platform == "win32":
            # UCRT has no exported vsnprintf (it is a header inline over this), and msvcrt's misreads C99 sizes.
            fn = ctypes.CDLL("ucrtbase").__stdio_common_vsprintf
            fn.argtypes = [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p,
                           ctypes.c_void_p]  # fmt: skip
            snprintf_behaviour = 2  # _CRT_INTERNAL_PRINTF_STANDARD_SNPRINTF_BEHAVIOR
            return lambda buf, size, fmt, args: fn(snprintf_behaviour, buf, size, fmt, None, args)
        fn = ctypes.CDLL(ctypes.util.find_library("c")).vsnprintf
        fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p]
        return lambda buf, size, fmt, args: fn(buf, size, fmt, args)
    except (OSError, AttributeError, TypeError):
        return None


class Player:
    def __init__(self, on_frame: FrameHandler | None):
        """on_frame gets each decoded RV32 frame; None means VLC draws into a window given to set_window()."""
        self._on_frame = on_frame
        self._instance = vlc.Instance(*_VLC_ARGS)
        self._mp = self._instance.media_player_new()
        self._buf_lock = threading.Lock()
        self._buf = None
        self._width = self._height = self._stride = 0
        self._volume = 100
        self._volume_pending = False

        if on_frame is not None:
            # Keep references: ctypes callbacks are freed if garbage-collected while VLC still holds them.
            self._format_raw = _FormatCb(self._setup_format)
            self._format_cb = vlc.CallbackDecorators.VideoFormatCb(ctypes.cast(self._format_raw, ctypes.c_void_p).value)
            self._cleanup_cb = vlc.CallbackDecorators.VideoCleanupCb(lambda _opaque: None)
            self._lock_cb = vlc.CallbackDecorators.VideoLockCb(self._lock_frame)
            self._unlock_cb = vlc.CallbackDecorators.VideoUnlockCb(lambda *_: None)
            self._display_cb = vlc.CallbackDecorators.VideoDisplayCb(self._display_frame)
            self._mp.video_set_format_callbacks(self._format_cb, self._cleanup_cb)
            self._mp.video_set_callbacks(self._lock_cb, self._unlock_cb, self._display_cb, None)

        # VLC's own warnings and errors go to the app log (a windowed exe has nowhere else to show them).
        self._formatter = _c_formatter()
        self._last_vlc_line = ("", 0)  # (text, repeats) so a message repeated every frame logs once
        self._log_cb = vlc.CallbackDecorators.LogCb(self._on_vlc_log)
        if self._formatter is not None:
            self._instance.log_set(self._log_cb, None)
        log.info("libvlc %s", (vlc.libvlc_get_version() or b"?").decode(errors="replace"))

    def _on_vlc_log(self, _data, level, ctx, fmt, args) -> None:
        if level < LIBVLC_WARNING:
            return
        try:
            buf = ctypes.create_string_buffer(LOG_LINE_BYTES)
            self._formatter(ctypes.cast(buf, ctypes.c_void_p), LOG_LINE_BYTES, fmt, args)
            module = ctypes.c_char_p()
            vlc.dll.libvlc_log_get_context(ctypes.cast(ctx, ctypes.c_void_p), ctypes.byref(module), None, None)
            text = f"{(module.value or b'?').decode(errors='replace')}: {buf.value.decode(errors='replace')}"
            last, repeats = self._last_vlc_line
            if text == last:
                self._last_vlc_line = (last, repeats + 1)
                return
            if repeats:
                vlc_log.warning("(previous message repeated %d more times)", repeats)
            self._last_vlc_line = (text, 0)
            vlc_log.log(logging.ERROR if level > LIBVLC_WARNING else logging.WARNING, "%s", text)
        except Exception:  # noqa: BLE001 - never let a log line raise inside VLC's thread
            pass

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

    def set_window(self, handle: int) -> None:
        """Let VLC draw (hardware-accelerated) into a native window: an HWND on Windows, an X11 window id."""
        if sys.platform == "win32":
            self._mp.set_hwnd(handle)
        else:
            self._mp.set_xwindow(handle)
        # Clicks and keys go to the app's widget underneath, not to VLC (double-click = fullscreen, Space...).
        self._mp.video_set_mouse_input(False)
        self._mp.video_set_key_input(False)

    def open(self, mrl: str) -> None:
        log.info("open %s", mrl)
        self._mp.set_media(self._instance.media_new(mrl))
        self._play_new_media()

    def _play_new_media(self) -> None:
        # Always push the slider's volume: WirePlumber restores the last saved volume for VLC's
        # "Video" stream role, so a new output may start anywhere, even at 0.
        self._volume_pending = True
        self._mp.play()

    def open_cd_track(self, device: str, track: int) -> None:
        log.info("open cdda://%s track %d", device, track)
        media = self._instance.media_new(f"cdda://{device}")
        # Without a track option VLC treats the CD as a playlist, which a bare MediaPlayer won't walk.
        media.add_option(f":cdda-track={track}")
        self._mp.set_media(media)
        self._play_new_media()

    def is_ended(self) -> bool:
        return self._mp.get_state() == vlc.State.Ended

    def has_stopped_early(self) -> bool:
        """Ended or failed. Before any title shows up this means VLC couldn't open the media:
        libvlc 3 reports an unreadable disc as Ended, not Error."""
        return self._mp.get_state() in (vlc.State.Ended, vlc.State.Error)

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
        return self._volume

    def set_volume(self, percent: int) -> None:
        self._volume, self._volume_pending = int(percent), True
        self.sync_volume()

    def sync_volume(self) -> None:
        """Apply a volume change once audio is flowing: VLC's PipeWire output segfaults if set earlier,
        including while the state already says Playing but the clock hasn't started."""
        state = self._mp.get_state()
        if self._volume_pending and state in (vlc.State.Playing, vlc.State.Paused) and self._mp.get_time() > 0:
            self._mp.audio_set_volume(self._volume)
            self._volume_pending = False

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
        self._instance.log_unset()  # waits for a log callback in progress; none may run after this
        self._mp.stop()
        self._mp.release()
        self._instance.release()
        self._mp = None


def _tracks(descs) -> list[Track]:
    return [Track(tid, n.decode(errors="replace") if isinstance(n, bytes) else str(n)) for tid, n in descs or []]
