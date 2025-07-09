from pathlib import Path
import subprocess
from datetime import datetime
from dji_geotagger.tools.install_utils import download_RTKLIB_instruction
from dji_geotagger.tools.tools import get_rtklib_executable

def extract_datetime_from_filename(file: Path) -> datetime:
    """
    Parse datetime from a filename like: DRTK3_0006_20250513073737_xxx.dat
    """
    parts = file.stem.split("_")
    for p in parts:
        if len(p) == 14 and p.isdigit():  
            return datetime.strptime(p, "%Y%m%d%H%M%S")
        elif len(p) == 12 and p.isdigit():  
            return datetime.strptime(p, "%Y%m%d%H%M")
    raise ValueError("No valid timestamp found in filename.")


def find_raw_files_by_keywords(input_dir: Path, keywords: list[str]) -> list[Path]:
    raw_files = list(input_dir.rglob("*.dat")) + list(input_dir.rglob("*.bin"))
    result = []
    for f in raw_files:
        name = f.name.lower()
        if all(k.lower() in name for k in keywords):
            result.append(f)
    return result

def raw_to_rinex_single(
    input_path: Path,
    output_dir: Path= Path("temp"),
    antenna_height_in_meter: float= 0.0,
    type: str = "base",
    convbin: Path = None
    ):

    

    # Check convbin.exe
    if convbin is None:
        convbin = get_rtklib_executable("rnx2rtkp")

    if not convbin.exists():
        success = download_RTKLIB_instruction(convbin)
        if success:
            # After download, recheck
            convbin = get_rtklib_executable("rnx2rtkp")
            if not convbin.exists():
                raise FileNotFoundError(f"[FATAL] RTKLIB tool still not found: {convbin}")
    rinex_dir = output_dir / f"rinex_{type}"
    rinex_dir.mkdir(parents=True, exist_ok=True)

    stem = input_path.stem
    obs_path = rinex_dir / f"{stem}.obs"
    nav_path = rinex_dir / f"{stem}.nav"

    # parse time from file name
    dt_start = extract_datetime_from_filename(input_path)
    ts_str = dt_start.strftime("%Y/%m/%d %H:%M:%S")


    cmd = [
        str(convbin),
        "-r", "rtcm3",
        "-tr", ts_str,
        "-hd", f"0/0/{antenna_height_in_meter}",
        "-o", str(obs_path),
        "-n", str(nav_path),
        str(input_path)
    ]

    print(f"[INFO] Converting: {input_path.name} → {type}")
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[INFO] ✓ Converted: {obs_path.name}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"[ERROR] Failed to convert {input_path}") from e


def raw_to_rinex_batch(
    keywords: list[str],
    input_dir: Path,
    output_dir: Path = Path("temp"),
    antenna_height_in_meter: float = 0.0,
    type: str = "base",
    convbin: Path = None
):
    matched_files = find_raw_files_by_keywords(input_dir, keywords)
    if not matched_files:
        print("[INFO] No matching files found.")
        return
    
    # Check convbin.exe
    if convbin is None:
        convbin = get_rtklib_executable("rnx2rtkp")

    if not convbin.exists():
        success = download_RTKLIB_instruction(convbin)
        if success:
            # After download, recheck
            convbin = get_rtklib_executable("rnx2rtkp")
            if not convbin.exists():
                raise FileNotFoundError(f"[FATAL] RTKLIB tool still not found: {convbin}")

    print(f"[INFO] Found {len(matched_files)} files for type: {type}")
    for f in matched_files:
        try:
            raw_to_rinex_single(f, output_dir, antenna_height_in_meter, type, convbin)
        except Exception as e:
            print(f"[ERROR] {f.name}: {e}")


    if type == "base":
        print("""
[INFO] Base station RINEX files have been exported.
[HINT] You can now submit the RINEX file to CSRS-PPP for precise positioning:
       
        🔗 https://webapp.geod.nrcan.gc.ca/geod/tools-outils/ppp.php
              1. Upload the `.obs` file
              2. Enter your email address to receive results

        ⚠️ Recommended options:")
                - Positioning mode: Static
                - Coordinate system: ITRF
        
        
        Processing takes ~5–30 minutes depending on data length.
              """)

