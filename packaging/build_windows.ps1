# Build the LapSmith Windows distributable (a tuning tool for Forza Horizon 6).
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Produces, in dist\ :
#   dist\LapSmith\LapSmith.exe              - the self-contained one-folder app
#   dist\LapSmith-<version>-portable.zip    - zipped portable build (no install)
#   dist\LapSmith-Setup.exe                 - Inno Setup installer (if ISCC present)
#
# The <version> comes from ONE source: lapsmith/__init__.py __version__. The same
# value names the zip and is passed to Inno (/DMyAppVersion), so they stay in sync.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)   # repo root

# --- single source of truth for the version -------------------------------------
$ver = (python -c "import lapsmith; print(lapsmith.__version__)").Trim()
if (-not $ver) { throw "could not read lapsmith.__version__" }
Write-Host "==> Building LapSmith $ver"

Write-Host "==> Installing build + runtime dependencies"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-gui.txt   # PySide6 + RapidOCR (bundled local OCR) etc.
python -m pip install pyinstaller

# Confirm the PRIMARY reader's ONNX models are present so the spec can bundle them.
python -c "import rapidocr_onnxruntime, glob, os; r=os.path.dirname(rapidocr_onnxruntime.__file__); n=len(glob.glob(os.path.join(r,'**','*.onnx'),recursive=True)); print(f'RapidOCR models found: {n}'); exit(1 if n==0 else 0)"
if ($LASTEXITCODE -ne 0) { throw "RapidOCR ONNX models not found; offline OCR would be missing. Aborting build." }

if (-not (Test-Path "packaging\tesseract\tesseract.exe")) {
  Write-Warning "packaging\tesseract\tesseract.exe not found - that's fine: RapidOCR (bundled, offline) is the PRIMARY reader. Tesseract is only an optional 16:9 fallback. See README_PACKAGING.md."
}

Write-Host "==> Running self-test before packaging (build aborts if it fails)"
python selftest.py
if ($LASTEXITCODE -ne 0) { throw "selftest failed; aborting build" }

Write-Host "==> Building with PyInstaller"
if (Test-Path "dist\LapSmith") { Remove-Item "dist\LapSmith" -Recurse -Force }
pyinstaller --noconfirm packaging\lapsmith.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

# --- sanity-check the bundle ----------------------------------------------------
$exe = "dist\LapSmith\LapSmith.exe"
if (-not (Test-Path $exe)) { throw "expected $exe was not produced" }
$qwin = Get-ChildItem -Recurse "dist\LapSmith" -Filter "qwindows.dll" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $qwin) { throw "Qt 'qwindows' platform plugin missing from the build; the GUI would not start." }
$onnx = Get-ChildItem -Recurse "dist\LapSmith" -Filter "*.onnx" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $onnx) { Write-Warning "No .onnx models found in the build; offline OCR may be unavailable." }
Write-Host "    qwindows plugin: $($qwin.FullName)"

# --- bundle the third-party notices into the app folder -------------------------
Copy-Item "THIRD-PARTY-NOTICES.txt" "dist\LapSmith\" -Force

# --- portable zip ---------------------------------------------------------------
$zip = "dist\LapSmith-$ver-portable.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "dist\LapSmith\*" -DestinationPath $zip
Write-Host "==> Portable zip: $zip"

# --- installer (Inno Setup), if ISCC is available -------------------------------
$iscc = $null
foreach ($p in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                 "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
  if (Test-Path $p) { $iscc = $p; break }
}
if (-not $iscc) {
  $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
  if ($cmd) { $iscc = $cmd.Source }
}
if ($iscc) {
  Write-Host "==> Building installer with Inno Setup"
  & $iscc "/DMyAppVersion=$ver" "packaging\lapsmith.iss"
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup (ISCC) failed" }
  Write-Host "==> Installer: dist\LapSmith-Setup.exe"
} else {
  Write-Warning "Inno Setup (ISCC.exe) not found - skipping installer. Install from https://jrsoftware.org/isdl.php then re-run, or run ISCC manually (see packaging\lapsmith.iss)."
}

Write-Host ""
Write-Host "==> Done. Artifacts in dist\ :"
Get-ChildItem dist\ -File | Where-Object { $_.Name -like "LapSmith*" } |
  ForEach-Object { "    {0}  ({1:N1} MB)" -f $_.Name, ($_.Length/1MB) }
Write-Host "    LapSmith\LapSmith.exe  (one-folder app)"
