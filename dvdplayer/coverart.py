"""Cover Art Archive (MusicBrainz's image store): front cover for a release."""

import urllib.error
import urllib.request

API = "https://coverartarchive.org/release"
USER_AGENT = "dvd-player/1.0 ( personal desktop tool )"
TIMEOUT_S = 15
MAX_BYTES = 8 * 2**20
# Thumbnails are always JPEG; 1200 px is plenty for display and embedding. Older uploads may lack it.
SIZES = ("front-1200", "front-500")


def fetch_front(release_id: str) -> bytes | None:
    """Blocking; call from a worker thread. None when the release has no front cover or on network errors."""
    for size in SIZES:
        request = urllib.request.Request(f"{API}/{release_id}/{size}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                data = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue  # this size (or any cover) doesn't exist
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
        if data[:3] == b"\xff\xd8\xff" and len(data) <= MAX_BYTES:
            return data
    return None
