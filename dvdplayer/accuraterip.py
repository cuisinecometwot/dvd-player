"""AccurateRip lookups: the drive's read offset (table cached locally) and the disc's checksum entry."""

import json
import logging
import os

from domain.accuraterip import DB_URL, OFFSETS_URL, DiscIds, TrackEntry, drive_offset, parse_entry, parse_offsets
from dvdplayer import appdata, net

TIMEOUT_S = 20
log = logging.getLogger(__name__)


def _offsets_file():
    return appdata.cache_root() / "drive-offsets.json"


def _offset_table() -> dict[str, int]:
    """The whole drive table (~1.7 MB page, ~5000 drives); fetched once, then read from the cache."""
    path = _offsets_file()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    page = net.get(OFFSETS_URL, timeout=TIMEOUT_S, max_bytes=8 * 2**20)
    table = parse_offsets((page or b"").decode("latin-1"))
    if table:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(table), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
    return table


def read_offset(drive_name: str) -> tuple[int | None, str]:
    """(offset in samples or None, where it came from / why it's missing). Blocking; worker thread only."""
    try:
        table = _offset_table()
    except net.NetworkError:
        return None, "AccurateRip drive list unreachable"
    offset = drive_offset(table, drive_name)
    log.info("read offset for %r: %s", drive_name, offset)
    return (offset, "AccurateRip drive list") if offset is not None else (None, "drive not in AccurateRip's list")


def fetch_entry(ids: DiscIds) -> list[list[TrackEntry]]:
    """Known checksums for this disc ([] when nobody submitted it). Raises net.NetworkError."""
    data = net.get(DB_URL + ids.path, timeout=TIMEOUT_S, max_bytes=2**20)
    return parse_entry(data) if data else []
