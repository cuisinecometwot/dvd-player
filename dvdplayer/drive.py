"""Linux optical drive access: status ioctl, sysfs info, mount lookup, eject."""

import fcntl
import json
import os
import struct
import subprocess
from pathlib import Path

from domain.disc import (
    DiscKind,
    DriveStatus,
    describe_regions,
    disc_kind_from_code,
    drive_status_from_code,
    is_css_protected,
    parse_region_mask,
    regions_from_mask,
)
from domain.ifo import parse_title_table, parse_vts_aspect, parse_vts_title_durations
from domain.rip import CD_DATA_TRACK_FLAG, CdTrack

CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDROMREADTOCHDR = 0x5305
CDROMREADTOCENTRY = 0x5306
DVD_READ_STRUCT = 0x5390
CDSL_CURRENT = 0x7FFFFFFF
CDROM_LBA = 0x01
CDROM_LEADOUT = 0xAA
DVD_STRUCT_COPYRIGHT = 0x01
# struct cdrom_tocentry: track, adr:4|ctrl:4, format, pad, int lba, datamode, pad
_TOCENTRY = struct.Struct("BBBxiBxxx")


class Drive:
    def __init__(self, device: str = "/dev/sr0"):
        self.device = device

    @property
    def name(self) -> str:
        sysfs = Path("/sys/block") / Path(self.device).name / "device"
        try:
            parts = [(sysfs / f).read_text().strip() for f in ("vendor", "model")]
            return " ".join(p for p in parts if p)
        except OSError:
            return self.device

    def status(self) -> DriveStatus:
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
            ).stdout
            dev = json.loads(out)["blockdevices"][0]
            return dev.get("label") or "", dev.get("mountpoint") or ""
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
            return "", ""

    @staticmethod
    def is_dvd_video(mountpoint: str) -> bool:
        return bool(mountpoint) and (Path(mountpoint) / "VIDEO_TS").is_dir()

    @staticmethod
    def region_text(mountpoint: str) -> str:
        try:
            with open(Path(mountpoint) / "VIDEO_TS" / "VIDEO_TS.IFO", "rb") as f:
                return describe_regions(regions_from_mask(parse_region_mask(f.read(64))))
        except (OSError, ValueError):
            return "Unknown"

    @staticmethod
    def title_info(mountpoint: str) -> list[tuple[int, float]]:
        """(duration_ms, aspect) per disc title, in title order; empty if unreadable."""
        root = Path(mountpoint) / "VIDEO_TS"
        try:
            entries = parse_title_table((root / "VIDEO_TS.IFO").read_bytes())
            vts_cache: dict[int, tuple[dict[int, int], float]] = {}
            info = []
            for e in entries:
                if e.vts not in vts_cache:
                    vts = (root / f"VTS_{e.vts:02d}_0.IFO").read_bytes()
                    vts_cache[e.vts] = (parse_vts_title_durations(vts), parse_vts_aspect(vts))
                durations, aspect = vts_cache[e.vts]
                info.append((durations.get(e.vts_title, 0), aspect))
            return info
        except (OSError, ValueError, IndexError):
            return []

    def eject(self) -> None:
        # Unmount via udisks first so eject doesn't need root; runs detached to keep the UI responsive.
        cmd = f"udisksctl unmount -b {self.device} >/dev/null 2>&1; eject {self.device}"
        subprocess.Popen(["sh", "-c", cmd])

    def close_tray(self) -> None:
        subprocess.Popen(["eject", "-t", self.device])
