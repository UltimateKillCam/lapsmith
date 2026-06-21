# One-command DEV run of the LapSmith overlay (no packaging).
#   powershell -ExecutionPolicy Bypass -File run_gui.ps1
#   (optional)  -Port 5607  -Web
#
# Installs the GUI deps then launches the overlay. Run Forza in BORDERLESS
# WINDOWED with Data Out ON (127.0.0.1, same port). For global hotkeys to work
# while the game has focus, run this terminal AS ADMINISTRATOR.
param(
    [int]$Port = 5607,
    [switch]$Web
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> Installing GUI dependencies (requirements-gui.txt)"
python -m pip install -r requirements-gui.txt

# warn if the Tesseract binary isn't on PATH (OCR will fall back to manual entry)
$tess = Get-Command tesseract -ErrorAction SilentlyContinue
if (-not $tess) {
    Write-Warning "Tesseract not on PATH - OCR will fall back to manual tyre-temp entry. Install: https://github.com/UB-Mannheim/tesseract/wiki"
}

$gui_args = @("--port", $Port)
if ($Web) { $gui_args += "--web" }

Write-Host "==> Launching overlay (python -m lapsmith.gui $gui_args)"
Write-Host "    Hotkeys: [F8] advance  [F9]/[F10] mark start/end  [F11] end test  [Ctrl+F12] quit"
python -m lapsmith.gui @gui_args
