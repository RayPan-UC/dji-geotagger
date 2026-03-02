from pathlib import Path
import requests
import zipfile
import io
import platform

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
        root_path: str = None
        ) -> Path:
    """
    Locate and return the full path to a RTKLIB executable tool.

    Search Strategy
    ---------------
    1. Check default expected path: `dji_geotagger/tools/RTKLIB/bin/<tool_name>.exe`
    2. If `root_path` provided, recursively search for the executable under that directory
    3. If not found, prompt user to auto-download from GitHub
    4. If user declines, provide manual installation instructions

    Parameters
    ----------
    tool_name : str
        Name of the RTKLIB tool (e.g., "convbin", "rnx2rtkp").
        Platform-specific extension (.exe for Windows, no extension for Linux/macOS) 
        is added automatically.
    root_path : str | Path, optional
        Custom root directory to recursively search for the tool.
        If provided and tool not found in default location, searches here before 
        prompting for auto-install. Default is None.

    Returns
    -------
    Path
        Absolute path to the RTKLIB executable.

    Raises
    ------
    FileNotFoundError
        If executable cannot be found in default location or `root_path`, user declines
        auto-install, or auto-install fails.
    ValueError
        If `root_path` does not exist (when provided).

    Notes
    -----
    - On Windows, automatically appends `.exe` extension
    - On Linux/macOS, uses tool name without extension
    - Auto-download fetches pre-compiled binaries from GitHub (RTKLIB v2.4.3)
    - Requires user confirmation before downloading
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

    # 3) still missing -> offer auto-install
    print(f"[ERROR] {exe_name} not found.")
    print(f"[INFO] Expected path: {tool_path}")

    answer = input("[HINT] Download and install RTKLIB binaries automatically? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        installed = download_rtklib_bins()
        if installed and tool_path.exists():
            return tool_path.resolve()
        else:
            raise FileNotFoundError(f"[ERROR] Download completed but {exe_name} still not found at {tool_path}")


    # 4) manual instructions
    raise FileNotFoundError(_manual_install_message(expected_tool_path=tool_path))




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
    print("[INFO] Downloading RTKLIB zip package...")
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

    print(f"[INFO] RTKLIB bin extracted to: {RTKLIB_BIN_DIR.resolve()}")
    return True