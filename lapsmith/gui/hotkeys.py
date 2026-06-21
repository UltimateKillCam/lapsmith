"""Global hotkeys via the `keyboard` library, so the user advances the loop and
marks segments WITHOUT alt-tabbing - the game keeps focus and never pauses.

Lazy-imports `keyboard` (needs admin on some Windows setups). Bindings are
rebindable. Action names map to Controller calls wired in app.py.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

# action name -> default global hotkey
DEFAULT_BINDINGS: Dict[str, str] = {
    "advance": "f8",        # confirm car / baseline applied / begin test / change applied
    "end_test": "f11",      # finished driving the characterisation test
    "mark_start": "f9",     # segment timer: mark START
    "mark_end": "f10",      # segment timer: mark END
    "view_mode": "f6",      # toggle Simple / Advanced on the live overlay
    "support_bundle": "ctrl+f11",   # write a support zip anytime
    "quit": "ctrl+f12",
}


class HotkeyManager:
    def __init__(self, handlers: Dict[str, Callable[[], None]],
                 bindings: Optional[Dict[str, str]] = None):
        self.handlers = handlers
        self.bindings = bindings or DEFAULT_BINDINGS
        self._kb = None
        self._registered = []

    def available(self) -> bool:
        try:
            import keyboard  # noqa: F401
            return True
        except Exception:
            return False

    def start(self) -> bool:
        try:
            import keyboard
        except Exception:
            return False
        self._kb = keyboard
        for action, key in self.bindings.items():
            fn = self.handlers.get(action)
            if fn is None:
                continue
            try:
                self._kb.add_hotkey(key, fn)
                self._registered.append(key)
            except Exception:
                pass
        return bool(self._registered)

    def stop(self):
        if not self._kb:
            return
        for key in self._registered:
            try:
                self._kb.remove_hotkey(key)
            except Exception:
                pass
        self._registered.clear()

    def help_text(self) -> str:
        return "  ".join(f"[{k.upper()}] {a}" for a, k in self.bindings.items())
