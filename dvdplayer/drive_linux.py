"""Linux optical drive access: status ioctl, sysfs info, mount lookup, eject."""

import fcntl
import json
import os
import struct
import subprocess
from pathlib import Path

from domain.disc import DiscKind, DriveStatus, disc_kind_from_code, drive_status_from_code, is_css_protected
from domain.rip import CD_DATA_TRACK_FLAG, CdTrack
from dvdplayer.drive_base import DriveBase
from dvdplayer.proc import NO_WINDOW

CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDROMREADTOCHDR = 0x5305
CDROMREADTOCENTRY = 0x5306
CDROM_GET_MCN = 0x5311
DVD_READ_STRUCT = 0x5390
CDSL_CURRENT = 0x7FFFFFFF
CDROM_LBA = 0x01
CDROM_LEADOUT = 0xAA
DVD_STRUCT_COPYRIGHT = 0x01
# struct cdrom_tocentry: track, adr:4|ctrl:4, format, pad, int lba, datamode, pad
_TOCENTRY = struct.Struct("BBBxiBxxx")


def find_optical_device() -> str | None:
    """First optical drive the kernel knows about; USB drives can come back as sr1, sr2... after a replug."""
    names = sorted(p.name for p in Path("/sys/block").glob("sr*"))
    return f"/dev/{names[0]}" if names else None


class Drive(DriveBase):
    def __init__(self, device: str | None = None):
        # Without an explicit device, follow whichever optical drive is present.
        self._auto = device is None
        self.device = device or find_optical_device() or "/dev/sr0"

    def _follow_device(self) -> None:
        if self._auto and not os.path.exists(self.device):
            self.device = find_optical_device() or self.device

    @property
    def name(self) -> str:
        sysfs = Path("/sys/block") / Path(self.device).name / "device"
        try:
            parts = [(sysfs / f).read_text().strip() for f in ("vendor", "model")]
            return " ".join(p for p in parts if p)
        except OSError:
            return self.device

    def status(self) -> DriveStatus:
        self._follow_device()
        try:
            # O_NONBLOCK lets us query an empty drive or open tray without waiting.
            fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return DriveStatus.NO_INFO
        try:
            return drive_status_from_code(fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT))
        except OSError:
            return DriveStatus.NO_INFO
        finally:
            os.close(fd)

    def _ioctl(self, request: int, arg):
        fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, request, arg)
        finally:
            os.close(fd)

    def disc_kind(self) -> DiscKind:
        try:
            return disc_kind_from_code(self._ioctl(CDROM_DISC_STATUS, 0))
        except OSError:
            return DiscKind.UNKNOWN

    def is_copy_protected(self) -> bool:
        """True for CSS-encrypted DVDs; CDs (and drives that refuse the query) report False."""
        buf = bytearray(struct.pack("BBBB", DVD_STRUCT_COPYRIGHT, 0, 0, 0) + bytes(2048))
        try:
            self._ioctl(DVD_READ_STRUCT, buf)
        except OSError:
            return False
        return is_css_protected(buf[2])

    def cd_tracks(self) -> tuple[list[CdTrack], int]:
        """Audio CD table of contents: tracks and the lead-out LBA."""
        try:
            first, last = self._ioctl(CDROMREADTOCHDR, bytes(2))[:2]
            tracks = []
            for number in [*range(first, last + 1), CDROM_LEADOUT]:
                raw = self._ioctl(CDROMREADTOCENTRY, _TOCENTRY.pack(number, 0, CDROM_LBA, 0, 0))
                _, adr_ctrl, _, lba, _ = _TOCENTRY.unpack(raw)
                tracks.append(CdTrack(number, lba, bool((adr_ctrl >> 4) & CD_DATA_TRACK_FLAG)))
            return tracks[:-1], tracks[-1].start_lba
        except (OSError, struct.error, ValueError):
            return [], 0

    def mcn(self) -> str:
        """Media Catalog Number: the album's 13-digit EAN/UPC barcode, or "" when the disc has none."""
        try:
            raw = self._ioctl(CDROM_GET_MCN, bytes(14))
        except OSError:
            return ""
        digits = raw.split(b"\0", 1)[0].decode("ascii", "replace")
        return digits if digits.isdigit() and digits.strip("0") else ""

    def cd_info_report(self) -> str:
        """libcdio's disc report, which includes any CD-Text (~3 s: it reads the disc)."""
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
            return int((Path("/sys/block") / Path(self.device).name / "size").read_text()) * 512
        except (OSError, ValueError):
            return 0

    def label_and_mount(self) -> tuple[str, str]:
        try:
            out = subprocess.run(
                ["lsblk", "-J", "-o", "LABEL,MOUNTPOINT", self.device],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
                **NO_WINDOW,
            ).stdout
            dev = json.loads(out)["blockdevices"][0]
            return dev.get("label") or "", dev.get("mountpoint") or ""
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
            return "", ""

    def eject(self) -> None:
        # Unmount via udisks first so eject doesn't need root; runs detached to keep the UI responsive.
        cmd = f"udisksctl unmount -b {self.device} >/dev/null 2>&1; eject {self.device}"
        subprocess.Popen(["sh", "-c", cmd])

    def close_tray(self) -> None:
        subprocess.Popen(["eject", "-t", self.device])
