from domain.media import Title, apply_disc_info, clamp, display_aspect, fit_rect, format_duration, main_title


def test_format_duration():
    assert format_duration(0) == "0:00"
    assert format_duration(65_000) == "1:05"
    assert format_duration(3_723_000) == "1:02:03"
    assert format_duration(-10) == "0:00"


def test_main_title_prefers_longest_non_menu():
    titles = [Title(0, 9_000_000, is_menu=True), Title(1, 5_000_000), Title(2, 600_000)]
    assert main_title(titles).index == 1
    assert main_title([]) is None


def test_title_label():
    assert Title(0, 1).label == "Title 1"
    assert Title(0, 1, name="Bonus").label == "Bonus"


def test_display_aspect_applies_sar():
    # NTSC 16:9 DVD: 720x480 with SAR 32:27
    assert abs(display_aspect(720, 480, 32, 27) - 16 / 9) < 0.01
    assert display_aspect(640, 480) == 4 / 3


def test_fit_rect_letterbox_and_pillarbox():
    assert fit_rect(1600, 1200, 16 / 9) == (0, 150, 1600, 900)
    x, y, w, h = fit_rect(1920, 1080, 4 / 3)
    assert (y, h) == (0, 1080) and w == 1440 and x == 240
    assert fit_rect(0, 100, 1.5) == (0, 0, 0, 0)


def test_clamp():
    assert clamp(-1, 0, 10) == 0 and clamp(11, 0, 10) == 10 and clamp(5, 0, 10) == 5


def test_apply_disc_info_fills_unknown_durations():
    titles = [Title(0, 0), Title(1, 500), Title(2, 0)]
    merged = apply_disc_info(titles, [(9000, 16 / 9), (8000, 4 / 3)])
    assert [(t.duration_ms, t.aspect) for t in merged] == [(9000, 16 / 9), (500, 4 / 3), (0, 0.0)]
