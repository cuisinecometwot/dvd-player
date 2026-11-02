# DVD Player

Small native GNOME app to play DVDs from the external USB drive (`/dev/sr0`) and manage the drive.
Current goal: v1 personal tool — works well on this machine (Fedora, GNOME/Wayland).

## Product
- **User**: the owner, on their own Fedora laptop with a USB DVD writer (Matshita UJ8B0AW, region set to 2).
- **Problem**: VLC's disc menus are clumsy (Japanese-only menus) and there's no single place to see drive/disc state.
- **Solution**: one window — drive panel + title/chapter list on the left, embedded video on the right; disc menus skipped.
- **v1 features**: embedded playback (play/pause/seek/volume/fullscreen), title & chapter picker with real lengths,
  drive status/eject/close tray with auto-detect on insert, audio/subtitle track switching.
- **Engagement**: utility, used whenever a disc is played. Personal, non-commercial, not distributed. English UI.
- **UI**: libadwaita, forced dark (video app). Keys: Space, F/F11, Esc, ←/→ seek 10 s, ↑/↓ volume.

## Stack & architecture
- Python 3 + GTK4/libadwaita (system PyGObject), libvlc via `python-vlc` in `.venv` (created with `--system-site-packages`).
- Chosen over the Tauri desktop default: tiny local tool, GTK/libadwaita already installed, no build step.
- `domain/` — pure stdlib: IFO parsing (title lengths, aspect, region), drive status codes, durations, letterbox math.
- `dvdplayer/` — framework code: `drive.py` (ioctl/sysfs/lsblk/eject), `player.py` (libvlc), `video_view.py`, `window.py`.
- Video: libvlc renders RV32 frames to memory (`video_set_callbacks`), GTK draws them as textures — VLC can't embed
  into a GTK4 widget on Wayland.
- libvlc reports 0 length for DVD titles via `dvdsimple://`, so lengths/aspect come from the IFO files instead.
- Needs `libdvdcss` (RPM Fusion tainted) for encrypted discs.

## Dev
- Setup: `python3 -m venv --system-site-packages .venv && .venv/bin/pip install python-vlc pytest ruff`
- Run: `./run.sh` · Test: `.venv/bin/python -m pytest` · Lint: `.venv/bin/ruff check . && .venv/bin/ruff format .`

## Progress
- Done: v1 features above; domain unit tests.
- Next (ideas): rip audio CDs / unprotected discs to file; audio CD playback (`cdda://`); remember last position.
