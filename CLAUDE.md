# DVD Player

Small native GNOME app to play DVDs from the external USB drive (auto-detected `/dev/sr*`; it can come back as `sr1` after a USB replug) and manage the drive.
Current goal: v1 personal tool — works well on this machine (Fedora, GNOME/Wayland).

## Product
- **User**: the owner, on their own Fedora laptop with a USB DVD writer (Matshita UJ8B0AW, region set to 2).
- **Problem**: VLC's disc menus are clumsy (Japanese-only menus) and there's no single place to see drive/disc state.
- **Solution**: one window — drive panel + title/chapter list on the left, embedded video on the right; disc menus skipped.
- **v1 features**: embedded playback (play/pause/seek/volume/fullscreen), title & chapter picker with real lengths,
  drive status/eject/close tray with auto-detect on insert, audio/subtitle track switching.
- **Audio CDs**: track list in the sidebar, auto-play from track 1, auto-advance, prev/next track.
- **Ripping**: DVD title → MKV (lossless stream copy), audio CD → one FLAC per track, any unprotected disc → ISO.
  CSS-protected discs are refused with an explanation: bypassing CSS is illegal in Japan even for private copies.
- **Engagement**: utility, used whenever a disc is played. Personal, non-commercial, not distributed. English UI.
- **UI**: libadwaita, forced dark (video app). Keys: Space, F/F11, Esc, ←/→ seek 10 s, ↑/↓ volume.

## Stack & architecture
- Python 3 + GTK4/libadwaita (system PyGObject), libvlc via `python-vlc` in `.venv` (created with `--system-site-packages`).
- Chosen over the Tauri desktop default: tiny local tool, GTK/libadwaita already installed, no build step.
- `domain/` — pure stdlib: CD track navigation (`cd.py`), IFO parsing (title lengths, aspect, region), drive status codes, durations, letterbox math,
  rip rules + ffmpeg command building (`rip.py`).
- `dvdplayer/` — framework code: `drive.py` (ioctl/sysfs/lsblk/eject/TOC/CSS check), `player.py` (libvlc),
  `ripper.py` (background ffmpeg / image-copy jobs), `video_view.py`, `window.py`.
- Ripping uses system `ffmpeg` (`dvdvideo` demuxer) and `cd-paranoia` (libcdio-paranoia) for audio CDs, piped into
  ffmpeg for FLAC, one pipeline per track (the single-pass segment muxer writes wrong FLAC lengths). Not ffmpeg's
  `libcdio` demuxer: it spins forever near the lead-out (every last track) and ignores SIGTERM, so cancel SIGKILLs.
- VLC must be released before the interpreter exits (its video thread calls into Python), hence the app `shutdown` hook.
- Video: libvlc renders RV32 frames to memory (`video_set_callbacks`), GTK draws them as textures — VLC can't embed
  into a GTK4 widget on Wayland.
- CD tracks play as `cdda://` + `:cdda-track=N`, one media per track (a bare libvlc MediaPlayer won't walk
  the CD playlist), so the app advances tracks itself when VLC reports Ended.
- libvlc reports 0 length for DVD titles via `dvdsimple://`, so lengths/aspect come from the IFO files instead.
- Needs `libdvdcss` (RPM Fusion tainted) for encrypted discs.

## Dev
- Setup: `python3 -m venv --system-site-packages .venv && .venv/bin/pip install python-vlc pytest ruff`
- Run: `./run.sh` · Test: `.venv/bin/python -m pytest` · Lint: `.venv/bin/ruff check . && .venv/bin/ruff format .`

## Progress
- Done: v1 features above; domain unit tests; Rip button (DVD title/audio CD/ISO); audio CD playback.
- Audio CD rip verified on a real disc. Untested on real media: unprotected-DVD rips (only protected DVDs on hand; jobs verified on stand-in input).
- Next (ideas): CD track names via MusicBrainz; remember last position.
