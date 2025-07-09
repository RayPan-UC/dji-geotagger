
from pathlib import Path
import requests
import zipfile
import io

# RTKLIB

def download_RTKLIB_instruction(path: Path):
    answer = input("[HINT] Would you like to download and install RTKLIB automatically? [Y/n] .strip().lower()")

    def print_instruction():
            print(f"""
User declined auto-install.
Please install RTKLIB manually from the official website:
🔗 https://www.rtklib.com/
    
Then either:
1. Specify the full path to rnx2rtkp.exe when calling this script
    e.g. rnx2rtkp = Path(r"tools/RTKLIB_bin-rtklib_2.4.3/bin/rnx2rtkp.exe")
         convbin = Path(r"tools/RTKLIB_bin-rtklib_2.4.3/bin/convbin.exe")
2. Or place it in the default folder:
    {path.resolve()}

Exiting.
              """)
            
    if answer not in ["", "y", "yes"]:
        print_instruction()
        return True
    

    bin_dir = path.parent
    bin_dir.mkdir(parents=True, exist_ok=True)

    try:
        print("[INFO] Downloading RTKLIB zip package...")
        url = "https://github.com/tomojitakasu/RTKLIB_bin/archive/refs/heads/rtklib_2.4.3.zip"
        response = requests.get(url, timeout=20)

        if response.status_code != 200:
            print(f"[ERROR] Failed to download file. Status: {response.status_code}")
            print_instruction()
            return True

        print("[INFO] Extracting files...")
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            zf.extractall(bin_dir.parent)

        if path.exists():
            print(f"[INFO] RTKLIB installed successfully at: {path.resolve()}")
            return False
        else:
            print("[ERROR] RTKLIB downloaded, but rnx2rtkp.exe not found. Please verify manually.")
            return True

    except Exception as e:
        print(f"[ERROR] Failed to download or extract RTKLIB: {e}")
        return True