# Opens the Revenue Chaser dashboard with no console window. The desktop shortcut runs this with the venv's
# pythonw.exe (scripts/make_shortcut.ps1); run_dashboard.bat is the visible fallback.
import os
import sys

# This folder is the package RevenueChaser, so its parent must be importable (run_dashboard.bat used to set
# PYTHONPATH for that; pythonw only puts this folder itself on sys.path).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from RevenueChaser.app import launcher
except Exception as exc:  # noqa: BLE001 - pythonw has no console: say it in a box, not nowhere
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, f"The launcher could not be loaded: {exc!r}\n\n"
                                           "run_dashboard.bat runs the same start in a visible window.",
                                     "Revenue Chaser", 0x10)
    raise SystemExit(1) from exc

raise SystemExit(launcher.main())
