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

# miniaudio is the preview player's audio backend. IMPORTANT: `miniaudio` is a
# single .py module, and the compiled part is a SEPARATE top-level cffi extension
# `_miniaudio` (e.g. _miniaudio.pyd) — NOT inside a `miniaudio` package. So
# collect_all("miniaudio") does NOT pull in _miniaudio, and the frozen exe then
# fails to import miniaudio ("preview isn't available"). We must add _miniaudio
# as a hidden import (PyInstaller then bundles the .pyd) and collect its lib.
try:
    ma_datas, ma_binaries, ma_hidden = collect_all("miniaudio")
except Exception:
    ma_datas, ma_binaries, ma_hidden = [], [], []
# miniaudio is a cffi module: it needs BOTH the compiled _miniaudio extension AND
# the cffi runtime backend `_cffi_backend` (miniaudio.py line `import cffi`). Neither
# is under the `miniaudio` module, so collect_all("miniaudio") misses them and the
# frozen exe fails with ModuleNotFoundError: _cffi_backend / _miniaudio -> the
# player silently reports "preview not available". Pull in the whole cffi package
# + both extensions explicitly. (Verified in a frozen build on Windows.)
ma_hidden = list(ma_hidden) + ["miniaudio", "_miniaudio", "cffi", "_cffi_backend"]
try:
    cffi_datas, cffi_binaries, cffi_hidden = collect_all("cffi")
    ma_datas = list(ma_datas) + list(cffi_datas)
    ma_binaries = list(ma_binaries) + list(cffi_binaries)
    ma_hidden = ma_hidden + list(cffi_hidden)
except Exception:
    pass
try:
    from PyInstaller.utils.hooks import collect_dynamic_libs
    ma_binaries = list(ma_binaries) + collect_dynamic_libs("_miniaudio")
except Exception:
    pass

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
