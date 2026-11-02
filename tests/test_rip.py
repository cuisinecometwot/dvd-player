from domain.disc import DiscKind, disc_kind_from_code, is_css_protected
from domain.rip import (
    CdTrack,
    RipKind,
    audio_track_commands,
    audio_track_spans,
    dvd_title_command,
    eta_text,
    parse_progress_line,
    progress_fraction,
    rip_options,
    safe_filename,
)


def test_disc_kind_and_protection():
    assert disc_kind_from_code(100) is DiscKind.AUDIO
    assert disc_kind_from_code(101) is DiscKind.DATA
    assert disc_kind_from_code(105) is DiscKind.MIXED
    assert disc_kind_from_code(0) is DiscKind.UNKNOWN
    assert is_css_protected(1) and not is_css_protected(0)


def test_protected_disc_cannot_be_ripped():
    opts = rip_options(DiscKind.DATA, is_video_dvd=True, protected=True)
    assert opts.kinds == () and "copy-protected" in opts.blocked_reason


def test_rip_options_by_disc_type():
    assert rip_options(DiscKind.DATA, True, False).kinds == (RipKind.DVD_TITLE, RipKind.DISC_IMAGE)
    assert rip_options(DiscKind.AUDIO, False, False).kinds == (RipKind.AUDIO_TRACKS,)
    assert rip_options(DiscKind.DATA, False, False).kinds == (RipKind.DISC_IMAGE,)
    assert rip_options(DiscKind.UNKNOWN, False, False).blocked_reason


def test_audio_track_spans_skip_data_tracks():
    tracks = [CdTrack(1, 0), CdTrack(2, 75 * 60), CdTrack(3, 75 * 100, is_data=True)]
    assert audio_track_spans(tracks, 75 * 200) == [(1, 0, 60_000), (2, 60_000, 100_000)]
    assert audio_track_spans([CdTrack(1, 0, is_data=True)], 100) == []


def test_audio_track_commands_one_per_track():
    cmds = audio_track_commands("/dev/sr0", [(1, 0, 60_000), (2, 60_000, 90_500)], "Track {:02d}.flac")
    assert [d for _, d in cmds] == [60_000, 30_500]
    second, _ = cmds[1]
    assert second[second.index("-ss") + 1] == "60.000" and second[second.index("-t") + 1] == "30.500"
    assert second.index("-ss") < second.index("-i")  # input seek: don't decode the skipped audio
    assert second[-1] == "Track 02.flac"


def test_dvd_title_command():
    cmd = dvd_title_command("/dev/sr0", 2, "out.mkv")
    assert cmd[cmd.index("-title") + 1] == "2"
    assert ["-c", "copy"] == cmd[cmd.index("-c") : cmd.index("-c") + 2]
    assert cmd[-1] == "out.mkv"


def test_safe_filename():
    assert safe_filename('A/B: "C"?') == "A_B_ _C_"
    assert safe_filename(" .. ") == "disc"


def test_progress_parsing():
    assert parse_progress_line("out_time_us=1500000\n") == 1500
    assert parse_progress_line("out_time_ms=-5") == 0
    assert parse_progress_line("frame=12") is None
    assert progress_fraction(50, 200) == 0.25 and progress_fraction(5, 0) == 0.0


def test_eta_text():
    assert eta_text(0, 10) == "estimating…"
    assert eta_text(0.5, 20) == "less than a minute left"
    assert eta_text(0.25, 300) == "about 15 min left"


def test_unique_name():
    from domain.rip import unique_name

    taken = {"a.mkv", "a (2).mkv", "folder"}
    assert unique_name("b.mkv", taken.__contains__) == "b.mkv"
    assert unique_name("a.mkv", taken.__contains__) == "a (3).mkv"
    assert unique_name("folder", taken.__contains__) == "folder (2)"
