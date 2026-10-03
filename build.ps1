<#
.SYNOPSIS
    Builds dist\RuckusRadio.exe — a standalone, windowed onefile .exe.

.DESCRIPTION
    Creates/uses .\venv, installs requirements.txt, fetches ffmpeg.exe +
    ffprobe.exe into assets\ (from Gyan's "essentials" build — demux/decode
    MP3+MP4/AAC and encode MP3 via libmp3lame, ~98 MB/binary instead of the
    ~212 MB/binary "full" build with every codec) if they aren't already
    there, generates assets\icon.ico if missing, then runs
    `pyinstaller build.spec --noconfirm`.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPython = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Output "Creating venv..."
    python -m venv venv
}

Write-Output "Installing dependencies..."
& $venvPython -m pip install --disable-pip-version-check -q -r requirements.txt

$assetsDir = Join-Path $root "assets"
New-Item -ItemType Directory -Force -Path $assetsDir | Out-Null
$ffmpegDest = Join-Path $assetsDir "ffmpeg.exe"
$ffprobeDest = Join-Path $assetsDir "ffprobe.exe"
if (-not (Test-Path $ffmpegDest) -or -not (Test-Path $ffprobeDest)) {
    Write-Output "ffmpeg.exe/ffprobe.exe missing from assets\, fetching Gyan's essentials build..."
    # Pinned release + SHA-256 of the zip: a moved "latest" link or a tampered download
    # must never end up in the exe. Bump both together when updating ffmpeg.
    $essentialsUrl = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.1/ffmpeg-9.0.1-essentials_build.zip"
    $essentialsSha256 = "fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9"
    $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("ruckus-ffmpeg-" + [Guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    $zipPath = Join-Path $tmpDir "ffmpeg-essentials.zip"
    $downloaded = $false
    $hashMismatch = $false
    try {
        Write-Output "Downloading $essentialsUrl ..."
        Invoke-WebRequest -Uri $essentialsUrl -OutFile $zipPath -UseBasicParsing
        $actualSha256 = (Get-FileHash -Algorithm SHA256 $zipPath).Hash.ToLowerInvariant()
        if ($actualSha256 -ne $essentialsSha256) {
            $hashMismatch = $true
            throw "SHA-256 mismatch for $essentialsUrl (expected $essentialsSha256, got $actualSha256)."
        }
        Expand-Archive -Path $zipPath -DestinationPath $tmpDir -Force
        $ffmpegSrc = Get-ChildItem $tmpDir -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
        $ffprobeSrc = Get-ChildItem $tmpDir -Filter "ffprobe.exe" -Recurse | Select-Object -First 1
        if (-not $ffmpegSrc -or -not $ffprobeSrc) {
            throw "ffmpeg.exe/ffprobe.exe not found inside the downloaded essentials build."
        }
        Copy-Item $ffmpegSrc.FullName $ffmpegDest -Force
        Copy-Item $ffprobeSrc.FullName $ffprobeDest -Force
        $downloaded = $true
        Write-Output "Fetched ffmpeg/ffprobe (essentials build) into assets\"
    } catch {
        if ($hashMismatch) {
            Remove-Item -Recurse -Force $tmpDir -Confirm:$false -ErrorAction SilentlyContinue
            throw "$_ Refusing to build with an unverified ffmpeg."
        }
        Write-Warning "Essentials download failed ($_). Falling back to the winget Gyan.FFmpeg install (larger, includes every codec)."
    } finally {
        Remove-Item -Recurse -Force $tmpDir -Confirm:$false -ErrorAction SilentlyContinue
    }

    if (-not $downloaded) {
        $wingetRoot = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        $found = Get-ChildItem $wingetRoot -Filter "ffmpeg.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $found) {
            throw "Could not fetch the pinned ffmpeg essentials build and no winget Gyan.FFmpeg install was found under $wingetRoot. Install it with 'winget install Gyan.FFmpeg', or download https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip yourself and copy ffmpeg.exe + ffprobe.exe into assets\ (see README.md)."
        }
        $srcDir = $found.DirectoryName
        Write-Output "Copying ffmpeg/ffprobe from $srcDir (winget fallback)"
        Copy-Item (Join-Path $srcDir "ffmpeg.exe") $ffmpegDest -Force
        Copy-Item (Join-Path $srcDir "ffprobe.exe") $ffprobeDest -Force
    }
}

$iconPath = Join-Path $assetsDir "icon.ico"
if (-not (Test-Path $iconPath)) {
    Write-Output "Generating assets\icon.ico..."
    & $venvPython tools\make_icon.py
}

$webui = Join-Path $root "webui"
if (Test-Path (Join-Path $webui "package.json")) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw "webui\ needs Node.js (npm) to build - install it from https://nodejs.org. Without it the exe would ship without its web interface."
    }
    & $venvPython tools\gen_protocol_ts.py --check
    if ($LASTEXITCODE -ne 0) { throw "webui\src\bridge\protocol.gen.json is stale: run 'venv\Scripts\python.exe tools\gen_protocol_ts.py'." }
    Write-Output "Building the web interface..."
    Push-Location $webui
    try {
        npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed in webui\" }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed in webui\" }
    } finally {
        Pop-Location
    }
    if (-not (Test-Path (Join-Path $webui "dist\index.html"))) { throw "webui\dist\index.html is missing after the build." }
}

Write-Output "Running PyInstaller..."
& (Join-Path $root "venv\Scripts\pyinstaller.exe") build.spec --noconfirm

Write-Output "Done: dist\RuckusRadio.exe"
