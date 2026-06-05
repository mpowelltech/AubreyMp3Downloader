#requires -version 5
<#
  Builds the x64 Windows .exe for "Aubrey's YT-MP3 Downloader".

  Run this INSIDE the Windows VM. Easiest: double-click build_windows.bat
  (next to this file). Or from PowerShell:
      powershell -ExecutionPolicy Bypass -File .\build_windows.ps1

  What it does:
    1. Finds (or installs, for your user only) an x64 Python 3.12.
    2. Copies the project to a fast local working folder.
    3. Downloads a static x64 ffmpeg and bundles it.
    4. Builds a single-file x64 .exe with PyInstaller.
    5. Copies the .exe into the project's _winbuild_out\ folder
       (which shows up on your Mac), then launches it to test.
#>

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Section($t) { Write-Host "`n==== $t ====" -ForegroundColor Cyan }
function Check($msg)  { if ($LASTEXITCODE -ne 0) { throw "$msg (exit $LASTEXITCODE)" } }

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Section "Project root: $ProjectRoot"

# --- 1. Ensure an x64 Python ------------------------------------------------
function Find-X64Python {
    $cands = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"
    )
    $cands += @(Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object Source)
    foreach ($p in ($cands | Select-Object -Unique)) {
        if ($p -and (Test-Path $p)) {
            try { $m = (& $p -c "import platform;print(platform.machine())" 2>$null).Trim() } catch { $m = "" }
            if ($m -eq "AMD64") { return $p }
        }
    }
    return $null
}

Section "Locating an x64 Python"
$py = Find-X64Python
if (-not $py) {
    Write-Host "No x64 Python found - installing one for your user account..."
    $ok = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            winget install -e --id Python.Python.3.12 --architecture x64 --scope user `
                --accept-source-agreements --accept-package-agreements
            $ok = ($LASTEXITCODE -eq 0)
        } catch { Write-Host "winget failed: $_" }
    }
    if (-not $ok) {
        $url = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
        $dl  = "$env:TEMP\python-amd64.exe"
        Write-Host "Falling back to direct download: $url"
        Invoke-WebRequest -Uri $url -OutFile $dl
        Start-Process $dl -Wait -ArgumentList `
            "/quiet InstallAllUsers=0 PrependPath=1 Include_tcltk=1 Include_pip=1 Include_launcher=1"
    }
    $py = Find-X64Python
}
if (-not $py) { throw "Could not find or install an x64 Python." }
Write-Host ("Using Python: {0}  ({1})" -f $py, (& $py -c "import platform,sys;print(platform.machine(), sys.version.split()[0])")) -ForegroundColor Green

# --- 2. Copy project to a fast local working folder -------------------------
Section "Copying project to a local working folder"
$Work = "$env:USERPROFILE\AubreyMp3Build"
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
New-Item -ItemType Directory -Force -Path $Work | Out-Null
robocopy "$ProjectRoot" "$Work" /E /NFL /NDL /NJH /NJS /NP `
    /XD ".git" ".venv" "build" "dist" "__pycache__" "_winbuild_out" "ffmpeg_extract" /XF "*.mp3" | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed (exit $LASTEXITCODE)" }
$global:LASTEXITCODE = 0
Write-Host "Copied to $Work" -ForegroundColor Green

Push-Location $Work
try {
    # --- 3. venv + dependencies --------------------------------------------
    Section "Creating venv and installing dependencies"
    & $py -m venv .venv;                                  Check "venv creation"
    $vpy = "$Work\.venv\Scripts\python.exe"
    & $vpy -m pip install --upgrade pip --quiet;          Check "pip upgrade"
    & $vpy -m pip install --quiet -r requirements.txt pyinstaller; Check "pip install deps"
    Write-Host "Dependencies installed" -ForegroundColor Green

    # --- 4. Fetch a static x64 ffmpeg --------------------------------------
    Section "Downloading static x64 ffmpeg"
    $ffUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    Invoke-WebRequest -Uri $ffUrl -OutFile "$Work\ffmpeg.zip"
    Expand-Archive -Path "$Work\ffmpeg.zip" -DestinationPath "$Work\ffmpeg_extract" -Force
    $ff = Get-ChildItem -Path "$Work\ffmpeg_extract" -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    if (-not $ff) { throw "ffmpeg.exe not found in download" }
    New-Item -ItemType Directory -Force -Path "$Work\build" | Out-Null
    Copy-Item $ff.FullName "$Work\build\ffmpeg.exe" -Force
    Write-Host "Bundled ffmpeg from $($ff.FullName)" -ForegroundColor Green

    # --- 5. Build -----------------------------------------------------------
    Section "Building the exe with PyInstaller"
    & $vpy -m PyInstaller --noconfirm AubreyMp3.spec;     Check "PyInstaller build"
}
finally { Pop-Location }

$exe = Get-ChildItem -Path "$Work\dist" -Filter *.exe | Select-Object -First 1
if (-not $exe) { throw "Build produced no .exe" }
Write-Host ("Built: {0} ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB)) -ForegroundColor Green

# --- 6. Copy back to the project (visible on the Mac) -----------------------
Section "Copying exe back to the project folder"
$out = "$ProjectRoot\_winbuild_out"
New-Item -ItemType Directory -Force -Path $out | Out-Null
Copy-Item $exe.FullName "$out\" -Force
Write-Host "Copied to $out\$($exe.Name)" -ForegroundColor Green

Section "DONE"
Write-Host "EXE (local):  $($exe.FullName)"
Write-Host "EXE (on Mac): <project>\_winbuild_out\$($exe.Name)"
Write-Host ""
Write-Host "Launching it now so you can test the GUI..." -ForegroundColor Yellow
Start-Process $exe.FullName
