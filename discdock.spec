# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH)
hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("fastapi")
    + collect_submodules("apprise")
    + ["win32timezone", "pythoncom", "pywintypes"]
)
dashboard = root / "out"
if not (dashboard / "index.html").is_file():
    raise SystemExit("Build the static dashboard first; out/index.html is missing")
# The logo for the notification area; scripts/make-icon.py draws it from public/favicon.svg.
icon = root / "assets" / "discdock.ico"
data = [(str(dashboard), "web"), (str(icon), ".")] + collect_data_files("apprise")

a = Analysis(
    [str(root / "service" / "discdock_launcher.py")],
    pathex=[str(root / "service")],
    binaries=[],
    datas=data,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DiscDock",
    icon=str(icon),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DiscDock",
)
