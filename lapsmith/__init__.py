"""LapSmith - a telemetry-driven auto-tuning assistant for Forza Horizon 6."""

import os
import sys

# The single source of truth for the product name. Use this for ALL user-facing
# strings (window titles, overlay header, dialogs, tray, help, file headers).
PRODUCT_NAME = "LapSmith"
__version__ = "0.1.1"


def resource_path(rel: str) -> str:
    """Resolve a path to a BUNDLED resource (icons, assets) that works both when
    running from source and when frozen by PyInstaller.

    PyInstaller unpacks bundled data files under a temp dir exposed as
    ``sys._MEIPASS``; the spec ships ``lapsmith/assets/`` there as ``assets/``.
    From source the same files live in this package's ``assets/`` folder, next to
    this file. So ``resource_path("assets/lapsmith.ico")`` resolves in both cases.
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)
