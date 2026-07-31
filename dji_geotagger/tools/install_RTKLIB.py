from pathlib import Path
import requests
import zipfile
import io
import platform
import sys
from dji_geotagger.tools.logging_setup import get_logger

logger = get_logger(__name__)

TOOLS_DIR = Path(__file__).resolve().parent
RTKLIB_BIN_DIR = TOOLS_DIR / "RTKLIB" / "bin"

def _get_executable_name(tool_name: str) -> str:
    system = platform.system()

    if system == "Windows":
        return f"{tool_name}.exe"
    else:
        # Linux / Darwin (macOS)
        return tool_name

def get_rtklib_executable(
        tool_name: str,
        root_path: str = None,
        auto_install: bool = None
        ) -> Path:
    """
    Locate and return the full path to a RTKLIB executable tool.

    Search Strategy
    ---------------
    1. Check default expected path: `dji_geotagger/tools/RTKLIB/bin/<tool_name>.exe`
    2. If `root_path` provided, recursively search for the executable under that directory
    3. If not found, install from GitHub according to `auto_install`
    4. Otherwise, provide manual installation instructions

    Parameters
    ----------
    tool_name : str
        Name of the RTKLIB tool (e.g., "convbin", "rnx2rtkp").
        Platform-specific extension (.exe for Windows, no extension for Linux/macOS)
        is added automatically.
    root_path : str | Path, optional
        Custom root directory to recursively search for the tool.
        If provided and tool not found in default location, searches here before
        attempting auto-install. Default is None.
    auto_install : bool, optional
        What to do when the executable is missing.

        ``True``
            Download it without asking.
        ``False``
            Do not download; raise with manual installation instructions.
        ``None`` (default)
            Ask on the console, but only when running interactively. When
            stdin is not a terminal - a GUI, a scheduled job, a test suite -
            raise instead of asking. See Notes.

    Returns
    -------
    Path
        Absolute path to the RTKLIB executable.

    Raises
    ------
    FileNotFoundError
        If executable cannot be found in default location or `root_path`, the
        user declines auto-install, auto-install is disabled, or auto-install
        fails.
    ValueError
        If `root_path` does not exist (when provided).

    Notes
    -----
    - On Windows, automatically appends `.exe` extension
    - On Linux/macOS, uses tool name without extension
    - Auto-download fetches pre-compiled binaries from GitHub (RTKLIB v2.4.3)
    - The interactive prompt is guarded by a terminal check. A blocking
      ``input()`` in a library deadlocks any caller that has no console to
      answer it, so a non-interactive caller gets an actionable error instead.
      Such callers should either pass ``auto_install=True`` or call
      :func:`download_rtklib_bins` themselves after their own confirmation.
    """
    exe_name = _get_executable_name(tool_name)
    
    # 1) default expected path
    tool_path = RTKLIB_BIN_DIR / exe_name
    if tool_path.exists():
        return tool_path.resolve()

    # 2) optional custom search root
    if root_path:
        root = Path(root_path)
        if not root.exists():
            raise FileNotFoundError(f"Search root does not exist: {root}")

        # Search exactly for the exe name
        for file in root.rglob(exe_name):
            if file.is_file():
                return file.resolve()

    # 3) still missing -> install, ask, or refuse
    logger.error(f"{exe_name} not found.")
    logger.info(f"Expected path: {tool_path}")

    if auto_install is None:
        # Only prompt when there is a console to answer. Asking a caller that
        # cannot reply would block forever, or crash on EOF.
        answer = None
        if _is_interactive():
            try:
                answer = input(
                    "[HINT] Download and install RTKLIB binaries "
                    "automatically? [Y/n] "
                ).strip().lower()
            except (EOFError, OSError):
                # isatty() can report a terminal that nonetheless has no
                # readable input, e.g. stdin redirected from /dev/null.
                answer = None

        if answer is None:
            raise FileNotFoundError(
                f"[ERROR] {exe_name} not found at {tool_path}, and there is no "
                "console available to ask whether to download it.\n"
                "        Pass auto_install=True to download it, or call "
                "dji_geotagger.tools.install_RTKLIB.ensure_rtklib() at "
                "startup.\n"
                + _manual_install_message(expected_tool_path=tool_path)
            )
        auto_install = answer in ("", "y", "yes")

    if auto_install:
        installed = download_rtklib_bins()
        if installed and tool_path.exists():
            return tool_path.resolve()
        raise FileNotFoundError(
            f"[ERROR] Download completed but {exe_name} still not found at "
            f"{tool_path}")

    # 4) manual instructions
    raise FileNotFoundError(_manual_install_message(expected_tool_path=tool_path))


