"""What every platform's drive backend offers; the DVD-Video file parsing is shared."""

from pathlib import Path

from domain.disc import DiscKind, DriveStatus, describe_regions, parse_region_mask, regions_from_mask
from domain.ifo import parse_title_table, parse_vts_aspect, parse_vts_title_durations
from domain.rip import CdTrack


class DriveBase:
    device: str  # what ffmpeg and cd-paranoia are given

    @property
    def vlc_path(self) -> str:
        """Path part of the dvdsimple:// and cdda:// MRLs."""
        return self.device

    @property
    def raw_path(self) -> str:
        """What open() reads the whole disc from (ISO rips)."""
        return self.device

    @property
    def name(self) -> str:
        raise NotImplementedError

    def status(self) -> DriveStatus:
        raise NotImplementedError

    def disc_kind(self) -> DiscKind:
        raise NotImplementedError

    def is_copy_protected(self) -> bool:
        raise NotImplementedError

    def cd_tracks(self) -> tuple[list[CdTrack], int]:
        raise NotImplementedError

    def mcn(self) -> str:
        raise NotImplementedError

    def cd_info_report(self) -> str:
        raise NotImplementedError

    def size_bytes(self) -> int:
        raise NotImplementedError

    def label_and_mount(self) -> tuple[str, str]:
        raise NotImplementedError

    def eject(self) -> None:
        raise NotImplementedError

    def close_tray(self) -> None:
        raise NotImplementedError

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
