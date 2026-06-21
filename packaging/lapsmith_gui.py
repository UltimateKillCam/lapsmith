"""PyInstaller entry point for the GUI app (PyInstaller needs a script, not -m)."""
import sys
from lapsmith.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
