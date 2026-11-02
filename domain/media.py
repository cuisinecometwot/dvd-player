"""Playback-agnostic models and math for titles, chapters, tracks and video layout."""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Chapter:
    index: int
    offset_ms: int
    duration_ms: int


@dataclass(frozen=True)
class Title:
    index: int
    duration_ms: int
    is_menu: bool = False
    name: str = ""
    aspect: float = 0.0  # 0 = unknown

    @property
    def label(self) -> str:
        return self.name or f"Title {self.index + 1}"


@dataclass(frozen=True)
class Track:
    id: int
    name: str


def format_duration(ms: int) -> str:
    total = max(0, ms) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def main_title(titles: list[Title]) -> Title | None:
    """The feature film is almost always the longest non-menu title."""
    candidates = [t for t in titles if not t.is_menu] or titles
    return max(candidates, key=lambda t: t.duration_ms, default=None)


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def display_aspect(width: int, height: int, sar_num: int = 0, sar_den: int = 0) -> float:
    """Display aspect ratio; DVD pixels are non-square, so apply the sample aspect ratio."""
    if width <= 0 or height <= 0:
        return 16 / 9
    sar = sar_num / sar_den if sar_num > 0 and sar_den > 0 else 1.0
    return width * sar / height


def fit_rect(box_w: float, box_h: float, aspect: float) -> tuple[float, float, float, float]:
    """Largest rect of the given aspect centered in the box (letterbox/pillarbox)."""
    if box_w <= 0 or box_h <= 0 or aspect <= 0:
        return 0.0, 0.0, 0.0, 0.0
    if box_w / box_h > aspect:
        w, h = box_h * aspect, box_h
    else:
        w, h = box_w, box_w / aspect
    return (box_w - w) / 2, (box_h - h) / 2, w, h


def apply_disc_info(titles: list[Title], info: list[tuple[int, float]]) -> list[Title]:
    """Fill in (duration_ms, aspect) read from the disc's IFO files, matched by title order."""
    merged = []
    for t in titles:
        if t.index < len(info):
            duration, aspect = info[t.index]
            t = replace(t, duration_ms=t.duration_ms or duration, aspect=aspect)
        merged.append(t)
    return merged
