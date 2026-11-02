"""Facts about the optical drive and disc, derived from raw values only."""

from enum import Enum

VMG_MAGIC = b"DVDVIDEO-VMG"
# VMG category byte in VIDEO_TS.IFO; a set bit means "not playable in that region".
REGION_MASK_OFFSET = 0x23
REGION_COUNT = 8


class DriveStatus(Enum):
    NO_INFO = 0
    NO_DISC = 1
    TRAY_OPEN = 2
    NOT_READY = 3
    DISC_OK = 4

    @property
    def label(self) -> str:
        return {
            DriveStatus.NO_INFO: "Unknown",
            DriveStatus.NO_DISC: "No disc",
            DriveStatus.TRAY_OPEN: "Tray open",
            DriveStatus.NOT_READY: "Reading disc…",
            DriveStatus.DISC_OK: "Disc ready",
        }[self]


def drive_status_from_code(code: int) -> DriveStatus:
    """Map a Linux CDROM_DRIVE_STATUS ioctl result to a status."""
    try:
        return DriveStatus(code)
    except ValueError:
        return DriveStatus.NO_INFO


def parse_region_mask(vmg_ifo: bytes) -> int:
    if not vmg_ifo.startswith(VMG_MAGIC) or len(vmg_ifo) <= REGION_MASK_OFFSET:
        raise ValueError("not a DVD-Video VIDEO_TS.IFO")
    return vmg_ifo[REGION_MASK_OFFSET]


def regions_from_mask(mask: int) -> list[int]:
    return [r for r in range(1, REGION_COUNT + 1) if not mask & (1 << (r - 1))]


def describe_regions(regions: list[int]) -> str:
    if len(regions) == REGION_COUNT:
        return "Region-free"
    if not regions:
        return "No region"
    prefix = "Region" if len(regions) == 1 else "Regions"
    return f"{prefix} {', '.join(map(str, regions))}"
