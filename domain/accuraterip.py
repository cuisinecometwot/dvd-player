"""AccurateRip: disc IDs, the database entry format, track checksums, and drive read offsets.

AccurateRip holds checksums other people's rips of the same pressing produced; a matching checksum means the
rip is bit-identical to theirs, i.e. no read errors. Checksums only line up once the drive's read offset is
corrected, so the offset table lives here too.
"""

import html
import re
import struct
import sys
from array import array
from dataclasses import dataclass
from itertools import repeat
from operator import mul, rshift

from domain.rip import CdTrack

SAMPLES_PER_SECTOR = 588  # stereo 16-bit frames: 2352 bytes / 4
SKIPPED_EDGE = 5 * SAMPLES_PER_SECTOR  # first/last 5 sectors of the disc are ignored: drives can't all reach them
SESSION_GAP = 11400  # lead-out + lead-in + pregap between the audio session and an Enhanced CD's data session
DB_URL = "http://www.accuraterip.com/accuraterip/"
OFFSETS_URL = "http://www.accuraterip.com/driveoffsets.htm"
_MASK = 0xFFFFFFFF
_CHUNK = 1 << 20


@dataclass(frozen=True)
class DiscIds:
    track_count: int  # audio tracks only
    id1: int
    id2: int
    cddb: int

    @property
    def path(self) -> str:
        """Path of the database entry below DB_URL."""
        id1 = f"{self.id1:08x}"
        name = f"dBAR-{self.track_count:03d}-{id1}-{self.id2:08x}-{self.cddb:08x}.bin"
        return f"{id1[-1]}/{id1[-2]}/{id1[-3]}/{name}"

    @property
    def text(self) -> str:
        return f"{self.track_count:03d}-{self.id1:08x}-{self.id2:08x}-{self.cddb:08x}"


def _digit_sum(n: int) -> int:
    return sum(int(d) for d in str(n))


