# PyInstaller spec for the Windows build; run from the repo root inside MSYS2 UCRT64.
# ffmpeg, cd-paranoia and VLC are copied next to the exe afterwards (see the workflow), not analysed here,
# so VLC's own DLLs never get mixed with GTK's.
from pathlib import Path

root = Path(SPECPATH).parent.parent

a = Analysis(
    [str(root / "packaging" / "windows" / "launch.py")],
    pathex=[str(root)],
    hiddenimports=["vlc"],
    hooksconfig={
        "gi": {
            "module-versions": {"Gtk": "4.0", "Gdk": "4.0", "Gsk": "4.0", "Adw": "1"},
            "icons": ["Adwaita", "hicolor"],
            "themes": ["Adwaita"],
            "languages": ["en_US"],
        },
    },
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DvdPlayer",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="DvdPlayer", upx=False)
