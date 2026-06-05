@echo off
REM Run this in the Windows VM if the exe shows a stale/old icon in Explorer.
REM The icon IS embedded in the exe; Windows just caches icons per filename and
REM can keep showing an old one after rebuilds. This clears that cache.
echo Refreshing the Windows icon cache (Explorer will blink)...
taskkill /f /im explorer.exe >nul 2>&1
del /a /q "%LOCALAPPDATA%\IconCache.db" >nul 2>&1
del /a /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache*" >nul 2>&1
start explorer.exe
echo.
echo Done. The app's icon should now show correctly in Explorer.
pause
