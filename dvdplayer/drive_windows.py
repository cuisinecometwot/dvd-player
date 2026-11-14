"""Windows optical drive access via DeviceIoControl on \\\\.\\D: (ctypes, no extra packages)."""

import ctypes
import struct
import subprocess
import threading
from ctypes import wintypes

from domain.disc import DiscKind, DriveStatus, is_css_protected
from domain.rip import CdTrack
from domain.wincd import (
    COPYRIGHT_BUFFER_SIZE,
    MCN_BUFFER_SIZE,
    TOC_SIZE,
    copyright_protection_type,
    disc_kind_from_tracks,
    parse_mcn,
    parse_toc,
)
from dvdplayer.drive_base import DriveBase
from dvdplayer.proc import NO_WINDOW

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    wintypes.HANDLE,
]  # fmt: skip
_k32.DeviceIoControl.restype = wintypes.BOOL
_k32.DeviceIoControl.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]  # fmt: skip
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_k32.GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
]  # fmt: skip
_k32.GetDiskFreeSpaceExW.argtypes = [
    wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
    ctypes.POINTER(ctypes.c_ulonglong),
]  # fmt: skip
_k32.SetErrorMode.argtypes = [wintypes.UINT]

INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
GENERIC_READ = 0x80000000
FILE_SHARE_READ_WRITE = 0x1 | 0x2
OPEN_EXISTING = 3
DRIVE_CDROM = 5
ERROR_NOT_READY = 21
ERROR_MEDIA_CHANGED = 1110
SEM_FAILCRITICALERRORS = 0x0001

IOCTL_STORAGE_CHECK_VERIFY2 = 0x2D0800
IOCTL_STORAGE_EJECT_MEDIA = 0x2D4808
IOCTL_STORAGE_LOAD_MEDIA = 0x2D480C
IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
IOCTL_CDROM_READ_TOC = 0x24000
IOCTL_CDROM_READ_Q_CHANNEL = 0x2402C
IOCTL_DVD_READ_STRUCTURE = 0x335140
IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
IOCTL_CDROM_MEDIA_CATALOG = 0x02
DVD_COPYRIGHT_DESCRIPTOR = 1

# No "There is no disk in the drive" system dialogs when probing an empty drive.
_k32.SetErrorMode(SEM_FAILCRITICALERRORS)


def find_optical_device() -> str | None:
    """First drive letter that is a CD/DVD drive, e.g. "D:"."""
    mask = _k32.GetLogicalDrives()
    for i in range(26):
        letter = f"{chr(ord('A') + i)}:"
        if mask & (1 << i) and _k32.GetDriveTypeW(letter + "\\") == DRIVE_CDROM:
            return letter
    return None


def _normalize(device: str) -> str:
    d = device.strip().rstrip("\\/")
    if d.startswith("\\\\.\\"):
        d = d[4:]
    return d[:1].upper() + ":" if d else d


