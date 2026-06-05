# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds the single-file Windows exe.

Run from the repo root (on Windows / CI):  pyinstaller AubreyMp3.spec

Expects a static ``build/ffmpeg.exe`` to bundle (the CI workflow downloads it).
If it's missing the build still succeeds, but the resulting exe will rely on
ffmpeg being on PATH — fine for a quick local smoke build, not for release.
"""

import os
from PyInstaller.utils.hooks import collect_all

# customtkinter ships theme assets + needs a few hidden imports.
ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")

binaries = list(ctk_binaries)
_ffmpeg = os.path.join("build", "ffmpeg.exe")
if os.path.exists(_ffmpeg):
    binaries.append((_ffmpeg, "."))   # extracted next to the exe at runtime

_icon = os.path.join("assets", "icon.ico")
icon = _icon if os.path.exists(_icon) else None

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=list(ctk_datas),
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
