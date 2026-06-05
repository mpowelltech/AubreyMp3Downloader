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

# miniaudio is the preview player's audio backend: a single cffi extension
# (_miniaudio) with a statically-linked C lib. collect_all pulls in the .pyd so
# the frozen exe can import it. It's imported lazily + guarded in player.py, so
# even if this somehow misses, the app still launches (preview just degrades).
try:
    ma_datas, ma_binaries, ma_hidden = collect_all("miniaudio")
except Exception:
    ma_datas, ma_binaries, ma_hidden = [], [], []

binaries = list(ctk_binaries) + list(ma_binaries)
_ffmpeg = os.path.join(HERE, "build", "ffmpeg.exe")
if os.path.exists(_ffmpeg):
    binaries.append((_ffmpeg, "."))   # extracted next to the exe at runtime

_icon = os.path.join(HERE, "assets", "icon.ico")
icon = _icon if os.path.exists(_icon) else None
assert icon, "assets/icon.ico not found — the exe would have no icon"

datas = list(ctk_datas) + list(ma_datas)
_assets = os.path.join(HERE, "assets")
if os.path.isdir(_assets):
    datas.append((_assets, "assets"))   # bundle icon.ico / icon.png for the runtime window icon

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=list(ctk_hidden) + list(ma_hidden),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# Splash shown DURING the one-file unpack (before our code runs), so first
# launch isn't a silent gap while Windows extracts ~88 MB / Defender scans it.
# The app closes it (pyi_splash.close) the moment its window is ready.
_splash_img = os.path.join(HERE, "assets", "splash.png")
splash = Splash(
    _splash_img,
    binaries=a.binaries,
    datas=a.datas,
    always_on_top=True,
) if os.path.exists(_splash_img) else None

_exe_args = [pyz, a.scripts]
if splash is not None:
    _exe_args += [splash, splash.binaries]
_exe_args += [a.binaries, a.datas, []]

exe = EXE(
    *_exe_args,
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
