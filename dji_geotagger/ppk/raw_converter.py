from pathlib import Path
import subprocess
from datetime import datetime, timezone, timedelta
from dji_geotagger.tools.install_RTKLIB import get_rtklib_executable
import georinex as gr
from dji_geotagger.tools.logging_setup import get_logger
from dji_geotagger.tools.progress import as_progress

logger = get_logger(__name__)


def raw2rinex(
    input_path: str,
    output_dir: str = None,
    antenna_height_in_meter: float = 0.0,
    appr_time: str = None,
    RTKLIB: str = None,
    auto_install: bool = None,
    progress=None,
    ) -> tuple[Path, Path]:
    """
    Convert raw GNSS files (.dat, .bin) to RINEX format using RTKLIB convbin.

    File Type Mapping
    -----------------
    - .bin → Rover (UAV) GNSS data
    - .dat → Base station GNSS data

    Parameters
    ----------
    input_path : str | Path
        Path to raw GNSS file (.bin or .dat).
    output_dir : str | Path, optional
        Output base directory for RINEX files. If not provided, defaults to current working directory.
        RINEX files will be saved to: `output_dir/DGT_output/RINEX/{rover(UAV)|base}/`
        Default is None.
    antenna_height_in_meter : float, optional
        Antenna height above ground in metres. If ≤0 or None, treated as 0.0 m.
        Default is 0.0.
    appr_time : str, optional
        Approximate observation start time in format "YYYYMMDD HHMM" (UTC).
        If not provided, the function attempts to parse timestamp from filename tokens.
        Supported filename formats: "YYYYmmddHHMMSS" or "YYYYmmddHHMM".
        Default is None.
    RTKLIB : str | Path, optional
        Path to RTKLIB installation directory. If not provided, uses system default.
        Default is None.
    auto_install : bool, optional
        Controls what happens when the required RTKLIB executable is missing.
        ``True`` downloads it, ``False`` refuses, ``None`` (default) asks on
        the console but only when running interactively. Non-interactive
        callers such as a GUI get an error instead of a hung prompt; see
        :func:`~dji_geotagger.tools.install_RTKLIB.get_rtklib_executable`.

    Returns
    -------
    tuple[Path, Path]
        A tuple containing:
        - obs_path (Path): Path to generated RINEX observation file (.obs)
        - nav_path (Path): Path to generated RINEX navigation file (.nav)

    Raises
    ------
    FileNotFoundError
        If input file does not exist.
    ValueError
        If `appr_time` format is invalid, or if no valid timestamp can be parsed from filename.
    ValueError
        If file extension is not .bin or .dat.
    RuntimeError
        If RTKLIB convbin conversion fails.

    Notes
    -----
    - If output files already exist, conversion is skipped and existing paths are returned.
    - For .dat (base station) files, a PPP processing hint is automatically displayed.
    """
    # Check file exist
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"[ERROR] Input file not found: {input_path}")
    
    # make sure convbin exist
    convbin = get_rtklib_executable("convbin", RTKLIB, auto_install)
    
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
        logger.warning(f"Output exists, skipping: {input_path}")
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

    logger.info(f"Converting: {input_path.name}  ({type_dir})")

    progress = as_progress(progress)
    progress.update("convert", f"Converting {input_path.name}")

    # Supervised rather than awaited, so a cancel request during a large
    # conversion is acted on instead of queued until the child exits.
    returncode = progress.run_subprocess(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if returncode != 0:
        raise RuntimeError(
            f"[ERROR] Failed to convert {input_path} (convbin exit "
            f"{returncode})")
    logger.info(f"✓ Converted. Output: {rinex_dir}")
    
    if type_dir == "base":
        PPP_help(obs_path)
    

    return obs_path, nav_path
    

def PPP_help(obs_path: Path):
    """
    Display CSRS-PPP product tier availability estimate and submission instructions.

    Based on the last observation epoch in the RINEX file, this function estimates
    which NRCan precise orbit/clock product tier is currently available and provides
    instructions for submitting the data to CSRS-PPP for post-processing.

    Parameters
    ----------
    obs_path : Path
        Path to RINEX observation file (.obs).

    Returns
    -------
    None
        Prints status and instructions to console.

    Notes
    -----
    Product Availability Timeline:
    - Ultra-rapid orbits: ~1–2 hours after last observation epoch
    - Rapid orbits: ~17–18 hours after end of day (UTC)
    - Final orbits: ~12–15 days after end of calendar week (UTC Sunday)

    CSRS-PPP Submission Steps:
    1. Extract the .obs file from the PPK output directory
    2. Upload to https://webapp.geod.nrcan.gc.ca/geod/tools-outils/ppp.php
    3. Select positioning mode: "Static"
    4. Select coordinate system: "ITRF"
    5. Download result .sum file when processing completes
    6. Place .sum file in the same directory as the .obs file
    7. Re-run DJI-Geotagger PPK processing with the .sum file path
    """
    # get last epoch
    times = gr.gettime(obs_path)
    if len(times) == 0:
        logger.warning("Cannot determine last epoch from RINEX file.")
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
        logger.info(f"""
CSRS-PPP Product Tier Estimate: {current_available}
        TIME OF LAST OBS: {last_epoch.strftime('%Y-%m-%d %H:%M UTC')}
        Estimated FINAL availability: {final_available.strftime('%Y-%m-%d %H:%M UTC')}

        NRCan Product:
        - Ultra-rapid: ~1–2 hours after last epoch
        - Rapid: ~17–18 hours after end of day (UTC)
        - Final: ~12–15 days after end of week (UTC Sunday)
        """)
    else:
        logger.info(f"""
You can now submit the RINEX file to CSRS-PPP for precise positioning:
              
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

