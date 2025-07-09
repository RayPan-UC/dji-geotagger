
from pathlib import Path
import requests
import zipfile
import io
from dji_geotagger.tools.tools import get_library_root_path

# RTKLIB
def download_RTKLIB_instruction(path: Path) -> bool:
    """
    Downloads and installs RTKLIB executables (convbin.exe, rnx2rtkp.exe)
    into the specified path if they are not found.

    Args:
        path (Path): Expected full path to the RTKLIB tool (e.g., tools/RTKLIB/bin/rnx2rtkp.exe)

    Returns:
        bool: True if installation failed or was declined, False if successful
    """
    print(f"[ERROR] {path.name} not found. Expected path: {path}")
    answer = input("[HINT] Would you like to download and install RTKLIB automatically? [Y/n] ").strip().lower()

    def print_instruction():
        print(f"""
User declined auto-install.
Please install RTKLIB manually from the official website:
🔗 https://www.rtklib.com/

Then either:
1. Specify the full path to the RTKLIB executable, e.g.:
    rnx2rtkp = Path("tools/RTKLIB/bin/rnx2rtkp.exe")
    convbin  = Path("tools/RTKLIB/bin/convbin.exe")
2. Or place it at the default location:
    {path.resolve()}

Exiting.
        """)

    if answer not in ["", "y", "yes"]:
        print_instruction()
        return True

    # Always install to library_root/tools/RTKLIB/bin
    bin_dir = get_library_root_path() / "tools" / "RTKLIB" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    try:
        print("[INFO] Downloading RTKLIB zip package...")
        url = "https://github.com/tomojitakasu/RTKLIB_bin/archive/refs/heads/rtklib_2.4.3.zip"
        response = requests.get(url, timeout=20)

        if response.status_code != 200:
            print(f"[ERROR] Failed to download file. Status: {response.status_code}")
            print_instruction()
            return True

        print("[INFO] Extracting bin/ folder contents...")
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            for member in zf.namelist():
                if member.startswith("RTKLIB_bin-rtklib_2.4.3/bin/") and not member.endswith("/"):
                    relative_path = Path(member).relative_to("RTKLIB_bin-rtklib_2.4.3/bin")
                    target_path = bin_dir / relative_path
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as source, open(target_path, "wb") as out_file:
                        out_file.write(source.read())

        # Final check
        if path.exists():
            print(f"[✓] RTKLIB installed successfully at: {bin_dir.resolve()}")
            return False
        else:
            print("[ERROR] RTKLIB downloaded, but expected executable not found. Please verify manually.")
            return True

    except Exception as e:
        print(f"[ERROR] Failed to download or extract RTKLIB: {e}")
        return True