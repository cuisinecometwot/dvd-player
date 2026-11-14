import pytest

from domain.housekeeping import DEFAULT_LOG_DAYS, expired, format_bytes, log_retention_days


@pytest.mark.parametrize(
    ("raw", "days"),
    [(None, DEFAULT_LOG_DAYS), ("", DEFAULT_LOG_DAYS), ("3", 3.0), ("0.5", 0.5), ("0", 0.0), ("-1", DEFAULT_LOG_DAYS),
     ("week", DEFAULT_LOG_DAYS)],
)  # fmt: skip
def test_log_retention_days(raw, days):
    assert log_retention_days(raw) == days


def test_expired_keeps_the_last_day():
    now = 1_000_000.0
    entries = [("old.log", now - 90_000), ("fresh.log", now - 3_600), ("edge.log", now - 86_400)]
    assert expired(entries, now, 1) == ["old.log"]
    assert expired(entries, now, 0) == ["old.log", "fresh.log", "edge.log"]


@pytest.mark.parametrize(
    ("size", "text"),
    [(0, "0 bytes"), (1, "1 byte"), (999, "999 bytes"), (1_500, "1.5 KB"), (12_345_678, "12.3 MB"),
     (250_000_000, "250 MB"), (3_200_000_000, "3.2 GB")],
)  # fmt: skip
def test_format_bytes(size, text):
    assert format_bytes(size) == text
