@echo off
REM Double-click me inside the Windows VM to build the x64 exe.
echo Building Aubrey's YT-MP3 Downloader (x64)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1"
echo.
echo ============================================================
echo If you see errors above, copy this whole window and send it.
echo ============================================================
pause
