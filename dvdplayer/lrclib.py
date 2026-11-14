"""LRCLIB (lrclib.net) lyrics lookup: free, no API key; returns plain and time-synced lyrics."""

import json
import urllib.error
import urllib.parse
import urllib.request

from domain.lyrics import Lyrics, is_instrumental_title, pick_lyrics, search_queries

API = "https://lrclib.net/api"
USER_AGENT = "dvd-player/1.0 ( personal desktop tool )"
TIMEOUT_S = 8


def _search(params: dict[str, str]) -> list[dict]:
    url = f"{API}/search?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def fetch(title: str, artist: str, duration_s: float) -> Lyrics | None:
    """Blocking; call from a worker thread. None for instrumentals, misses and network errors."""
    if is_instrumental_title(title):
        return None
    for params in search_queries(title, artist):
        if found := pick_lyrics(_search(params), title, artist, duration_s):
            return found
    return None