def cddb_disc_id(tracks: list[CdTrack], leadout_lba: int) -> int:
    """freedb/CDDB disc ID over the whole TOC (data tracks included), as AccurateRip uses it."""
    seconds = [(t.start_lba + 150) // 75 for t in tracks]
    n = sum(_digit_sum(s) for s in seconds)
    length = (leadout_lba + 150) // 75 - seconds[0]
    return ((n % 0xFF) << 24 | length << 8 | len(tracks)) & _MASK


def disc_ids(tracks: list[CdTrack], leadout_lba: int) -> DiscIds | None:
    """None for discs AccurateRip can't describe (no audio, or data tracks before the audio)."""
    tracks = sorted(tracks, key=lambda t: t.start_lba)
    audio = [t for t in tracks if not t.is_data]
    if not audio or tracks[: len(audio)] != audio:
        return None
    end = leadout_lba if len(audio) == len(tracks) else tracks[len(audio)].start_lba - SESSION_GAP
    id1 = sum(t.start_lba for t in audio) + end
    id2 = sum(max(t.start_lba, 1) * i for i, t in enumerate(audio, start=1)) + end * (len(audio) + 1)
    return DiscIds(len(audio), id1 & _MASK, id2 & _MASK, cddb_disc_id(tracks, leadout_lba))


@dataclass(frozen=True)
class TrackEntry:
    confidence: int  # how many submitted rips agree
    checksum: int


def parse_entry(data: bytes) -> list[list[TrackEntry]]:
    """The .bin entry: one block per known pressing, each a list of per-track checksums."""
    pressings: list[list[TrackEntry]] = []
    pos = 0
    while pos + 13 <= len(data):
        count = data[pos]
        end = pos + 13 + 9 * count
        if count == 0 or end > len(data):
            break
        tracks = [TrackEntry(data[p], struct.unpack_from("<I", data, p + 1)[0]) for p in range(pos + 13, end, 9)]
        pressings.append(tracks)
        pos = end
    return pressings


def checksums(pcm: bytes, first: bool, last: bool) -> tuple[int, int]:
    """(v1, v2) AccurateRip checksums of one track's 16-bit stereo little-endian PCM."""
    words = array("I")
    words.frombytes(pcm[: len(pcm) - len(pcm) % 4])
    if sys.byteorder == "big":
        words.byteswap()
    # Each sample frame is weighted by its 1-based position; the disc's outer 5 sectors don't count.
    start = SKIPPED_EDGE if first else 1
    stop = len(words) - SKIPPED_EDGE if last else len(words)
    if stop < start:
        return 0, 0
    total = high = 0
    for begin in range(start, stop + 1, _CHUNK):  # chunked: a 4-minute track is 10 million products
        end = min(begin + _CHUNK, stop + 1)
        products = list(map(mul, range(begin, end), words[begin - 1 : end - 1]))
        total += sum(products)
        high += sum(map(rshift, products, repeat(32)))  # v2 also adds the high half of each 64-bit product
    return total & _MASK, (total + high) & _MASK


@dataclass(frozen=True)
class TrackResult:
    confidence: int  # 0 = no match
    version: int  # 1 or 2 when matched
    in_database: bool  # False: nobody submitted this disc (or not this track count)

    @property
    def accurate(self) -> bool:
        return self.confidence > 0

    def text(self) -> str:
        if not self.in_database:
            return "not in AccurateRip"
        if self.accurate:
            return f"accurately ripped (confidence {self.confidence}, v{self.version})"
        return "doesn't match AccurateRip"


def verify(pressings: list[list[TrackEntry]], index: int, v1: int, v2: int) -> TrackResult:
    """Best match for the track at 0-based index among all pressings."""
    rows = [p[index] for p in pressings if index < len(p)]
    if not rows:
        return TrackResult(0, 0, False)
    best = TrackResult(0, 0, True)
    for row in rows:
        version = 2 if row.checksum == v2 else 1 if row.checksum == v1 else 0
        if version and row.confidence > best.confidence:
            best = TrackResult(row.confidence, version, True)
    return best


def summary(results: list[TrackResult]) -> str:
    if not results:
        return ""
    if not any(r.in_database for r in results):
        return "Disc not in AccurateRip, so it can't be verified"
    good = sum(r.accurate for r in results)
    if good == len(results):
        return f"All {good} tracks accurately ripped (AccurateRip)"
    return f"{good} of {len(results)} tracks verified by AccurateRip — see rip.log"


# ---------- drive read offsets ----------

# AccurateRip lists some drives under a different vendor than the one the drive reports.
VENDOR_ALIASES = {"MATSHITA": "PANASONIC", "HL-DT-ST": "LG ELECTRONICS", "JLMS": "LITE-ON"}
_ROW = re.compile(
    r"<tr>\s*<td[^>]*>\s*<font[^>]*>(.*?)</font>\s*</td>\s*<td[^>]*>\s*<font[^>]*>\s*([+-]?\d+)\s*</font>",
    re.S | re.I,
)


def _norm(text: str) -> str:
    return " ".join(text.upper().split())


def parse_offsets(page: str) -> dict[str, int]:
    """'VENDOR MODEL' (normalized) -> read offset in samples, from AccurateRip's drive offset page.

    Purged drives (inconsistent offsets) have no number and are left out.
    """
    table: dict[str, int] = {}
    for name, offset in _ROW.findall(page):
        vendor, sep, model = html.unescape(re.sub(r"<[^>]+>", "", name)).partition(" - ")
        if sep:
            table[_norm(f"{vendor} {model}")] = int(offset)
    return table


def drive_offset(table: dict[str, int], drive_name: str) -> int | None:
    """Offset for a drive named as the OS reports it ('MATSHITA DVD-RAM UJ8B0AW', maybe '... (D:)')."""
    name = _norm(re.sub(r"\s*\([A-Za-z]:\)\s*$", "", drive_name))
    if not name:
        return None
    vendor, _, model = name.partition(" ")
    for key in (name, f"{VENDOR_ALIASES.get(vendor, vendor)} {model}"):
        if key in table:
            return table[key]
    # Same model under another vendor label (OEM rebrands): only if they all agree.
    found = {offset for key, offset in table.items() if model and key.endswith(" " + model)}
    return found.pop() if len(found) == 1 else None
