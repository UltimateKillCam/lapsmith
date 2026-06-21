"""GUI package. Importing this does NOT pull in PySide6/keyboard/fastapi - those
are imported lazily by the view modules (overlay, hotkeys, web, setup_form) so
the core package stays importable without GUI extras.

The headless `controller.Controller` is always importable and unit-tested.
"""
from . import controller

__all__ = ["controller"]
