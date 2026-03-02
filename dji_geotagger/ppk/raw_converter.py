from pathlib import Path
import subprocess
from datetime import datetime, timezone, timedelta
from dji_geotagger.tools.install_RTKLIB import get_rtklib_executable
import georinex as gr


def raw2rinex(
    input_path: str,
    output_dir: str = None,
    antenna_height_in_meter: float = 0.0,
    appr_time: str = None,
    RTKLIB: str = None,
    ) -> Path:
    """
    Convert raw GNSS files (e.g., .dat, .bin) to RINEX using RTKLIB convbin.
    - .bin -> rover (UAV)
    - .dat -> base

    Parameters:
    input_path : Path
        Raw GNSS file path (.bin / .dat)
    output_dir : Optional[Path]
        Output base directory. Default: current working directory.
    antenna_height_in_meter : float
        Antenna height (m). If <=0 or None-like, treated as 0.0
    appr_time : Optional[str]
        Approx time in "YYYYMMDD HHMM" (UTC).
        If None, try parse from filename token "YYYYmmddHHMMSS" or "YYYYmmddHHMM".
    RTKLIB: str = None,

    Returns:
    (obs_path, nav_path) : Tuple[Path, Path]
    """
    # Check file exist
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"[ERROR] Input file not found: {input_path}")
    
    # make sure convbin exist
    convbin = get_rtklib_executable("convbin", RTKLIB)
    
    # User input (antenna_height)
    ah = float(antenna_height_in_meter or 0.0)


    # User input (appr_time)
    if appr_time:
        try:
            dt = datetime.strptime(appr_time, "%Y%m%d %H%M")
        except ValueError:
            raise ValueError(
                "[ERROR] `appr_time` must be in format YYYYMMDD HHMM"
            )
    else:
        dt = None
        # parse from filename tokens
        for token in input_path.stem.split("_"):
            if token.isdigit() and len(token) == 14:
                dt = datetime.strptime(token, "%Y%m%d%H%M%S")
                break
            if token.isdigit() and len(token) == 12:
                dt = datetime.strptime(token, "%Y%m%d%H%M")
                break

        if dt is None:
            raise ValueError(
                "[ERROR] No valid timestamp found in filename."
                "Please provide `appr_time` manually in format YYYYMMDD HHMM."
            )
    
    # User inputs (output_dir)
    # ".bin" file -> UAV (Rover)
    # ".dat" file -> Base
    base_out = Path(output_dir) if output_dir else Path.cwd()

    suffix = input_path.suffix.lower()
    if suffix == ".bin":
        type_dir = "rover(UAV)"
    elif suffix == ".dat":
        type_dir = "base"
    else:
        raise ValueError(f"[ERROR] Unsupported file extension: {suffix} (expect .bin or .dat)")

    rinex_dir = base_out / "DGT_output" / "RINEX" / type_dir
    rinex_dir.mkdir(parents=True, exist_ok=True)

    file_name = input_path.stem
    obs_path = rinex_dir / f"{file_name}.obs"
    nav_path = rinex_dir / f"{file_name}.nav"

    # Skip if exist
    if obs_path.exists() and nav_path.exists():
        print(f"[WARNING] Output exists, skipping: {input_path}")
        return obs_path, nav_path
            

    # Convert
    cmd = [
        str(convbin),
        "-r", "rtcm3",
        "-tr", dt.strftime("%Y/%m/%d %H:%M:%S"),
        "-hd", f"0/0/{ah}",
        "-o", str(obs_path),
        "-n", str(nav_path),
        str(input_path)
    ]

    print(f"[INFO] Converting: {input_path.name}  ({type_dir})")

    try:
        subprocess.run(
            cmd, 
            check=True, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
            )        
        print(f"[INFO] ✓ Converted. Output: {rinex_dir}")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"[ERROR] Failed to convert {input_path}") from e
    
    if type_dir == "base":
        PPP_help(obs_path)
    

    return obs_path, nav_path
    

def PPP_help(obs_path:Path):
    """
    NRCan Product:
      - Ultra-rapid: ~1–2 hours after last epoch
      - Rapid: ~17–18 hours after end of day (UTC)
      - Final: ~12–15 days after end of week (UTC Sunday)
    """
    # get last epoch
    times = gr.gettime(obs_path)
    if len(times) == 0:
        print("[WARNING] Cannot determine last epoch from RINEX file.")
        return
    last_epoch = times[-1].astype("datetime64[ms]").astype(datetime).replace(tzinfo=timezone.utc)

    # end of day (UTC)
    end_of_day = last_epoch.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    # end of week (UTC Sunday)
    days_until_sunday = (6 - last_epoch.weekday()) % 7
    end_of_week = last_epoch.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday + 1)

    rapid_available     = end_of_day  + timedelta(hours=18)
    final_available     = end_of_week + timedelta(days=14)
    
    now_utc = datetime.now(timezone.utc)

    if now_utc >= final_available:
        current_available = "FINAL"
    elif now_utc >= rapid_available:
        current_available = "RAPID"
    elif now_utc >= last_epoch + timedelta(hours=2):
        current_available = "ULTRA-RAPID"
    else:
        current_available = "NOT-YET"


    if current_available != "FINAL":
        print(f"""
[HINT] CSRS-PPP Product Tier Estimate: {current_available}
        TIME OF LAST OBS: {last_epoch.strftime('%Y-%m-%d %H:%M UTC')}
        Estimated FINAL availability: {final_available.strftime('%Y-%m-%d %H:%M UTC')}

        NRCan Product:
        - Ultra-rapid: ~1–2 hours after last epoch
        - Rapid: ~17–18 hours after end of day (UTC)
        - Final: ~12–15 days after end of week (UTC Sunday)
        """)
    else:
        print(f"""
[HINT] You can now submit the RINEX file to CSRS-PPP for precise positioning:
              
        - Product Tier Estimate: {current_available}

        - https://webapp.geod.nrcan.gc.ca/geod/tools-outils/ppp.php
            1. Upload your .obs file
            2. Enter your email address
            3. Download the result file when processing completes 
            4. Place the PPP summary file with your .obs file
            5. Continue processing with DJI-Geotagger.

        - Recommended options:
                1. Positioning mode: Static
                2. Coordinate system: ITRF
        
        - 
        
        Processing takes ~5–30 minutes depending on data length.
            """)