class _Handle:
    def __init__(self, path: str):
        self.h = _k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None)
        if self.h == INVALID_HANDLE_VALUE or self.h is None:
            raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        _k32.CloseHandle(self.h)

    def ioctl(self, code: int, inbuf: bytes = b"", out_size: int = 0) -> bytes:
        inb = ctypes.create_string_buffer(inbuf, len(inbuf)) if inbuf else None
        outb = ctypes.create_string_buffer(out_size) if out_size else None
        returned = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(self.h, code, inb, len(inbuf), outb, out_size, ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return outb.raw[: returned.value] if outb else b""


class Drive(DriveBase):
    def __init__(self, device: str | None = None):
        self._auto = device is None
        self.device = _normalize(device) if device else (find_optical_device() or "D:")

    def _follow_device(self) -> None:
        if self._auto and _k32.GetDriveTypeW(self.device + "\\") != DRIVE_CDROM:
            self.device = find_optical_device() or self.device

    @property
    def vlc_path(self) -> str:
        return f"/{self.device}/"

    @property
    def raw_path(self) -> str:
        return f"\\\\.\\{self.device}"

    def _open(self) -> _Handle:
        return _Handle(self.raw_path)

    @property
    def name(self) -> str:
        try:
            # STORAGE_PROPERTY_QUERY{StorageDeviceProperty, PropertyStandardQuery} -> STORAGE_DEVICE_DESCRIPTOR
            with self._open() as h:
                raw = h.ioctl(IOCTL_STORAGE_QUERY_PROPERTY, struct.pack("<II4x", 0, 0), 1024)
            vendor_off, product_off = struct.unpack_from("<II", raw, 8)
            parts = [_c_string(raw, off) for off in (vendor_off, product_off) if off]
            text = " ".join(p for p in parts if p)
            if text:
                return f"{text} ({self.device})"
        except (OSError, struct.error):
            pass
        return f"Optical drive ({self.device})"

    def status(self) -> DriveStatus:
        self._follow_device()
        try:
            with self._open() as h:
                try:
                    h.ioctl(IOCTL_STORAGE_CHECK_VERIFY2)
                except OSError as e:
                    if e.winerror == ERROR_MEDIA_CHANGED:
                        h.ioctl(IOCTL_STORAGE_CHECK_VERIFY2)  # the first check after a swap only reports the change
                    else:
                        raise
            return DriveStatus.DISC_OK
        except OSError as e:
            if getattr(e, "winerror", None) == ERROR_NOT_READY:
                return DriveStatus.NO_DISC  # Windows can't tell an open tray from an empty one cheaply
            return DriveStatus.NO_INFO

    def cd_tracks(self) -> tuple[list[CdTrack], int]:
        try:
            with self._open() as h:
                return parse_toc(h.ioctl(IOCTL_CDROM_READ_TOC, out_size=TOC_SIZE))
        except (OSError, ValueError):
            return [], 0

    def disc_kind(self) -> DiscKind:
        tracks, _ = self.cd_tracks()
        if not tracks:
            # DVDs answer READ_TOC too, but some drives refuse it; a readable filesystem still means data.
            return DiscKind.DATA if self.label_and_mount()[1] else DiscKind.UNKNOWN
        return disc_kind_from_tracks(tracks)

    def is_copy_protected(self) -> bool:
        # DVD_READ_STRUCTURE: LARGE_INTEGER offset, DVD_STRUCTURE_FORMAT, ULONG session, UCHAR layer (+pad to 24)
        request = struct.pack("<qIIB7x", 0, DVD_COPYRIGHT_DESCRIPTOR, 0, 0)
        try:
            with self._open() as h:
                raw = h.ioctl(IOCTL_DVD_READ_STRUCTURE, request, COPYRIGHT_BUFFER_SIZE)
            return is_css_protected(copyright_protection_type(raw))
        except (OSError, ValueError):
            return False

    def mcn(self) -> str:
        # CDROM_SUB_Q_DATA_FORMAT: Format, Track
        try:
            with self._open() as h:
                raw = h.ioctl(IOCTL_CDROM_READ_Q_CHANNEL, bytes([IOCTL_CDROM_MEDIA_CATALOG, 0]), MCN_BUFFER_SIZE)
            return parse_mcn(raw)
        except OSError:
            return ""

    def cd_info_report(self) -> str:
        try:
            return subprocess.run(
                ["cd-info", "--no-header", "--no-device-info", "--no-disc-mode", "--no-ioctl", "--no-vcd", "-q",
                 self.device],
                capture_output=True, text=True, errors="replace", timeout=30, **NO_WINDOW,
            ).stdout  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            return ""

    def size_bytes(self) -> int:
        try:
            with self._open() as h:
                return struct.unpack("<q", h.ioctl(IOCTL_DISK_GET_LENGTH_INFO, out_size=8))[0]
        except (OSError, struct.error):
            pass
        total = ctypes.c_ulonglong(0)
        if _k32.GetDiskFreeSpaceExW(self.device + "\\", None, ctypes.byref(total), None):
            return total.value
        return 0

    def label_and_mount(self) -> tuple[str, str]:
        root = self.device + "\\"
        label = ctypes.create_unicode_buffer(261)
        if not _k32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0):
            return "", ""
        return label.value, root

    def _media_ioctl(self, code: int) -> None:
        def run() -> None:
            try:
                with self._open() as h:
                    h.ioctl(code)
            except OSError:
                pass

        threading.Thread(target=run, daemon=True).start()

    def eject(self) -> None:
        self._media_ioctl(IOCTL_STORAGE_EJECT_MEDIA)

    def close_tray(self) -> None:
        self._media_ioctl(IOCTL_STORAGE_LOAD_MEDIA)


def _c_string(raw: bytes, offset: int) -> str:
    if offset >= len(raw):
        return ""
    return raw[offset:].split(b"\0", 1)[0].decode("ascii", "replace").strip()
