"""Lyrics matching rules for LRCLIB results and .lrc output (no network here)."""

import re
from dataclasses import dataclass

DURATION_TOLERANCE_S = 3  # same recording/arrangement; synced timings would drift beyond this

_INSTRUMENTAL = re.compile(r"instrumental|off[\s-]?vocal|karaoke|カラオケ|backing track|\binst\b\.?", re.IGNORECASE)
_TRAILING_BRACKET = re.compile(r"\s*[\[(（【][^\[\]()（）【】]*[\])）】]\s*$")
_ARTIST_SPLIT = re.compile(r"\s*(?:[、,&/・×]|\bfeat\.?|\bx\b)\s*", re.IGNORECASE)
_TIMESTAMP = re.compile(r"\[\d+:\d+(?:[.:]\d+)?\]")


@dataclass(frozen=True)
class Lyrics:
    plain: str
    synced: str = ""


def is_instrumental_title(title: str) -> bool:
    return bool(_INSTRUMENTAL.search(title))


def base_title(title: str) -> str:
    """'Song [X Solo ver.] (Remaster)' -> 'Song': version suffixes rarely exist as separate LRCLIB entries."""
    while (stripped := _TRAILING_BRACKET.sub("", title).strip()) and stripped != title:
        title = stripped
    return title.strip()


def artist_names(artist: str) -> list[str]:
    return [a for a in _ARTIST_SPLIT.split(artist) if a]


def search_queries(title: str, artist: str) -> list[dict[str, str]]:
    """Most to least specific; the last one drops the artist because LRCLIB often has it romanized."""
    first_artist = (artist_names(artist) or [""])[0]
    base = base_title(title)
    queries = [{"track_name": title, "artist_name": first_artist}]
    if base != title:
        queries.append({"track_name": base, "artist_name": first_artist})
    queries.append({"track_name": base})
    return [{k: v for k, v in q.items() if v} for q in queries]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def pick_lyrics(results: list[dict], title: str, artist: str, duration_s: float) -> Lyrics | None:
    """Best LRCLIB search result for a track, or None when nothing is close enough to trust."""
    wanted_titles = {_norm(title), _norm(base_title(title))}
    names = [_norm(a) for a in artist_names(artist)]
    candidates = []
    for r in results:
        if r.get("instrumental") or not (r.get("plainLyrics") or r.get("syncedLyrics")):
            continue
        diff = abs((r.get("duration") or 0) - duration_s)
        if diff > DURATION_TOLERANCE_S:
            continue
        found_title = _norm(r.get("trackName") or "")
        if found_title not in wanted_titles and _norm(base_title(r.get("trackName") or "")) not in wanted_titles:
            continue
        found_artist = _norm(r.get("artistName") or "")
        artist_match = any(n and n in found_artist for n in names)
        exact_version = found_title == _norm(title)
        # Prefer: synced, right artist, the exact version, then closest length.
        rank = (not r.get("syncedLyrics"), not artist_match, not exact_version, diff)
        candidates.append((rank, r))
    if not candidates:
        return None
    best = min(candidates, key=lambda c: c[0])[1]
    synced = best.get("syncedLyrics") or ""
    return Lyrics(plain=best.get("plainLyrics") or strip_timestamps(synced), synced=synced)


def strip_timestamps(synced: str) -> str:
    return "\n".join(_TIMESTAMP.sub("", line).strip() for line in synced.splitlines()).strip()


def lrc_text(synced: str, title: str, artist: str, album: str) -> str:
    header = [f"[ti:{title}]", f"[ar:{artist}]", f"[al:{album}]"]
    return "\n".join([*header, synced.strip()]) + "\n"


def lrc_path_for(flac_path: str) -> str:
    stem = flac_path[: -len(".flac")] if flac_path.endswith(".flac") else flac_path
    return f"{stem}.lrc"


def lyrics_tag(lyrics: Lyrics) -> list[str]:
    return ["-metadata", f"LYRICS={lyrics.plain}"] if lyrics.plain else []
