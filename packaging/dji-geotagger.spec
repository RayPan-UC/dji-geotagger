# PyInstaller build for the desktop front end.
#
#   pyinstaller packaging/dji-geotagger.spec --noconfirm
#
# One directory, not one file. A --onefile build of this dependency set is
# several hundred megabytes that unpack to a temporary directory on every
# launch, so the window takes seconds to appear and leaves copies behind. The
# directory build starts immediately and zips to about the same size.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []

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
_RTKLIB_BIN = Path(SPECPATH).parent / "dji_geotagger" / "tools" / "RTKLIB" / "bin"
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

hiddenimports = []
# georinex reaches for backends by name, so the analysis does not see them.
hiddenimports += collect_submodules("xarray")
hiddenimports += ["netCDF4", "cftime", "defusedxml", "defusedxml.ElementTree"]
# pymap3d and astropy do the same for their own submodules.
hiddenimports += collect_submodules("pymap3d")

a = Analysis(
    ["launch_gui.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # RTKLIB is fetched on first use rather than bundled, so nothing here
    # needs its binaries. Tests and notebooks are excluded outright.
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "notebook"],
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
