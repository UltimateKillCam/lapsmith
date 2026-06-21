# PyInstaller spec for the LapSmith GUI (a tuning tool for Forza Horizon 6).
# Build on Windows:  pyinstaller packaging/lapsmith.spec  ->  dist/LapSmith/LapSmith.exe
#
# One-folder build: bundles the Python runtime, PySide6 (main window + overlay +
# tray) with its platform plugins (qwindows), the full RapidOCR + onnxruntime OCR
# stack incl. the PP-OCR .onnx models, and the app icon - so it runs with NO
# Python, NO pip, and tyre-temp reading works fully OFFLINE.
#
# OPTIONAL: stage a Tesseract install under packaging/tesseract/ (tesseract.exe +
# tessdata/eng.traineddata) for a 16:9 fallback. See README_PACKAGING.md.
import os
from PyInstaller.utils.hooks import (
    collect_all, collect_submodules, collect_dynamic_libs,
)

# PyInstaller resolves RELATIVE paths in a .spec against the spec's own directory,
# so anchor everything to the repo root (the parent of packaging/) via SPECPATH and
# use absolute paths throughout - otherwise "packaging/..." would double up.
ROOT = os.path.dirname(os.path.abspath(SPECPATH))   # noqa: F821 (PyInstaller global)
TESS = os.path.join(ROOT, "packaging", "tesseract")
ASSETS = os.path.join(ROOT, "lapsmith", "assets")
APP_ICON = os.path.join(ASSETS, "lapsmith.ico")
MAIN = os.path.join(ROOT, "packaging", "lapsmith_gui.py")

datas, binaries, hiddenimports = [], [], []

# App icon + PNG (lapsmith/assets/*) -> "assets/" at the bundle root, where
# lapsmith.resource_path("assets/lapsmith.ico") looks (sys._MEIPASS/assets).
if os.path.isdir(ASSETS):
    for f in os.listdir(ASSETS):
        datas.append((os.path.join(ASSETS, f), "assets"))

# Optional Tesseract fallback -> "tesseract/" where read_tyres._bundled_tesseract()
# looks (sys._MEIPASS/tesseract).
if os.path.isdir(TESS):
    for dirpath, _dirs, files in os.walk(TESS):
        for f in files:
            src = os.path.join(dirpath, f)
            rel = os.path.relpath(dirpath, TESS)
            dest = os.path.join("tesseract", rel) if rel != "." else "tesseract"
            datas.append((src, dest))

# OCR stack: collect EVERYTHING (python modules + PP-OCR .onnx models + config.yaml
# + native libraries) so the reader is fully offline. RapidOCR loads its models
# relative to its OWN package dir, which under PyInstaller resolves to
# sys._MEIPASS/<pkg>/... - the same _MEIPASS-relative scheme as resource_path(),
# never an absolute path baked in at build time.
for pkg in ("rapidocr_onnxruntime", "rapidocr", "onnxruntime"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Belt-and-suspenders for the native onnxruntime / OCR DLLs in case a given
# version's hook misses them.
for pkg in ("onnxruntime", "rapidocr_onnxruntime"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass

# PySide6: the app uses ONLY QtCore / QtGui / QtWidgets. We deliberately do NOT
# collect_submodules("PySide6") - that drags in QtWebEngine (~150 MB), Qt3D,
# QtQuick/QML, Charts, Multimedia, etc. (~450 MB of dead weight). PyInstaller's
# import-driven analysis pulls the three modules we actually import, and the
# built-in PySide6 hook still ships the Qt platform plugins (incl. qwindows).
hiddenimports += ["keyboard", "fastapi", "uvicorn", "pytesseract", "PIL", "mss",
                  "onnxruntime", "rapidocr_onnxruntime"]

# Belt-and-suspenders: explicitly exclude the heavy Qt modules the app never uses,
# so no transitive reference can drag them (or their big DLLs) back in.
qt_excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtWebView",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtQuickTest",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner",
    "PySide6.QtLocation", "PySide6.QtPositioning", "PySide6.QtSensors",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtRemoteObjects",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
]

# Heavy scientific/data libs pulled in TRANSITIVELY (by build/runtime deps) but NOT
# used by the OCR inference path (rapidocr_onnxruntime + onnxruntime + opencv +
# numpy), nor by the telemetry/GUI paths. Verified: nothing under lapsmith imports
# them, and a real RapidOCR() init loads none of them. Excluding them trims ~175 MB.
# (numpy + opencv are KEPT - OCR needs them.)
lib_excludes = ["pandas", "scipy", "matplotlib", "pyarrow"]

a = Analysis(
    [MAIN],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"] + qt_excludes + lib_excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="LapSmith",
    console=False,            # GUI app, no console window
    icon=APP_ICON,            # the exe (and its taskbar/Explorer icon)
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    name="LapSmith",
)
