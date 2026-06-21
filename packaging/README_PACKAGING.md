# Packaging the LapSmith GUI (Windows)

Produces a standalone Windows app that end users run with **no Python, no pip, and
no command line** — the Python runtime, PySide6, the OCR stack, and the PP-OCR
models are all bundled, so tyre-temp reading works **fully offline with no API key**.

> This must be built **on Windows** (PyInstaller produces a native exe; it can't
> be cross-built from Linux). The repo ships the spec + build script; you run them.

## One-command build

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

This installs deps, runs `selftest.py` (the build **aborts if it fails**), runs
PyInstaller, sanity-checks the bundle (Qt `qwindows` plugin + `.onnx` models
present), then emits — all in `dist\` :

| Artifact | What it is |
|----------|-----------|
| `dist\LapSmith\LapSmith.exe` | the self-contained **one-folder** app |
| `dist\LapSmith-<version>-portable.zip` | zipped portable build (no install) |
| `dist\LapSmith-Setup.exe` | **Inno Setup installer** (only if ISCC is present) |

The `<version>` comes from a **single source of truth**, `lapsmith/__init__.py`
`__version__`. The same value names the zip and is passed to Inno Setup
(`/DMyAppVersion`), so the installer version and the zip name never drift.

## Unsigned build — SmartScreen / antivirus warning (IMPORTANT)

**The exe and installer are UNSIGNED.** On a machine that has never seen them,
Windows **SmartScreen** will show a blue *"Windows protected your PC"* dialog, and
some antivirus may flag the fresh exe on first run. This is expected for any new,
unsigned app — it is about the *absence of a code-signing reputation*, not malware.

To run it anyway:

1. On the SmartScreen dialog, click **More info**.
2. Then click **Run anyway**.

(For a downloaded zip you can also right-click the file → **Properties** → tick
**Unblock** → **OK** before extracting.)

**Code signing is a later, paid step** (an OV/EV code-signing certificate plus
signing the exe + installer). Once signed, the SmartScreen prompt goes away as
reputation builds. Until then, the *More info → Run anyway* path above is the
expected first-run experience and should be documented to users.

## Local OCR models (PRIMARY reader — bundled automatically)

The primary reader is **RapidOCR** (PP-OCR ONNX models, ~14 MB, CPU). The spec
uses `collect_all("rapidocr_onnxruntime")` / `collect_all("onnxruntime")` plus
`collect_dynamic_libs(...)` to bundle the python modules, the `.onnx` models, the
`config.yaml`, and the native onnxruntime DLLs. RapidOCR loads its models
**relative to its own package directory**, which under PyInstaller resolves to
`sys._MEIPASS/...` — the same `_MEIPASS`-relative scheme as
`lapsmith.resource_path()`, **never an absolute path** baked in at build time. So
the models are found at runtime regardless of where the user installed the app.
Verified offline at 1920×1080, 2560×1440, and ultrawide. No network and no
`ANTHROPIC_API_KEY` are required at runtime.

## (Optional) Stage Tesseract — 16:9 FALLBACK only

Tesseract is only used if RapidOCR is unavailable; you can skip this entirely.
1. Install Tesseract-OCR (UB-Mannheim build): <https://github.com/UB-Mannheim/tesseract/wiki>
2. Copy the install into the repo so the spec can bundle it:
   ```
   packaging\tesseract\tesseract.exe
   packaging\tesseract\tessdata\eng.traineddata
   ```
   (Copy `tesseract.exe` and the `tessdata\` folder from `C:\Program Files\Tesseract-OCR\`.)
   You only need `eng.traineddata`.

At runtime the app points `pytesseract` at this bundled copy via
`read_tyres._bundled_tesseract()` (it looks in `sys._MEIPASS/tesseract/`). If you
skip this step the app still builds and runs — RapidOCR is the primary reader, and
if it ever fails the loop tunes camber by lap-time search.

## Installer (Inno Setup)

`build_windows.ps1` runs `packaging\lapsmith.iss` automatically if it finds
`ISCC.exe` (default install location or on `PATH`). To install Inno Setup:
<https://jrsoftware.org/isdl.php>. To run it by hand:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=0.1.0 packaging\lapsmith.iss
```

The installer wraps the self-contained `dist\LapSmith\` into `dist\LapSmith-Setup.exe`
and provides:
- **Start-menu** and optional **desktop** shortcuts (using `lapsmith.ico`);
- an **uninstaller** (Add/Remove Programs entry, also `lapsmith.ico`);
- a **"Always run as administrator"** checkbox — global hotkeys need elevation to
  register while the game has focus (sets the per-user `RUNASADMIN` compat flag,
  removed on uninstall);
- the bundled `THIRD-PARTY-NOTICES.txt`.

`SetupIconFile` is `lapsmith.ico`, so the installer's own exe carries the icon too.

## Third-party license notices

`THIRD-PARTY-NOTICES.txt` (repo root) is copied into the build and shipped with
both the zip and the installer. It covers:
- **RapidOCR** + **PP-OCR models** — Apache-2.0 (PaddleOCR models).
- **onnxruntime** — MIT.  **OpenCV** (`opencv-python`) — Apache-2.0.
- **PySide6 / Qt** — LGPL-3.0 (with the source-availability / relinking notice).
- **Python** runtime + stdlib — PSF License.
- **Tesseract** (only if you bundle it) — Apache-2.0.

## Run (end user — no command line)

1. In **Forza Horizon 6**: `Settings → HUD and Gameplay → Data Out` → **On**,
   IP `127.0.0.1`, Port `5607`.
2. Run the game **Borderless** (fullscreen-windowed) so the overlay can sit on top
   without the game minimizing.
3. Launch **LapSmith** (Start menu / desktop / `LapSmith.exe`). The management
   window opens with a first-run *"Getting started"* panel repeating the steps
   above. Drive briefly, then press **▶ START TUNING**.

   | Hotkey | Action |
   |--------|--------|
   | `F8` | advance (confirm car / baseline applied / begin test / apply change) |
   | `F11` | end the characterisation test (stop driving) |
   | `F9` / `F10` | mark segment **start** / **end** (the free-roam timer) |
   | `F6` | simple / advanced overlay view |
   | `F7` | switch tab |
   | `Ctrl+F12` | quit (cleanly releases UDP 5607 so a relaunch reconnects) |

   Global hotkeys may require running the app **as administrator** (the installer's
   "Always run as administrator" option sets this up).

## Notes

- The single-folder build (`COLLECT`) starts faster than one-file; the portable
  zip is just `dist\LapSmith\` zipped. To make a one-file exe instead, replace the
  `COLLECT` in the spec with a one-file `EXE(... a.binaries, a.datas ...)`.
- Optional LAN view: launch with `--web` (the exe accepts the same args), then open
  `http://<pc-ip>:8077` on a phone/second screen.
