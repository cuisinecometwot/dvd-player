"""Rules for the app's own disposable files: how long logs are kept, and how sizes are shown."""

from collections.abc import Iterable

DEFAULT_LOG_DAYS = 1.0
SECONDS_PER_DAY = 86_400


def log_retention_days(raw: str | None) -> float:
    """Days of logs to keep, from the environment variable's text; bad or missing values give the default."""
    try:
        days = float(raw) if raw is not None and raw.strip() else DEFAULT_LOG_DAYS
    except ValueError:
        return DEFAULT_LOG_DAYS
    return days if days >= 0 else DEFAULT_LOG_DAYS


def expired(entries: Iterable[tuple[str, float]], now: float, keep_days: float) -> list[str]:
    """Names of (name, modified-time) entries older than keep_days."""
    cutoff = now - keep_days * SECONDS_PER_DAY
    return [name for name, mtime in entries if mtime < cutoff]


def format_bytes(size: int) -> str:
    if size < 1000:
        return f"{size} bytes" if size != 1 else "1 byte"
    for unit in ("KB", "MB", "GB"):
        size /= 1000
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if size >= 100 else f"{size:.1f} {unit}"
    raise AssertionError("unreachable")
