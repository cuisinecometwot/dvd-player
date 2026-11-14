# PyInstaller spec for the Windows build (Qt UI); run from the repo root with python.org Python + PySide6.
# ffmpeg, cd-paranoia and VLC are copied next to the exe afterwards (see the workflow), not analysed here,
# so VLC's own DLLs never get mixed with Qt's.
from pathlib import Path

root = Path(SPECPATH).parent.parent

# Qt pieces PySide6's hooks would bundle but nothing here uses: software OpenGL (~20 MB), QML/Quick, PDF and
# SVG image plugins, and Qt's own translations (the UI is English).
DROP = (
    "opengl32sw.dll", "Qt6Pdf", "Qt6Qml", "Qt6Quick", "Qt6Svg", "Qt6Network", "Qt6OpenGL", "Qt6VirtualKeyboard",
    "qpdf", "qsvg", "translations",
)

a = Analysis(
    [str(root / "packaging" / "windows" / "launch.py")],
    pathex=[str(root)],
    hiddenimports=["vlc", "dvdplayer.qtui.app", "dvdplayer.qtui.window"],
    excludes=["gi", "dvdplayer.gtkui", "tkinter", "pytest", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick"],
)
a.binaries = [b for b in a.binaries if not any(d in b[0] for d in DROP)]
a.datas = [d for d in a.datas if not any(x in d[0] for x in DROP)]
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
