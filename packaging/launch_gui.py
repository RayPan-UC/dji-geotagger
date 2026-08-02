"""
Entry point for the packaged executable.

A module of its own rather than `-m dji_geotagger.gui`: PyInstaller freezes a
script, and giving it one keeps the console-vs-window decision and the crash
handling in a place that can be read.
"""

import sys
import traceback


def main() -> int:
    try:
        from dji_geotagger.gui import launch
        launch()
        return 0
    except Exception:  # noqa: BLE001
        # A windowed build has no console, so an unhandled exception would
        # otherwise close without a trace of why.
        detail = traceback.format_exc()
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, detail[-1500:], "dji-geotagger failed to start", 0x10)
        except Exception:  # noqa: BLE001
            sys.stderr.write(detail)
        return 1


if __name__ == "__main__":
    sys.exit(main())
