"""Cover Art Archive (MusicBrainz's image store): front cover for a release."""

from dvdplayer import net

API = "https://coverartarchive.org/release"
TIMEOUT_S = 15
MAX_BYTES = 8 * 2**20
# Thumbnails are always JPEG; 1200 px is plenty for display and embedding. Older uploads may lack it.
SIZES = ("front-1200", "front-500")


def fetch_front(release_id: str) -> bytes | None:
    """Blocking; call from a worker thread. None when the release has no front cover; raises net.NetworkError."""
    for size in SIZES:
        data = net.get(f"{API}/{release_id}/{size}", timeout=TIMEOUT_S, max_bytes=MAX_BYTES)
        if data is not None and data[:3] == b"\xff\xd8\xff":
            return data  # None: this size (or any cover) doesn't exist
    return None
