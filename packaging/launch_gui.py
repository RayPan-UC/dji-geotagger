"""
Entry point for the packaged executable.

A module of its own rather than `-m dji_geotagger.gui`: PyInstaller freezes a
script, and giving it one keeps the console-vs-window decision and the crash
handling in a place that can be read.
"""

import os
import sys
import traceback

#: Extensions worth clearing. .NET assemblies are the ones that actually
#: refuse to load; the rest are cheap to include and cost one failed unlink
#: each when there is nothing to remove.
_UNBLOCK_SUFFIXES = (".dll", ".exe", ".pyd")


def _unblock_bundle() -> None:
    """
    Clear the downloaded-from-the-internet mark on our own files, on Windows.

    Windows tags a downloaded archive with a Zone.Identifier alternate data
    stream and copies that tag onto every file extracted from it. The .NET
    Framework then refuses to load an assembly it considers untrusted, so
    pythonnet finds Python.Runtime.dll, fails to resolve Loader.Initialize
    inside it, and the window never opens. The traceback points at the DLL
    and says nothing about zones, which makes it near-undiagnosable for
    anyone who just downloaded a release.

    Right-clicking the zip and ticking Unblock before extracting avoids it,
    but nobody knows to do that. This does the same thing the checkbox does,
    to files we shipped ourselves, and must run before webview is imported -
    the assembly load happens inside that import.

    Nothing here weakens a security boundary: SmartScreen still evaluates
    the executable, and only files inside our own bundle are touched. Every
    failure is ignored, because a read-only install or a non-NTFS volume is
    a reason to carry on and let the real error surface, not to refuse to
    start.
    """
    root = getattr(sys, "_MEIPASS", None)
    if sys.platform != "win32" or root is None:
        return  # not frozen, or not a platform with alternate data streams
    for folder, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(_UNBLOCK_SUFFIXES):
                try:
                    os.remove(os.path.join(folder, name) + ":Zone.Identifier")
                except OSError:
                    pass  # not marked, or not removable; neither is fatal


def main() -> int:
    try:
        _unblock_bundle()
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
