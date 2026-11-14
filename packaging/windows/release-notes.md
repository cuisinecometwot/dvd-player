**Experimental Windows build: not yet tested on a real Windows machine.** Please report what works and what doesn't.

Downloads:
- `DvdPlayer-…-windows-setup.exe`: installer (Start menu shortcut, uninstaller). It is not code-signed, so Windows
  SmartScreen warns about it. Choose *More info → Run anyway*.
- `DvdPlayer-…-windows-portable.zip`: unzip anywhere and run `DvdPlayer\DvdPlayer.exe`.

Bundled: GTK 4 + libadwaita, VLC (libvlc, including libdvdcss for encrypted DVDs), FFmpeg, and libcdio's
cd-paranoia/cd-info.

Known limits on Windows:
- The drive is found by drive letter (e.g. `D:`). An open tray shows as "No disc", because Windows doesn't report the tray state.
- Audio CD rips read each track to a temporary WAV file before encoding, so progress moves in steps.
- Settings and the CD cache live in `%LOCALAPPDATA%\dvd-player`.
