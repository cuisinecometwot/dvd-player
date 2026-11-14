from domain.disc import DiscKind, disc_kind_from_code, is_css_protected
from domain.rip import (
    CdTrack,
    RipKind,
    audio_track_commands,
    audio_track_spans,
    dvd_title_command,
    eta_text,
    opus_command,
    parse_progress_line,
    progress_fraction,
    rip_options,
    safe_filename,
    through_file,
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
    cmds = audio_track_commands(
        "/dev/sr0",
        [(1, 0, 60_000), (2, 60_000, 90_500)],
        {1: ("01.flac", []), 2: ("02.flac", ["-metadata", "title=B"])},
    )
    assert [d for _, d in cmds] == [60_000, 30_500]
    (reader, encoder), _ = cmds[1]
    # cd-paranoia reads the whole track itself: ffmpeg's libcdio demuxer hangs at the lead-out.
    assert reader[0] == "cd-paranoia" and "2-2" in reader and reader[-1] == "-"
    assert reader[reader.index("--force-cdrom-device") + 1] == "/dev/sr0"
    assert encoder[0] == "ffmpeg" and "pipe:0" in encoder and "libcdio" not in encoder
    assert encoder[-3:] == ["-metadata", "title=B", "02.flac"]


def test_audio_track_commands_embed_cover():
    [((_, encoder), _)] = audio_track_commands("/dev/sr0", [(1, 0, 1000)], {1: ("01.flac", [])}, "cover.jpg")
    assert encoder[encoder.index("pipe:0") + 1 : encoder.index("pipe:0") + 3] == ["-i", "cover.jpg"]
    assert "attached_pic" in encoder and encoder[-1] == "01.flac"
    [((_, plain), _)] = audio_track_commands("/dev/sr0", [(1, 0, 1000)], {1: ("01.flac", [])})
    assert "-map" not in plain


def test_opus_command():
    [cmd] = opus_command("01.flac", "1.ffmeta", "01.opus")
    assert cmd[cmd.index("-map_metadata") + 1] == "1"  # tags + cover come from the metadata file
    assert cmd[cmd.index("-c:a") + 1] == "libopus" and cmd[cmd.index("-b:a") + 1] == "160k"
    assert ["-map", "0:a"] == cmd[cmd.index("-map") : cmd.index("-map") + 2] and cmd[-1] == "01.opus"


def test_dvd_title_command():
    [cmd] = dvd_title_command("/dev/sr0", 2, "out.mkv")
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


def test_audio_track_commands_can_use_original_cdparanoia():
    [((reader, _), _)] = audio_track_commands("/dev/sr1", [(1, 0, 1000)], {1: ("01.flac", [])}, reader="cdparanoia")
    assert reader[0] == "cdparanoia" and "1-1" in reader


def test_through_file_splits_the_pipeline_at_a_wav():
    [(pipeline, _)] = audio_track_commands("D:", [(3, 0, 1000)], {3: ("03.flac", ["-metadata", "title=C"])})
    read, encode = through_file(pipeline, "w/3.wav")
    assert len(read) == 1 and read[0][0] == "cd-paranoia" and read[0][-1] == "w/3.wav" and "3-3" in read[0]
    assert len(encode) == 1 and "pipe:0" not in encode[0]
    assert encode[0][encode[0].index("w/3.wav") - 1] == "-i" and encode[0][-1] == "03.flac"
