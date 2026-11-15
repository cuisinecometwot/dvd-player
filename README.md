# DVD Player

A small GNOME app for playing DVDs and audio CDs from a USB or built-in drive: no fighting with disc menus,
synced lyrics over the album cover, and one-click rips you can play again later without the disc.

- **DVDs open straight into the film.** Disc menus are skipped; every title and chapter is listed in the sidebar
  with its real length, and the main feature starts on its own.
- **Audio CDs get names, lyrics and a cover.** Track and album names come from the disc's CD-Text or MusicBrainz,
  the front cover from the Cover Art Archive, and synced lyrics from LRCLIB, shown large under the cover as the song
  plays.
- **Rip the disc.** An audio CD becomes a tagged FLAC album (with cover, lyrics and optional Opus copies for your
  phone), a DVD title becomes an MKV with all audio and subtitle tracks, and any unprotected disc can be saved
  as an ISO.
- **Play your rips.** Open an album folder or an MKV and it plays just like the disc did, lyrics and chapters
  included, fully offline.
- **One window for the drive too.** See whether there is a disc and what it is, eject or close the tray; discs are
  picked up automatically when inserted.

**Windows (experimental):** download the installer or portable zip from
[Releases](https://github.com/cuisinecometwot/dvd-player/releases). On Linux, run it from source (see below).

## Requirements

Built and tested on **Fedora (GNOME, Wayland)** with Python 3.14. Any Linux distribution with Python 3.12+,
GTK 4 and libadwaita 1.5+ should work.

**Fedora**

```sh
sudo dnf install python3-gobject gtk4 libadwaita vlc ffmpeg-free libcdio libcdio-paranoia
```

Encrypted (CSS) DVDs, which is most commercial films, need `libdvdcss` from [RPM Fusion](https://rpmfusion.org/).
With RPM Fusion Free enabled, add its "tainted" repository:
`sudo dnf install rpmfusion-free-release-tainted && sudo dnf install libdvdcss`

**Debian 13+ / Ubuntu 24.04+** (Debian 12 is too old: its libadwaita is 1.2)

```sh
sudo apt install python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 vlc ffmpeg libcdio-utils cdparanoia
```

For encrypted DVDs, install `libdvd-pkg` (Debian: `contrib` repository; Ubuntu: `multiverse`), which downloads
and builds `libdvdcss`: `sudo apt install libdvd-pkg && sudo dpkg-reconfigure libdvd-pkg`

Ripping a DVD title to MKV needs an FFmpeg 7.0+ build with its `dvdvideo` input; check with
`ffmpeg -hide_banner -demuxers | grep dvdvideo`. Ubuntu 24.04 ships FFmpeg 6.1, so there only audio CD and ISO
rips work.

**All distributions:** a DVD drive. It is found automatically (`/dev/sr0`, `/dev/sr1`, …).

**Windows 10/11, 64-bit (experimental)**

Download the installer or the portable zip from the
[pre-releases](https://github.com/cuisinecometwot/dvd-player/releases). On Windows the app has a native
interface (Qt) and everything is bundled (VLC with libdvdcss, a minimal FFmpeg, cd-paranoia), so nothing else
needs installing. The exe is not signed, so SmartScreen will warn: choose *More info → Run anyway*. The drive is
found by its letter (pass `D:` to pick one); an open tray shows as "No disc". Cached album info lives in
`%LOCALAPPDATA%\dvd-player`; uninstalling (*Settings → Apps*) offers to delete it too. Your rips are never
touched. For the portable zip, delete the folder and that cache folder. The build is made by
`.github/workflows/windows-release.yml` on GitHub's Windows runners, which also check that the bundled FFmpeg runs
the app's rip commands, that the bundled VLC plays the results, and that the packaged exe starts.

## Install and run

```sh
git clone https://github.com/cuisinecometwot/dvd-player.git
cd dvd-player
python3 -m venv --system-site-packages .venv
.venv/bin/pip install python-vlc
./run.sh
```

`--system-site-packages` lets the virtual environment use the system's GTK bindings. To use a specific drive,
pass it as an argument: `./run.sh /dev/sr1`.

## Using it

Insert a disc and playback starts. Pick another title, chapter or track in the sidebar; switch the audio
language and subtitles with the dropdowns under the video.

| Key | Action |
|---|---|
| Space | Play / pause |
| ← / → | Seek 10 seconds |
| ↑ / ↓ | Volume |
| F, F11, double-click | Fullscreen (Esc to leave) |
| L | Show / hide lyrics (audio CDs and albums) |
| Ctrl+O | Open a track or video file |
| Ctrl+Shift+O | Open an album folder |

**Ripping:** press **Rip**, choose what to save and where. For audio CDs you can correct the album and track
names, choose a cover image, and turn lyrics and Opus copies on or off before starting. Your edits are
remembered for that disc. Playback stops while ripping, since the drive can't do both at full speed.

A ripped album looks like this:

```
Album/
  01 - Song.flac      tags, cover and plain lyrics inside
  01 - Song.lrc       synced lyrics, for players that show them
  cover.jpg
Album (Opus)/         optional, ≈1.2 MB per minute
  01 - Song.opus
```

**Playing rips:** use the folder button at the top left, the buttons on the start page, or drag a folder or file
onto the window. Opening a single track plays its whole album from that track. If you insert a disc meanwhile,
the file keeps playing and a **Play Disc** button appears; the reload button also switches back to the disc.

## Privacy

Looking up an audio CD sends only the disc ID, its barcode, the song titles, artist and track lengths to
[MusicBrainz](https://musicbrainz.org/), [LRCLIB](https://lrclib.net/) and the
[Cover Art Archive](https://coverartarchive.org/). Results are cached in `~/.cache/dvd-player/`, so each disc is
looked up once. The sidebar shows what is being fetched and from where. Nothing is sent for DVDs or for files
you open from disk.

**Clear Saved Data…** in the main menu (☰) deletes that cache (album names including your edits, covers,
lyrics), temporary files a crash may have left behind, and old log files. Your rips are never touched.

## Logs

Each run writes a log to the temp folder (`/tmp/dvd-player-logs-<uid>/` on Linux, `%TEMP%\dvd-player-logs\`
on Windows; **Open Log Folder** in the main menu opens it). It records drive and disc events, the rip commands
and their errors, online lookups (host and path only), and VLC's warnings. Logs older than a day are deleted at
startup; set `DVD_PLAYER_LOG_DAYS` (e.g. `7`, or `0` to keep only the current run) to change that.

## Copy protection

Ripping a copy-protected (CSS) DVD would mean bypassing that protection, which the author's local law (Japan)
forbids even for private copies, so the app refuses and explains why. Playing those discs works with
`libdvdcss`. Audio CDs, unprotected DVDs and data discs rip normally.

## Status

A personal project, used daily on one machine (Fedora, a Panasonic/Matshita USB DVD writer). Known gaps:

- Ripping unprotected DVDs and reading CD-Text are implemented but haven't been tried on real discs yet.
- Windows build is experimental.

Bug reports and pull requests are welcome.

## Development

```sh
.venv/bin/pip install pytest ruff
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format .
```

The rules (track navigation, metadata, lyrics timing, rip commands, album folders) live in `domain/`, plain
Python with unit tests. `dvdplayer/session.py` runs the app (drive, player, files, rips) for either UI:
`dvdplayer/gtkui/` (GTK 4 / libadwaita, Linux) and `dvdplayer/qtui/` (Qt, Windows). To try the Qt UI on Linux:
`.venv/bin/pip install PySide6-Essentials` and `./run.sh --qt`.

## License

[MIT](LICENSE)
