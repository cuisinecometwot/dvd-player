# DVD Player

Small native GNOME app to play DVDs from the external USB drive (auto-detected `/dev/sr*`; it can come back as `sr1` after a USB replug) and manage the drive.
Current goal: v1 personal tool — works well on this machine (Fedora, GNOME/Wayland); Debian 13+/Ubuntu 24.04+
supported on a best-effort basis (untested on real installs). Windows: experimental build
(pre-release on GitHub, untested on real Windows). The repo is public on GitHub,
so others may try it; `README.md` is the user-facing guide.

## Product
- **User**: the owner, on their own Fedora laptop with a USB DVD writer (Matshita UJ8B0AW, region set to 2).
- **Problem**: VLC's disc menus are clumsy (Japanese-only menus) and there's no single place to see drive/disc state.
- **Solution**: one window — drive panel + title/chapter list on the left, embedded video on the right; disc menus skipped.
- **v1 features**: embedded playback (play/pause/seek/volume/fullscreen), title & chapter picker with real lengths,
  drive status/eject/close tray with auto-detect on insert, audio/subtitle track switching.
- **Audio CDs**: track list in the sidebar, auto-play from track 1, auto-advance, prev/next track. Album/track names:
  user edits > disc CD-Text > MusicBrainz (disc ID, then barcode/MCN), editable in the Rip dialog. Synced lyrics
  (LRCLIB) shown big under the cover while playing (toggle: L); the CD page has only cover + lyrics, track details
  are in the window title. Everything cached per disc ID in
  `~/.cache/dvd-player/cd/<discid>.json`; misses retried after 7 days. Rips go to `<Album>/NN - Title.flac`
  with tags (artist only in tags, by choice); optional lyrics: plain in the FLAC `LYRICS` tag + synced `.lrc`
  beside it; instrumentals skipped. Optional Opus copies (160 kb/s, for phones) in a sibling `<Album> (Opus)`
  folder, encoded from the fresh FLACs (disc read once), with tags + lyrics + cover (METADATA_BLOCK_PICTURE via
  an ffmetadata file: ffmpeg can't mux a picture stream into Ogg, and the base64 is too long for argv).
- **Cover art**: CDs hold none; front cover from the Cover Art Archive via the MusicBrainz release (so CD-Text/typed
  albums get none unless the user picks a JPEG/PNG in the Rip dialog — user's pick is never replaced). Shown on the
  CD page + sidebar thumbnail; on rip, embedded in each FLAC and saved as `cover.jpg/png`.
- **Online transparency**: sidebar "Album info" row — spinner + what is being fetched from where, then where each
  piece came from (or "Saved on this computer"); tooltip lists exactly what is sent. Unreachable services show
  Retry and are never cached as "not found" (`net.NetworkError` vs. a 404/None).
- **Files on disk** (Open menu, Ctrl+O / Ctrl+Shift+O, or drag & drop): an album folder or any track in it plays on
  the CD page (names from tags, else `NN - Title` file names; synced lyrics from the `.lrc` beside each track, plain
  from the LYRICS tag; cover from `cover.jpg/png`, else the picture embedded in the first track, e.g. the Opus
  copies). A video file (MKV rips) plays like a DVD title with its chapters. Nothing is fetched online for files.
  While a file plays, the drive is still watched but a newly inserted disc only shows a "Play Disc" toast;
  Reload goes back to the disc. Rip stays disc-only.
- **DVD subtitles**: only the disc's own tracks (dropdown); nothing is fetched for DVDs.
- **Ripping**: DVD title → MKV (lossless stream copy), audio CD → one FLAC per track, any unprotected disc → ISO.
  CSS-protected discs are refused with an explanation: bypassing CSS is illegal in Japan even for private copies.
- **Engagement**: utility, used whenever a disc is played. Non-commercial; built for the owner first, but the repo is
  public (github.com/cuisinecometwot/dvd-player, MIT license), so features must also make sense to strangers
  and be explained in `README.md`. English UI.
- **UI**: libadwaita on Linux, Qt (native look, menu bar + toolbar) on Windows; both forced dark (video app).
  Main menu: Clear Saved Data (CD cache, crash leftovers, old logs; never rips), Open Log Folder. Keys: Space, F/F11, Esc, ←/→ seek 10 s, ↑/↓ volume, L lyrics (CD).

## Stack & architecture
- Python 3 + GTK4/libadwaita (system PyGObject) on Linux, PySide6 (Qt) on Windows; libvlc via `python-vlc` in
  `.venv` (created with `--system-site-packages`).
- Chosen over the Tauri desktop default: tiny local tool, GTK/libadwaita already installed, no build step. Qt for
  Windows because GTK4/libadwaita can't look native there and the official Qt wheels are smaller than MSYS2 GTK.
- `dvdplayer/session.py` holds all app behaviour (drive polling, disc/CD/file lifecycle, rips, clear data) and
  calls a `View` protocol; `gtkui/` and `qtui/` only build widgets and implement View. Worker threads come back via
  `mainthread.call` (GLib.idle_add / queued Qt signal). Shared modules must not import gi or PySide6.
- `domain/` — pure stdlib: CD track navigation (`cd.py`), album folders on disk (`library.py`), CD metadata/disc ID/names/tags (`cdmeta.py`), lyrics matching + LRC timing (`lyrics.py`), cache + cover rules (`cdcache.py`), CD-Text parsing (`cdtext.py`), IFO parsing (title lengths, aspect, region), drive status codes, durations, letterbox math,
  rip rules + ffmpeg command building (`rip.py`).
- `dvdplayer/` — framework code: `drive.py` (ioctl/sysfs/lsblk/eject/TOC/MCN/CSS check), `musicbrainz.py` (urllib, 1 req/s, retries 503), `net.py` (shared GET, NetworkError vs not-found), `lrclib.py`, `coverart.py`, `audiorip.py` (lyrics/tags/FLAC/Opus rip plan), `localmedia.py` (album folder via ffprobe), `cdinfo.py` (cache → CD-Text via `cd-info` → network, background threads), `player.py` (libvlc),
  `ripper.py` (background ffmpeg / image-copy jobs), `session.py`, `applog.py` (log file in the temp dir, purged
  after `DVD_PLAYER_LOG_DAYS`, default 1; VLC warnings via libvlc's log callback + vsnprintf), `appdata.py`
  (cache root, Clear Saved Data), `userdirs.py`, `gtkui/` (window, video_view, images), `qtui/` (window,
  ripdialog, widgets).
- Ripping uses system `ffmpeg` (`dvdvideo` demuxer) and `cd-paranoia` (libcdio-paranoia) for audio CDs, piped into
  ffmpeg for FLAC, one pipeline per track (the single-pass segment muxer writes wrong FLAC lengths). Not ffmpeg's
  `libcdio` demuxer: it spins forever near the lead-out (every last track) and ignores SIGTERM, so cancel SIGKILLs.
- VLC must be released before the interpreter exits (its video thread calls into Python), hence the app `shutdown` hook.
- Video: libvlc renders RV32 frames to memory (`video_set_callbacks`), GTK draws them as textures — VLC can't embed
  into a GTK4 widget on Wayland.
- CD tracks play as `cdda://` + `:cdda-track=N`, one media per track (a bare libvlc MediaPlayer won't walk
  the CD playlist), so the app advances tracks itself when VLC reports Ended.
- ffprobe: Ogg/Opus tags live on the audio stream, FLAC's on the container, so both are read.
- Volume is applied only once the playback clock runs: libvlc's PipeWire output segfaults on `audio_set_volume`
  before that (even while the state already says Playing).
- libvlc reports 0 length for DVD titles via `dvdsimple://`, so lengths/aspect come from the IFO files instead.
- Windows port: `drive.py` picks `drive_linux.py` (ioctl/sysfs) or `drive_windows.py` (ctypes `DeviceIoControl`
  on `\\.\D:`; struct parsing in `domain/wincd.py`); `DriveBase` holds the shared IFO code and `vlc_path`/`raw_path`.
  CD rips go through a temp WAV on Windows (cd-paranoia's stdout is text mode there). Built only in CI
  (`.github/workflows/windows-release.yml`; tag `v*-windows*` publishes a pre-release, pushes to main
  only build): python.org Python + PySide6 wheels + PyInstaller onedir; MSYS2 only builds a minimal FFmpeg
  (`packaging/windows/build-ffmpeg.sh`, ~10 MB, only the app's demuxers/encoders) and supplies cd-paranoia/cd-info;
  the VLC zip is trimmed (`trim-vlc.sh`, ~61 MB). CI runs `smoke_ffmpeg.py` (the app's real commands on the bundled
  FFmpeg, PATH isolated) and `smoke_vlc.py` (Player plays the results with the trimmed libvlc), then starts the
  packaged exe and checks its log. `dvdplayer/winsetup.py` wires bin/ and vlc/ beside the exe. On Windows VLC draws
  into the video widget's HWND (`Player.set_window`); `DVD_PLAYER_QT_FRAMES=1` falls back to RV32 frames.
  Cache on Windows is `%LOCALAPPDATA%\dvd-player` (GLib's cache dir there is INetCache); the uninstaller offers
  to delete it. Without an optical drive the device is empty, never a guessed `D:` (a hard-disk partition).
- Needs `libdvdcss` (RPM Fusion tainted; `libdvd-pkg` on Debian/Ubuntu) for encrypted discs.
- Distro compat floor: Python 3.12, libadwaita 1.5 (Ubuntu 24.04) — `Adw.Spinner` (1.6) falls back to
  `Gtk.Spinner`. Debian/Ubuntu ship the original `cdparanoia` (same options), used when `cd-paranoia` is missing.

## Dev
- Keep `README.md` in sync when user-visible features, keys, requirements or online lookups change.
- Setup: `python3 -m venv --system-site-packages .venv && .venv/bin/pip install python-vlc pytest ruff`
- Run: `./run.sh` · Test: `.venv/bin/python -m pytest` · Lint: `.venv/bin/ruff check . && .venv/bin/ruff format .`

## Progress
- Done: v1 features above; domain unit tests; Rip button (DVD title/audio CD/ISO); audio CD playback; CD metadata + tagged FLAC names; LRCLIB lyrics on rip and during playback; per-disc cache; cover art; playing ripped albums/videos from disk; README; log file + Clear Saved Data; Session/View split; Qt UI for Windows.
- Windows GTK build (v0.2.0-windows-experimental.2) ran fine on the owner's PC. The Qt build is untested on real
  Windows (only CI's startup test) and the refactored disc path is untested with a real disc (tray was open).
- Audio CD rip verified on a real disc. Untested on real media: unprotected-DVD rips (only protected DVDs on hand; jobs verified on stand-in input).
- Untested on real media: CD-Text parsing (no CD-Text disc on hand; parser follows libcdio's `cd-info` format).
- Next (ideas): remember last position; verify on a Debian 13 / Ubuntu 24.04 VM; test the
  Windows build on real hardware (merged to main untested there).
