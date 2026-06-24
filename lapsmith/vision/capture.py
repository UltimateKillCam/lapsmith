"""Screenshot capture. Saves a PNG of the full screen (or a region) to disk.

Prefers `mss` (fast, multi-monitor), falls back to `pyautogui`. The PNG is
what an external vision reader reads - nothing here does OCR; it just grabs
pixels so they can be read.
"""
from __future__ import annotations

import os
import time
from typing import Optional, Tuple

CAPTURE_DIR = os.environ.get("FH6_CAPTURE_DIR", "captures")


def captures_dir() -> str:
    """The directory captured Heat frames are written to (for the 'open captures
    folder' button). Created on first capture; returned here so the UI can open it."""
    return os.path.abspath(CAPTURE_DIR)


def _ensure_dir() -> None:
    os.makedirs(CAPTURE_DIR, exist_ok=True)


def grab(name: str, region: Optional[Tuple[int, int, int, int]] = None,
         monotonic_tag: Optional[int] = None) -> str:
    """Capture the screen to captures/<name>_<tag>.png and return the path.

    `region` is (left, top, width, height) in pixels; None = full primary screen.
    `monotonic_tag` lets the caller supply a deterministic counter (Date.now is
    avoided so this stays reproducible).
    """
    _ensure_dir()
    tag = monotonic_tag if monotonic_tag is not None else int(time.time())
    path = os.path.join(CAPTURE_DIR, f"{name}_{tag}.png")

    if _grab_mss(path, region):
        return path
    if _grab_pillow(path, region):
        return path
    if _grab_pyautogui(path, region):
        return path
    raise RuntimeError(
        "No screenshot backend available. Install one of:\n"
        "  pip install mss        (recommended)\n"
        "  pip install pyautogui  (fallback)"
    )


def _grab_pillow(path: str, region) -> bool:
    """Pillow's ImageGrab - ALWAYS available in the frozen build (Pillow is a hard
    dependency for OCR), so the screenshot backend can't go missing the way an
    un-bundled mss/pyautogui can. Windows/macOS only (which is all we ship)."""
    try:
        from PIL import ImageGrab  # type: ignore
    except Exception:
        return False
    try:
        if region:
            l, t, w, h = region
            bbox = (l, t, l + w, t + h)              # PIL wants (l,t,r,b)
            img = ImageGrab.grab(bbox=bbox)
        else:
            img = ImageGrab.grab()
        img.save(path)
        return True
    except Exception:
        return False


def _grab_mss(path: str, region) -> bool:
    try:
        import mss  # type: ignore
        import mss.tools  # type: ignore
    except Exception:
        return False
    with mss.mss() as sct:
        if region:
            l, t, w, h = region
            mon = {"left": l, "top": t, "width": w, "height": h}
        else:
            mon = sct.monitors[1]  # primary monitor
        img = sct.grab(mon)
        mss.tools.to_png(img.rgb, img.size, output=path)
    return True


def _grab_pyautogui(path: str, region) -> bool:
    try:
        import pyautogui  # type: ignore
    except Exception:
        return False
    shot = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
    shot.save(path)
    return True


def backend_available() -> bool:
    # Pillow's ImageGrab is the always-bundled backend; mss/pyautogui are optional
    # fast paths. Probe ImageGrab.grab too, not just the import (headless => no grab).
    try:
        from PIL import ImageGrab
        ImageGrab.grab(bbox=(0, 0, 1, 1))
        return True
    except Exception:
        pass
    for mod in ("mss", "pyautogui"):
        try:
            __import__(mod)
            return True
        except Exception:
            continue
    return False


def backend_name() -> str:
    """Which screenshot backend is active (for the startup diagnostic)."""
    try:
        import mss  # noqa: F401
        return "mss"
    except Exception:
        pass
    try:
        from PIL import ImageGrab
        ImageGrab.grab(bbox=(0, 0, 1, 1))
        return "Pillow ImageGrab"
    except Exception:
        pass
    try:
        import pyautogui  # noqa: F401
        return "pyautogui"
    except Exception:
        return "none"


def screen_size() -> Optional[Tuple[int, int]]:
    """Primary-monitor (width, height) for the support-bundle env info, or None."""
    try:
        import mss
        with mss.mss() as s:
            mon = s.monitors[1] if len(s.monitors) > 1 else s.monitors[0]
            return (int(mon["width"]), int(mon["height"]))
    except Exception:
        pass
    try:
        import pyautogui
        w, h = pyautogui.size()
        return (int(w), int(h))
    except Exception:
        return None


# Windows: keep the app's OWN windows out of screen captures, so the Heat-page
# screenshot is game-only (our always-on-top overlay was obscuring Front-Left).
WDA_NONE = 0x00000000                 # window visible to captures (Windows default)
WDA_EXCLUDEFROMCAPTURE = 0x00000011   # window absent from captures (Win10 2004+)

# Dev override: if set (to anything non-empty), the overlay is ALWAYS capturable,
# regardless of the user's checkbox. Lets developers record the overlay easily.
OVERLAY_CAPTURABLE_ENV = "LAPSMITH_OVERLAY_CAPTURABLE"


def overlay_capturable(setting: bool = False) -> bool:
    """Effective 'show overlay in captures' state: the user's setting, force-ON by
    the dev env var."""
    return bool(setting) or bool(os.environ.get(OVERLAY_CAPTURABLE_ENV))


def set_window_capturable(win_id: int, capturable: bool) -> bool:
    """SetWindowDisplayAffinity for our overlay. WDA_NONE makes the window VISIBLE
    to screen capture; WDA_EXCLUDEFROMCAPTURE hides it (the default - flicker-free,
    no per-frame hide). The dev env var forces capturable ON. Returns True on
    success (Windows-only; no-op elsewhere)."""
    import sys
    if not sys.platform.startswith("win"):
        return False
    affinity = WDA_NONE if overlay_capturable(capturable) else WDA_EXCLUDEFROMCAPTURE
    try:
        import ctypes
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(win_id), affinity))
    except Exception:
        return False


def exclude_window_from_capture(win_id: int) -> bool:
    """Back-compat: unconditionally hide a window from capture."""
    import sys
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(
            int(win_id), WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False
