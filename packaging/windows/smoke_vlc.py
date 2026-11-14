"""Plays files through the app's own Player with the bundled (trimmed) libvlc, so a removed plugin we need fails CI.

Usage: python packaging/windows/smoke_vlc.py <vlc dir with libvlc.dll + plugins/> <dir of files to play>
Run from the repo root. The files are the rips smoke_ffmpeg.py made with the minimal FFmpeg.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Plugins the app relies on even though no file here exercises them (discs aren't available in CI).
REQUIRED_PLUGINS = [
    "access/libdvdread_plugin.dll", "access/libdvdnav_plugin.dll", "access/libcdda_plugin.dll",
    "access/libfilesystem_plugin.dll", "demux/libps_plugin.dll", "demux/libmkv_plugin.dll",
    "codec/libavcodec_plugin.dll", "codec/libspudec_plugin.dll", "codec/liblpcm_plugin.dll",
    "video_output/libvmem_plugin.dll",
]  # fmt: skip
PLAY_S = 2.5


def main() -> int:
    vlc_dir, media_dir = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if sys.platform == "win32":
        os.environ["PYTHON_VLC_LIB_PATH"] = str(vlc_dir / "libvlc.dll")
        os.environ["PYTHON_VLC_MODULE_PATH"] = str(vlc_dir / "plugins")
        os.add_dll_directory(str(vlc_dir))
    failures = []
    for rel in REQUIRED_PLUGINS:
        if sys.platform == "win32" and not (vlc_dir / "plugins" / rel).is_file():
            failures.append(rel)
            print(f"FAIL plugin missing: {rel}")

    from dvdplayer.player import Player  # imports python-vlc, which loads libvlc from the paths above

    for media in sorted(media_dir.iterdir()):
        frames = 0

        def on_frame(_data, _w, _h, _stride):
            nonlocal frames
            frames += 1

        player = Player(on_frame)
        player.open(media.as_uri())  # as the app does; libvlc rejects 'D:/x' paths with forward slashes
        time.sleep(PLAY_S)
        played_ms, length_ms = player.time_ms(), player.length_ms()
        player.release()
        video = media.suffix == ".mkv"
        ok = played_ms > 500 and length_ms > 2000 and (frames > 10 or not video)
        print(f"{'ok  ' if ok else 'FAIL'} play {media.name}: {played_ms} ms of {length_ms} ms, {frames} frames")
        if not ok:
            failures.append(media.name)

    print(f"\n{len(failures)} failed" if failures else "\nall VLC smoke checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
