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
    ) -> Path:
    """
        Convert a single raw GNSS log file (e.g., .dat, .bin) to RINEX using RTKLIB convbin.exe.

        For base station logs, returns the first generated (.obs, .nav) file pair.
        For rover logs, returns the RINEX output folder path.

        Parameters:
            input_path (Path): Path to raw GNSS log file (e.g., DJI .dat or .bin)
            output_dir (Path, optional): Output directory to store RINEX files. Default is "temp"
            antenna_height_in_meter (float, optional): Antenna height in meters. Default is 0.0
            type (str, optional): Either "base" or "rover". Affects output subfolder and return type.
            convbin (Path, optional): Path to RTKLIB convbin.exe. Auto-detected if not provided.

        Returns:
            Union[tuple[Path, Path], Path]:
                - (obs_path, nav_path) if type == "base"
                - Path to RINEX output folder if type == "rover"
    """
    # Check convbin.exe
    if convbin is None:
        convbin = get_rtklib_executable("convbin")

    if not convbin.exists():
        success = download_RTKLIB_instruction(convbin)
        if success:
            # After download, recheck
            convbin = get_rtklib_executable("convbin")
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
        print(f"[INFO] ✓ Converted: {obs_path.name}. Output: {rinex_dir}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"[ERROR] Failed to convert {input_path}") from e
    
    if type == "base":
        base_export_hint()
        rinex_dir = output_dir / "rinex_base"
        obs_list = list(rinex_dir.glob("*.obs"))
        nav_list = list(rinex_dir.glob("*.nav"))

        if not obs_list or not nav_list:
            print(f"[WARNING] No .obs/.nav file found in {rinex_dir}")
            return None

        return obs_list[0], nav_list[0]
    
    else:
        return output_dir / "rinex_rover"



def raw_to_rinex_batch(
    keywords: list[str],
    input_dir: Path,
    output_dir: Path = Path("temp"),
    antenna_height_in_meter: float = 0.0,
    type: str = "rover",
    convbin: Path = None,
) -> Path:
    """
    Batch convert raw GNSS files (e.g., .dat, .bin) to RINEX using RTKLIB convbin.exe.

    For base station logs, returns the first converted (.obs, .nav) file pair.
    For rover logs, returns the output RINEX folder path.

    Parameters:
        keywords (list[str]): Keywords to identify raw files (e.g., ["DRTK", ".dat"]).
        input_dir (Path): Path to directory containing raw files.
        output_dir (Path, optional): Output directory to store RINEX files. Default is "temp".
        antenna_height_in_meter (float, optional): Antenna height in meters. Default is 0.0.
        type (str, optional): Either "base" or "rover". Affects output folder and return type.
        convbin (Path, optional): Path to RTKLIB convbin.exe. Auto-detected if not provided.

    Returns:
        Union[tuple[Path, Path], Path]:
            - (obs_path, nav_path) if type == "base"
            - Path to RINEX output folder if type == "rover"
    """
    matched_files = find_raw_files_by_keywords(input_dir, keywords)
    if not matched_files:
        print("[INFO] No matching files found.")
        return
    
    # Check convbin.exe
    if convbin is None:
        convbin = get_rtklib_executable("convbin")

    if not convbin.exists():
        success = download_RTKLIB_instruction(convbin)
        if success:
            # After download, recheck
            convbin = get_rtklib_executable("convbin")
            if not convbin.exists():
                raise FileNotFoundError(f"[FATAL] RTKLIB tool still not found: {convbin}")

    print(f"[INFO] Found {len(matched_files)} files for type: {type}")
    for f in matched_files:
        try:
            raw_to_rinex_single(f, output_dir, antenna_height_in_meter, type, convbin)
        except Exception as e:
            print(f"[ERROR] {f.name}: {e}")
    


    if type == "base":
        base_export_hint()
        rinex_dir = output_dir / "rinex_base"
        obs_list = list(rinex_dir.glob("*.obs"))
        nav_list = list(rinex_dir.glob("*.nav"))

        if not obs_list or not nav_list:
            print(f"[WARNING] No .obs/.nav file found in {rinex_dir}")
            return None

        return obs_list[0], nav_list[0]
    
    else:
        return output_dir / "rinex_rover"
    
    

def base_export_hint():
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