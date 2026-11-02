import pytest

from domain.disc import (
    DriveStatus,
    describe_regions,
    drive_status_from_code,
    parse_region_mask,
    regions_from_mask,
)


def make_ifo(mask: int) -> bytes:
    data = bytearray(b"DVDVIDEO-VMG" + bytes(64))
    data[0x23] = mask
    return bytes(data)


def test_region_2_disc():
    assert regions_from_mask(parse_region_mask(make_ifo(0xFD))) == [2]


def test_region_free_disc():
    regions = regions_from_mask(0x00)
    assert regions == list(range(1, 9))
    assert describe_regions(regions) == "Region-free"


def test_multi_region_description():
    assert describe_regions(regions_from_mask(0xFC)) == "Regions 1, 2"
    assert describe_regions([2]) == "Region 2"


def test_rejects_non_dvd_ifo():
    with pytest.raises(ValueError):
        parse_region_mask(b"garbage" * 10)


def test_drive_status_mapping():
    assert drive_status_from_code(4) is DriveStatus.DISC_OK
    assert drive_status_from_code(2) is DriveStatus.TRAY_OPEN
    assert drive_status_from_code(-5) is DriveStatus.NO_INFO
