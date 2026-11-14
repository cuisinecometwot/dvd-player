**Experimental Windows build.** Please report what works and what doesn't (attach the log: *Tools → Open Log Folder*).

Downloads:
- `DvdPlayer-…-windows-setup.exe`: installer (Start menu shortcut, uninstaller). It is not code-signed, so Windows
  SmartScreen warns about it. Choose *More info → Run anyway*.
- `DvdPlayer-…-windows-portable.zip`: unzip anywhere and run `DvdPlayer\DvdPlayer.exe`.

Needs Windows 10 (version 1809 or later) or 11, 64-bit.

What's inside: a native Windows interface (Qt), VLC drawing video straight into the window (hardware decoding),
VLC's libdvdcss for encrypted DVDs, a minimal FFmpeg built only for the app's rips, and libcdio's cd-paranoia/cd-info.

Known limits on Windows:
- The drive is found by drive letter (e.g. `D:`). An open tray shows as "No disc", because Windows doesn't report
  the tray state.
- Audio CD rips read each track to a temporary WAV file before encoding, so progress moves in steps.
- Album info, covers and lyrics are cached in `%LOCALAPPDATA%\dvd-player` (*Tools → Clear Saved Data…* empties it).
  Logs go to `%TEMP%\dvd-player-logs` and are kept for a day.
- If video misbehaves (black picture, flicker), set the environment variable `DVD_PLAYER_QT_FRAMES=1` and restart:
  VLC then hands frames to the app instead of drawing into the window itself.

Uninstalling: *Settings → Apps → DVD Player → Uninstall* removes the program and offers to delete the cache.
Your rips are never touched. For the portable zip, delete the `DvdPlayer` folder and `%LOCALAPPDATA%\dvd-player`.
