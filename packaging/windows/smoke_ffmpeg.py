"""Runs the app's own FFmpeg commands against a (minimal) FFmpeg build, so a missing component fails the build.

Usage: python packaging/windows/smoke_ffmpeg.py <bin dir of the build under test> <full ffmpeg for fixtures> [keep dir]
Run from the repo root. Set LOCAL_TEST=1 when the build has no dvdvideo demuxer / libopus encoder.
ISOLATE=1 leaves only the bin dir and Windows' own folders on PATH, so a DLL missing from the bundle fails here
instead of being found in the build machine's MSYS2. With a keep dir, the rips are kept there (for smoke_vlc.py).
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from domain.cdmeta import ffmetadata, metadata_args, picture_block  # noqa: E402
from domain.rip import _FFMPEG, audio_track_commands, opus_command, through_file  # noqa: E402

LOCAL = os.environ.get("LOCAL_TEST") == "1"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def _dir(path: Path) -> Path:
    path.mkdir(exist_ok=True)  # the app hands _embedded_cover an existing temp folder
    return path


def run(cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=env)


def main() -> int:
    bin_dir, fixture_ffmpeg = Path(sys.argv[1]).resolve(), sys.argv[2]
    fixture_env = dict(os.environ)  # the full FFmpeg must not pick up the minimal build's libraries
    # The commands say plain "ffmpeg"/"ffprobe", exactly as the app runs them.
    base_path = os.environ["PATH"]
    if os.environ.get("ISOLATE") == "1":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        base_path = os.pathsep.join([os.path.join(root, "System32"), root])
    os.environ["PATH"] = os.pathsep.join([str(bin_dir), base_path])
    lib = bin_dir.parent / "lib"
    if lib.is_dir():
        os.environ["LD_LIBRARY_PATH"] = str(lib)
    from dvdplayer import localmedia  # after PATH is set; imports nothing from GTK

    ffmpeg = shutil.which("ffmpeg")
    check("ffmpeg under test is first on PATH", bool(ffmpeg) and Path(ffmpeg).parent == bin_dir, str(ffmpeg))
    demuxers = run(["ffmpeg", "-hide_banner", "-demuxers"]).stdout
    encoders = run(["ffmpeg", "-hide_banner", "-encoders"]).stdout
    if not LOCAL:
        check("dvdvideo demuxer", " dvdvideo " in demuxers)
        check("libopus encoder", " libopus " in encoders)

    work = Path(tempfile.mkdtemp(prefix="ffsmoke-"))
    try:
        fx = [fixture_ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        sine = ["-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100", "-ac", "2"]
        image = ["-f", "lavfi", "-i", "testsrc2=size=320x320:duration=1", "-frames:v", "1"]
        jpg, png, wav = work / "cover.jpg", work / "cover.png", work / "track.wav"
        for args in (
            [*image, str(jpg)], [*image, str(png)], [*sine, "-c:a", "pcm_s16le", str(wav)],
            [*sine, "-i", str(jpg), "-map", "0", "-map", "1", "-c:a", "libmp3lame", "-c:v", "copy",
             "-disposition:v", "attached_pic", "-metadata", "title=Mp3 Song", str(work / "a.mp3")],
            [*sine, "-i", str(jpg), "-map", "0", "-map", "1", "-c:a", "aac", "-c:v", "copy",
             "-disposition:v", "attached_pic", "-metadata", "title=M4a Song", str(work / "a.m4a")],
            [*sine, "-c:a", "libvorbis", "-metadata", "title=Ogg Song", str(work / "a.ogg")],
            # DVD-like program stream: MPEG-2 video, AC-3 audio. dvdvideo hands ffmpeg the same streams.
            ["-f", "lavfi", "-i", "testsrc2=size=720x480:rate=30000/1001:duration=3", *sine, "-ar", "48000",
             "-c:v", "mpeg2video", "-c:a", "ac3", "-f", "vob", str(work / "title.vob")],
        ):  # fmt: skip
            result = run([*fx, *args], fixture_env)
            if result.returncode:
                print(f"fixture failed: {' '.join(args[-1:])}: {result.stderr.strip()[-300:]}")
                return 2

        # 1. CD track -> FLAC with tags and the cover (the Windows path: WAV file, then encode).
        flac = work / "01 - Song.flac"
        tags = {"title": "Song = 1; #x", "artist": "Artist", "album": "Album", "track": "1", "TRACKTOTAL": "9"}
        [(pipeline, _)] = audio_track_commands("D:", [(1, 0, 3000)], {1: (str(flac), metadata_args(tags))}, str(jpg))
        _, encode = through_file(pipeline, str(wav))
        r = run(encode[0])
        check("WAV -> FLAC with cover", r.returncode == 0 and flac.is_file(), r.stderr.strip())
        p = localmedia.probe(flac)
        ok = p.tags.get("title") == tags["title"] and 2900 < p.duration_ms < 3100
        check("ffprobe FLAC tags + length", ok, str(p))
        cover = localmedia._embedded_cover(flac, _dir(work / "x1"))
        check("cover out of FLAC", cover is not None)

        # 2. FLAC -> Opus with tags + METADATA_BLOCK_PICTURE from an ffmetadata file.
        opus = work / "01 - Song.opus"
        meta = work / "1.ffmeta"
        block = picture_block(jpg.read_bytes(), "image/jpeg", 320, 320)
        meta.write_text(ffmetadata({**tags, "METADATA_BLOCK_PICTURE": block}), encoding="utf-8")
        if LOCAL:
            print("skip FLAC -> Opus (no libopus in a local test build)")
        else:
            r = run(opus_command(str(flac), str(meta), str(opus))[0])
            check("FLAC -> Opus", r.returncode == 0 and opus.is_file(), r.stderr.strip())
            p = localmedia.probe(opus)
            check("ffprobe Opus tags", p.tags.get("album") == "Album" and p.duration_ms > 2900, str(p))
            check("cover out of Opus", localmedia._embedded_cover(opus, _dir(work / "x2")) is not None)

        # 3. Other files the app can open.
        for name, title in (("a.mp3", "Mp3 Song"), ("a.m4a", "M4a Song"), ("a.ogg", "Ogg Song")):
            p = localmedia.probe(work / name)
            check(f"ffprobe {name}", p.tags.get("title") == title and p.duration_ms > 2900, str(p))
        check("ffprobe WAV length", localmedia.probe(wav).duration_ms > 2900)
        check("cover out of MP3", localmedia._embedded_cover(work / "a.mp3", _dir(work / "x3")) is not None)
        check("cover out of M4A", localmedia._embedded_cover(work / "a.m4a", _dir(work / "x4")) is not None)

        # 4. DVD title copy, same options as dvd_title_command, minus the disc input.
        mkv = work / "title.mkv"
        # +genpts: a raw program stream only stamps some frames (a full build fails the same way without it);
        # on a disc the dvdvideo demuxer supplies the timestamps itself.
        vob = str(work / "title.vob")
        r = run([*_FFMPEG, "-fflags", "+genpts", "-i", vob, "-map", "0", "-c", "copy", str(mkv)])
        ok = r.returncode == 0 and mkv.is_file() and mkv.stat().st_size > 10_000
        check("MPEG-PS (MPEG-2 + AC-3) -> MKV copy", ok, r.stderr.strip())
        streams = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(mkv)])
        codecs = sorted(streams.stdout.replace(",", " ").split())
        check("MKV keeps both streams", codecs == ["ac3", "mpeg2video"], streams.stdout)
        if len(sys.argv) > 3:
            keep = Path(sys.argv[3])
            keep.mkdir(parents=True, exist_ok=True)
            for made in (flac, opus, mkv):
                if made.is_file():
                    shutil.copy2(made, keep / made.name)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\n{len(failures)} failed" if failures else "\nall FFmpeg smoke checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
