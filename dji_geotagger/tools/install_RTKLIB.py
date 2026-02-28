
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
    Return the full path to a RTKLIB tool (e.g. convbin, rnx2rtkp).
    Default location: dji_geotagger/tools/RTKLIB/bin/<tool_name>.exe

    If root_path is provided, it will recursively search under that folder too.
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
    return (
        "\n\n\n\nPlease install RTKLIB manually from the official website: https://www.rtklib.com/\n"
        "Then either:\n"
        "1. Specify the full path to the RTKLIB\n"
        f"2. Or place it at the default location: {expected_tool_path}\n"
    )




def download_rtklib_bins() -> bool:
    """
    Ask user for permission, download RTKLIB_bin zip, and extract bin/ into RTKLIB_BIN_DIR.

    Returns
    -------
    bool
        True if download/extract was performed (or already present), False if user declined.
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