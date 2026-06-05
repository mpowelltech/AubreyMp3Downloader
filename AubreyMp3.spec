# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds the single-file Windows exe.

Run from the repo root (on Windows / CI):  pyinstaller AubreyMp3.spec

Expects a static ``build/ffmpeg.exe`` to bundle (the CI workflow downloads it).
If it's missing the build still succeeds, but the resulting exe will rely on
ffmpeg being on PATH — fine for a quick local smoke build, not for release.
"""

import os
from PyInstaller.utils.hooks import collect_all

# Anchor every path to the spec's own directory so paths resolve no matter what
# the current working directory is when PyInstaller runs.
try:
    HERE = SPECPATH  # injected by PyInstaller (absolute path to this spec's dir)
except NameError:
    HERE = os.getcwd()

# customtkinter ships theme assets + needs a few hidden imports.
ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")

binaries = list(ctk_binaries)
_ffmpeg = os.path.join(HERE, "build", "ffmpeg.exe")
if os.path.exists(_ffmpeg):
    binaries.append((_ffmpeg, "."))   # extracted next to the exe at runtime

_icon = os.path.join(HERE, "assets", "icon.ico")
icon = _icon if os.path.exists(_icon) else None
assert icon, "assets/icon.ico not found — the exe would have no icon"

datas = list(ctk_datas)
_assets = os.path.join(HERE, "assets")
if os.path.isdir(_assets):
    datas.append((_assets, "assets"))   # bundle icon.ico / icon.png for the runtime window icon

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=list(ctk_hidden),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Aubreys-YT-MP3-Downloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX off: it noticeably increases AV false positives
    runtime_tmpdir=None,
    console=False,        # no console window for a GUI app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