def _is_interactive() -> bool:
    """
    Report whether there is a console able to answer a prompt.

    Returns False for GUIs, scheduled runs, subprocesses with redirected
    stdin, and test suites - anywhere an ``input()`` call would hang.
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        # A closed or detached stdin raises rather than reporting False.
        return False




def _manual_install_message(expected_tool_path: Path) -> str:
    """
    Generate a formatted error message with RTKLIB installation instructions.

    Parameters
    ----------
    expected_tool_path : Path
        The expected default installation path where RTKLIB should be located.

    Returns
    -------
    str
        Formatted error message with download URL and installation options.

    Notes
    -----
    User has two options:
    1. Specify custom RTKLIB root path via function parameters
    2. Place RTKLIB at the default expected location
    """
    return (
        "\n\n\n\nPlease install RTKLIB manually from the official website: https://www.rtklib.com/\n"
        "Then either:\n"
        "1. Specify the full path to the RTKLIB installation\n"
        f"2. Or place it at the default location: {expected_tool_path}\n"
    )


def ensure_rtklib(auto_install: bool = True) -> bool:
    """
    Make sure the RTKLIB executables this package uses are present.

    Intended to be called once at startup by callers that have no console -
    a GUI, a scheduled job - so that a missing install is handled up front,
    under their own confirmation UX, rather than surfacing mid-pipeline.

    Parameters
    ----------
    auto_install : bool, default True
        Whether to download the binaries if they are missing.

    Returns
    -------
    bool
        True if every required executable is now available.
    """
    missing = [tool for tool in ("convbin", "rnx2rtkp")
               if not (RTKLIB_BIN_DIR / _get_executable_name(tool)).exists()]
    if not missing:
        return True

    logger.info(f"RTKLIB executables missing: {', '.join(missing)}")
    if not auto_install:
        return False

    download_rtklib_bins()
    return all((RTKLIB_BIN_DIR / _get_executable_name(tool)).exists()
               for tool in ("convbin", "rnx2rtkp"))


def download_rtklib_bins() -> bool:
    """
    Download and extract RTKLIB pre-compiled binaries from GitHub.

    This function downloads RTKLIB v2.4.3 binaries from the official GitHub repository
    and extracts the `/bin` directory to the default location:
    `dji_geotagger/tools/RTKLIB/bin/`

    Parameters
    ----------
    (none)

    Returns
    -------
    bool
        True if download and extraction completed successfully.
        False if user declined the installation prompt.

    Raises
    ------
    requests.exceptions.RequestException
        If network error occurs during download (timeout, connection failed, etc.).
    OSError
        If file system operations fail (permission denied, disk space, etc.).
    zipfile.BadZipFile
        If downloaded zip file is corrupted.

    Notes
    -----
    - Source: https://github.com/tomojitakasu/RTKLIB_bin (v2.4.3)
    - Extracts only the /bin directory, ignoring other files
    - Creates destination directory with parents if it doesn't exist
    - Download timeout is set to 60 seconds
    """
    RTKLIB_BIN_DIR.mkdir(parents=True, exist_ok=True)

    url = "https://github.com/tomojitakasu/RTKLIB_bin/archive/refs/heads/rtklib_2.4.3.zip"
    logger.info("Downloading RTKLIB zip package...")
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    prefix = "RTKLIB_bin-rtklib_2.4.3/bin/"

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for member in zf.namelist():
            if not member.startswith(prefix) or member.endswith("/"):
                continue
            rel = Path(member).relative_to(prefix)
            target = RTKLIB_BIN_DIR / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                out.write(src.read())

    logger.info(f"RTKLIB bin extracted to: {RTKLIB_BIN_DIR.resolve()}")
    return True