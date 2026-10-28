# Run on Windows with Python 3.12 (64-bit) and network access for the pinned wheels.
# Only -Publish replaces the tracked assets/LAN/DeckOps_Offline.exe.
param([switch]$Publish)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$exe = Join-Path $root 'assets/LAN/DeckOps_Offline.exe'
$source = Join-Path $root 'src/launcher_plut_win.py'
foreach ($asset in @('assets/fonts/RussoOne-Regular.ttf',
                     'assets/fonts/AtkinsonHyperlegible-Regular.ttf',
                     'assets/fonts/AtkinsonHyperlegible-Bold.ttf',
                     'assets/fonts/AtkinsonHyperlegible-OFL.txt',
                     'assets/images/heroes/waw-banner.png',
                     'assets/images/heroes/bo1-banner.png',
                     'assets/images/heroes/bo2-banner.png',
                     'assets/images/heroes/mw3-banner.png',
                     'assets/LAN/launcher_offline.sh',
                     'src/ui_theme.py')) {
    if (-not (Test-Path (Join-Path $root $asset))) { throw "Missing shipped asset: $asset" }
}
$python = (Get-Command py -ErrorAction Stop).Source
$version = & $python -3.12 -c 'import sys; print(sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32)'
if ($LASTEXITCODE -ne 0 -or $version -ne 'True') { throw '64-bit Python 3.12 is required.' }
$staging = Join-Path $env:TEMP ("deckops-offline-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    & $python -3.12 -m venv (Join-Path $staging 'venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment.' }
    $venvPython = Join-Path $staging 'venv/Scripts/python.exe'
    & $venvPython -m pip install --disable-pip-version-check `
        'PyQt5==5.15.11' 'PyQt5-Qt5==5.15.2' 'PyQt5-sip==12.20.0' `
        'pyinstaller==6.16.0' 'pyinstaller-hooks-contrib==2026.9' `
        'XInput-Python==0.4.0' 'pygame==2.6.1' 'altgraph==0.17.5' `
        'packaging==26.3' 'pefile==2023.2.7' 'pywin32-ctypes==0.2.3' `
        'setuptools==84.2.1'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install pinned build dependencies.' }
    & $venvPython -m py_compile $source (Join-Path $root 'src/ui_theme.py')
    if ($LASTEXITCODE -ne 0) { throw 'Launcher/theme syntax check failed.' }
    $previousQtPlatform = $env:QT_QPA_PLATFORM
    try {
        $env:QT_QPA_PLATFORM = 'offscreen'
        & $venvPython -m unittest discover -s (Join-Path $root 'tests') -p test_offline_launcher.py -v
        if ($LASTEXITCODE -ne 0) { throw 'Offline launcher tests failed.' }
        $fontProbe = Join-Path $staging 'check_fonts.py'
        @'
import sys
sys.path.insert(0, sys.argv[1])
from PyQt5.QtWidgets import QApplication
import launcher_plut_win as launcher
app = QApplication([])
launcher.FONTS_DIR = sys.argv[2]
launcher._load_font()
assert 'russo' in launcher._DISPLAY_FAMILY.lower(), launcher._DISPLAY_FAMILY
assert 'atkinson' in launcher._BODY_FAMILY.lower(), launcher._BODY_FAMILY
assert 'atkinson' in launcher._BODY_BOLD_FAMILY.lower(), launcher._BODY_BOLD_FAMILY
assert launcher._font().family() == launcher._BODY_FAMILY
assert launcher._font(bold=True).family() == launcher._BODY_BOLD_FAMILY
assert launcher._font(display=True).family() == launcher._DISPLAY_FAMILY
'@ | Set-Content -Path $fontProbe -Encoding UTF8
        & $venvPython $fontProbe (Join-Path $root 'src') (Join-Path $root 'assets/fonts')
        if ($LASTEXITCODE -ne 0) { throw 'Display/body font registration failed.' }
    } finally {
        $env:QT_QPA_PLATFORM = $previousQtPlatform
    }
    & $venvPython -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
        --name DeckOps_Offline --paths (Join-Path $root 'src') `
        --hidden-import ui_theme --hidden-import XInput --hidden-import pygame `
        --distpath (Join-Path $staging 'dist') --workpath (Join-Path $staging 'work') `
        --specpath (Join-Path $staging 'spec') $source
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    $built = Join-Path $staging 'dist/DeckOps_Offline.exe'
    if (-not (Test-Path $built)) { throw 'PyInstaller did not produce an exe.' }
    $bytes = [IO.File]::ReadAllBytes($built)
    if ($bytes.Length -lt 1MB -or $bytes[0] -ne 77 -or $bytes[1] -ne 90) {
        throw 'Built artifact failed PE sanity check.'
    }
    if ($Publish) {
        $pending = "$exe.new"
        try {
            Copy-Item $built $pending -Force
            if (Test-Path $exe) {
                [IO.File]::Replace($pending, $exe, (Join-Path $staging 'previous.exe'))
            } else {
                Move-Item $pending $exe
            }
        } finally {
            if (Test-Path $pending) { Remove-Item $pending -Force }
        }
        Write-Host "Published: $exe"
        Get-FileHash $exe -Algorithm SHA256
    } else {
        Write-Host "Disposable build verified (not published; temporary artifact will be removed)."
        Get-FileHash $built -Algorithm SHA256
    }
} finally {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}
