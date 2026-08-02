"""
Graphical front end for dji-geotagger.

Kept out of the top-level package import so that ``import dji_geotagger`` in a
headless script never pulls in pywebview or opens a display connection::

    from dji_geotagger.gui import launch
    launch()
"""

try:
    from dji_geotagger.gui.app import launch
except ModuleNotFoundError as exc:  # pragma: no cover - import-time guidance
    if exc.name != "webview":
        raise
    raise ModuleNotFoundError(
        "The desktop front end needs pywebview, which is not part of the base "
        "install:\n\n    pip install dji-geotagger[gui]\n\nor, working from a "
        "checkout:\n\n    pip install pywebview\n\nCheck which interpreter is "
        "active first - a virtual environment will not see a system-wide "
        "install."
    ) from exc

__all__ = ["launch"]
