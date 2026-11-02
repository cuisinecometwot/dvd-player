"""Audio CD navigation rules (track numbers are the disc's own 1-based numbers)."""

RESTART_THRESHOLD_MS = 3000


def track_after(tracks: list[int], current: int) -> int | None:
    later = [t for t in tracks if t > current]
    return min(later) if later else None


def track_before(tracks: list[int], current: int, position_ms: int) -> int:
    """Like most players: 'previous' restarts the track unless you're in its first few seconds."""
    if position_ms > RESTART_THRESHOLD_MS:
        return current
    earlier = [t for t in tracks if t < current]
    return max(earlier) if earlier else current
