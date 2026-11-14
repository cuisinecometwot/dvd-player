"""MusicBrainz lookups for audio CDs: by disc ID first, then by the disc's barcode (MCN)."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from domain.cdmeta import AlbumInfo, parse_release

API = "https://musicbrainz.org/ws/2"
USER_AGENT = "dvd-player/1.0 ( personal desktop tool )"
TIMEOUT_S = 6
REQUEST_GAP_S = 1.1  # MusicBrainz allows one request per second per client
RETRIES = 2
RELEASE_INC = "artist-credits+recordings"


class _Client:
    def __init__(self):
        self._last = 0.0

    def get(self, path: str, **params: str) -> dict | None:
        url = f"{API}/{path}?{urllib.parse.urlencode({**params, 'fmt': 'json'}, safe='+')}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        for attempt in range(RETRIES + 1):
            wait = self._last + REQUEST_GAP_S * (1 + 2 * attempt) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                    return json.load(response)
            except urllib.error.HTTPError as e:
                if e.code != 503:  # 503 is MusicBrainz throttling; anything else (404...) is final
                    return None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                return None  # offline: the user can still type everything in
        return None


def lookup(disc_id: str, barcode: str, track_count: int) -> AlbumInfo | None:
    """Blocking; call from a worker thread."""
    client = _Client()
    by_disc = client.get(f"discid/{urllib.parse.quote(disc_id)}", inc=RELEASE_INC) if disc_id else None
    for release in (by_disc or {}).get("releases", []):
        if info := parse_release(release, track_count):
            return info

    if not barcode:
        return None
    found = client.get("release", query=f"barcode:{barcode}")
    for candidate in (found or {}).get("releases", [])[:3]:
        if sum(m.get("track-count", 0) for m in candidate.get("media", [])) < track_count:
            continue
        release = client.get(f"release/{candidate['id']}", inc=RELEASE_INC)
        if release and (info := parse_release(release, track_count)):
            return info
    return None
