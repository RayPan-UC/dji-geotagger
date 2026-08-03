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
    Convert raw GNSS files (.dat, .bin, .RTK) to RINEX using RTKLIB convbin.

    File Type Mapping
    -----------------
    - .bin → Rover (UAV) GNSS data, written by a P1 or similar camera payload
    - .RTK → Rover (UAV) GNSS data, written by an L2
    - .dat → Base station GNSS data

    All three are RTCM 3. The L2 names its stream differently and puts a short
    preamble in front of it, which convbin skips by resynchronising on the
    first frame; the contents are the same MSM5 observations and ephemerides
    a P1 folder carries. The L2's other sidecars are not RTCM - in particular
    ``.RTB``, despite the name, is not the base's corrections in any form
    convbin reads.

    Parameters
    ----------
    input_path : str | Path
        Path to raw GNSS file (.bin, .RTK or .dat).
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
        If file extension is not .bin, .rtk or .dat.
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
    if suffix in (".bin", ".rtk"):
        type_dir = "rover(UAV)"
    elif suffix == ".dat":
        type_dir = "base"
    else:
        raise ValueError(
            f"[ERROR] Unsupported file extension: {suffix} "
            "(expect .bin, .rtk or .dat)")

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
        # Two argv tokens, not one. convbin parses the date from the token
        # after -tr and the time from the one after that, so a single
        # "Y/m/d H:M:S" string makes it swallow whichever option follows -
        # here -hd, silently, which is how the antenna height came to be
        # dropped for so long.
        "-tr", dt.strftime("%Y/%m/%d"), dt.strftime("%H:%M:%S"),
        # convbin documents this as "antenna delta h/e/n", height first.
        "-hd", f"{ah}/0/0",
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

    _force_antenna_delta(obs_path, ah)

    if type_dir == "base":
        PPP_help(obs_path)
    

    return obs_path, nav_path
    

def _force_antenna_delta(obs_path: Path, height: float,
                         east: float = 0.0, north: float = 0.0) -> None:
    """
    Ensure the antenna delta is in a converted RINEX header.

    Normally ``convbin -hd`` has already written it and this returns without
    touching the file. The check is kept because the consequence of a missing
    delta is not cosmetic: with no delta the solution refers to the antenna
    reference point rather than the ground mark, so a 2 m tripod puts the base
    2 m too high - and the base height propagates into every camera position.
    CSRS-PPP shows the value it received as "ARP to Marker" on the report.

    This originally existed because every converted header read::

        0.0000        0.0000        0.0000      ANTENNA: DELTA H/E/N

    which was assumed to be the decoder preferring the antenna height carried
    in RTCM message 1006 - a DJI D-RTK 3 broadcasts 1006 with that field set
    to 0.0000. Measured since, on a base file with 17,123 of those messages:
    ``-hd`` is applied and 1006 does not override it. The real cause was that
    ``-tr`` was being passed as one argv token and was swallowing ``-hd``.

    The line is fixed-format: three F14.4 fields, label at column 61.
    """
    label = "ANTENNA: DELTA H/E/N"
    replacement = f"{height:14.4f}{east:14.4f}{north:14.4f}".ljust(60) + label

    try:
        lines = obs_path.read_text(errors="ignore").splitlines(keepends=True)
    except OSError as exc:
        logger.warning(f"[WARN] Could not set the antenna delta: {exc}")
        return

    for index, line in enumerate(lines):
        if label in line:
            if line.rstrip("\r\n") == replacement:
                return                      # already right, leave the file alone
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            lines[index] = replacement + ending
            obs_path.write_text("".join(lines))
            logger.info(
                f"Antenna delta set to H={height:.4f} E={east:.4f} "
                f"N={north:.4f} m in {obs_path.name}")
            return

        if "END OF HEADER" in line:
            break

    logger.warning(
        f"[WARN] No '{label}' line in {obs_path.name}; antenna height "
        f"{height:.4f} m not applied.")


def PPP_help(obs_path: Path):
    """
    Report which NRCan orbit/clock product tier CSRS-PPP can currently use.

    Submitting before the FINAL orbits are published still works, but the base
    position it returns is weaker, and nothing in the .sum flags that the
    solution was ultra-rapid. Since the base position propagates into every
    image, that is worth knowing before submitting rather than after.

    Parameters
    ----------
    obs_path : Path
        Path to RINEX observation file (.obs).

    Returns
    -------
    None
        Logs the estimated tier; warns when it is below FINAL.

    Notes
    -----
    Availability, measured from the last observation epoch:

    - Ultra-rapid: ~1-2 hours
    - Rapid: ~17-18 hours after end of day (UTC)
    - Final: ~12-15 days after end of week (UTC Sunday)

    Submission itself is automated - see
    :func:`~dji_geotagger.ppk.base_position.resolve_base_position` with
    ``mode="online"``.
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


    obs_time = last_epoch.strftime('%Y-%m-%d %H:%M UTC')
    if current_available == "FINAL":
        logger.info(f"CSRS-PPP ephemeris tier: FINAL (last obs {obs_time})")
    elif current_available == "NOT-YET":
        logger.warning(
            f"CSRS-PPP ephemeris tier: none yet (last obs {obs_time}). "
            "Even ultra-rapid orbits are not published until ~2 h after the "
            "last epoch. FINAL expected "
            f"{final_available.strftime('%Y-%m-%d %H:%M UTC')}.")
    else:
        logger.warning(
            f"CSRS-PPP ephemeris tier: {current_available} "
            f"(last obs {obs_time}). FINAL expected "
            f"{final_available.strftime('%Y-%m-%d %H:%M UTC')}; "
            "reprocessing then gives a better base position.")

