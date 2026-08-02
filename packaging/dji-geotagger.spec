# PyInstaller build for the desktop front end.
#
#   pyinstaller packaging/dji-geotagger.spec --noconfirm
#
# One directory, not one file. A --onefile build of this dependency set is
# several hundred megabytes that unpack to a temporary directory on every
# launch, so the window takes seconds to appear and leaves copies behind. The
# directory build starts immediately and zips to about the same size.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Build the repository, not whatever happens to be installed. PyInstaller runs
# the spec from this directory, where dji_geotagger is not importable at all -
# collect_data_files then reported "not a package" and returned nothing, and
# the analysis had no source to follow. Both need the root on the path.
_ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(_ROOT))

datas = []

# A frozen application has no installed distribution, so importlib.metadata
# cannot answer for it and the About box would read "development". The version
# is taken from pyproject at build time - one source, no second place to keep
# in step - and dropped in beside the package.
import tomllib

_VERSION = tomllib.loads(
    (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
_VERSION_FILE = Path(SPECPATH) / "_build_version.txt"
_VERSION_FILE.write_text(_VERSION, encoding="utf-8")
datas.append((str(_VERSION_FILE), "dji_geotagger"))

# RTKLIB. Only the two command-line tools the pipeline actually runs: the bin
# directory as shipped is 129 MB of GUI applications - rtknavi, rtkplot and
# friends - none of which this program invokes. convbin and rnx2rtkp come to
# 2.4 MB and were checked to run with no other file present, so the DLLs
# beside them belong to the GUI tools.
#
# install_RTKLIB resolves them from `tools/RTKLIB/bin` next to its own module,
# so placing them at the same relative path needs no code change.
#
# LICENSE.txt travels with them: RTKLIB is BSD-2-Clause and a binary
# distribution has to carry the notice.
_RTKLIB_BIN = _ROOT / "dji_geotagger" / "tools" / "RTKLIB" / "bin"
_RTKLIB_DEST = "dji_geotagger/tools/RTKLIB/bin"
for _name in ("convbin.exe", "rnx2rtkp.exe", "LICENSE.txt"):
    _source = _RTKLIB_BIN / _name
    if not _source.exists():
        raise SystemExit(
            f"{_source} is missing. Run the pipeline once, or call "
            f"dji_geotagger.tools.install_RTKLIB.download_rtklib_bins(), "
            f"before building."
        )
    datas.append((str(_source), _RTKLIB_DEST))

# PROJ's own database and grids. Without them every transformation fails at
# run time, in a build that imported cleanly - the failure is nine megabytes
# of data away from anything the traceback mentions.
datas += collect_data_files("pyproj")

# pywebview injects JavaScript from files shipped beside the package.
datas += collect_data_files("webview")

# The front end itself: HTML, CSS, JS and the vendored Leaflet.
datas += collect_data_files("dji_geotagger.gui", includes=["web/**/*"])

# The RTKLIB configuration defaults live in the package.
datas += collect_data_files("dji_geotagger.config")

# Named individually rather than with collect_submodules("xarray"): that
# imports every submodule of the package, optional backend integrations
# included, and those reach for whatever the build machine happens to have.
# It dragged in torch, transformers, onnxruntime, pyarrow and llvmlite -
# 500 MB of unrelated libraries in a bundle that never calls any of them.
hiddenimports = [
    "netCDF4",
    "cftime",
    "defusedxml",
    "defusedxml.ElementTree",
    "xarray.backends.netCDF4_",
]

a = Analysis(
    ["launch_gui.py"],
    pathex=[str(_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # RTKLIB is fetched on first use rather than bundled, so nothing here
    # needs its binaries. Tests and notebooks are excluded outright.
    # Belt and braces. Nothing here is a dependency; they are excluded so a
    # build machine that happens to have them cannot leak them into the
    # bundle through some optional import path.
    excludes=[
        "tkinter", "matplotlib", "IPython", "pytest", "notebook",
        "torch", "transformers", "onnxruntime", "pyarrow", "llvmlite",
        "numba", "nltk", "av", "sklearn", "scipy", "sympy", "zarr",
        "dask", "bottleneck", "flox", "cartopy", "seaborn", "plotly",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dji-geotagger",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # windowed: launch_gui.py reports failures itself
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="dji-geotagger",
)
