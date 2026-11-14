"""LRCLIB (lrclib.net) lyrics lookup: free, no API key; returns plain and time-synced lyrics."""

import json
import urllib.parse

from domain.lyrics import Lyrics, is_instrumental_title, pick_lyrics, search_queries
from dvdplayer import net

API = "https://lrclib.net/api"
TIMEOUT_S = 8


def _search(params: dict[str, str]) -> list[dict]:
    raw = net.get(f"{API}/search?{urllib.parse.urlencode(params)}", timeout=TIMEOUT_S)
    try:
        data = json.loads(raw) if raw else []
    except ValueError as e:
        raise net.NetworkError("bad response from LRCLIB") from e
    return data if isinstance(data, list) else []


def fetch(title: str, artist: str, duration_s: float) -> Lyrics | None:
    """Blocking; call from a worker thread. None for instrumentals and misses; raises net.NetworkError."""
    if is_instrumental_title(title):
        return None
    for params in search_queries(title, artist):
        if found := pick_lyrics(_search(params), title, artist, duration_s):
            return found
    return None
