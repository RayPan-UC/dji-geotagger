from pathlib import Path
import subprocess
import pandas as pd
from dji_geotagger.tools.install_RTKLIB import get_rtklib_executable
from dji_geotagger.ppk.ephemeris_downloader import download_igs_data
from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser
from dji_geotagger.config.import_config import override_rtklib_config
from dji_geotagger.core.pos_parser import pos2df
from dji_geotagger.tools.logging_setup import get_logger
from dji_geotagger.tools.progress import as_progress
from dji_geotagger.ppk.time_check import check_time_overlap

logger = get_logger(__name__)


def process_ppk(
    base_obs: str | Path,
    base_nav: str | Path,
    rover_obs: str | Path,
    sum_file_path: str | Path = None,
    user_conf: dict = None,
    ephemeris_files: list[str | Path] = None,
    base_error_propagation_on: bool = True,
    output_dir: Path = None,
    RTKLIB: Path = None,
    overwrite: bool = False,
    base_position: dict = None,
    auto_install: bool = None,
    progress=None,
    check_overlap: bool = True
    ) -> pd.DataFrame:
    """
    Run a single RTKLIB PPK solution (rnx2rtkp) for one rover observation file and
    return the parsed trajectory as a DataFrame.

    Workflow
    --------
    1) Resolve base station ECEF coordinates (X/Y/Z, metres) from a CSRS-PPP .sum file
       or from manual entries in `user_conf` (ant2-pos1/2/3).
    2) Create a temporary RTKLIB config file by overriding defaults with `user_conf`.
    3) Ensure precise ephemeris/clock files are available:
       - uses `ephemeris_files` if provided
       - otherwise downloads IGS products via `download_igs_data()`
    4) Run RTKLIB `rnx2rtkp` to produce a rover trajectory `.pos` file.
       If the output already exists, `overwrite` controls whether to re-run.
    5) Parse the `.pos` output into a DataFrame via `pos2df()`. If enabled,
       PPP base covariance may be propagated into rover covariance.

    Base coordinate priority
    ------------------------
    1) `sum_file_path` (explicit CSRS-PPP .sum file)
    2) `base_obs` (used by `sum_file_parser` to auto-locate a .sum file)
    3) Manual XYZ in `user_conf`: ant2-pos1/ant2-pos2/ant2-pos3

    Ephemeris priority
    ------------------
    1) `ephemeris_files` (user-supplied *.sp3/*.clk)
    2) Auto-download via `download_igs_data(base_obs_path=base_obs)`

    Parameters
    ----------
    base_obs : str | Path
        Base station RINEX observation file.
    base_nav : str | Path
        Base station RINEX navigation file.
    rover_obs : str | Path
        Rover RINEX observation file to solve.
    sum_file_path : str | Path, optional
        CSRS-PPP .sum file path providing base ECEF coordinates (and possibly covariance).
        If not provided, `sum_file_parser` may attempt to locate it using `base_obs`.
    user_conf : dict, optional
        RTKLIB configuration overrides. If base position is not resolved from PPP inputs,
        you must provide base XYZ via ant2-pos1/2/3. This dict will be updated in-place
        with resolved base position entries (ant2-postype, ant2-pos1/2/3).
    ephemeris_files : list[str | Path], optional
        Precise orbit/clock products (*.sp3/*.clk). If None, downloads are attempted.
    base_error_propagation_on : bool, default True
        Whether to propagate base station PPP covariance into rover covariance when parsing
        the final `.pos` (passed through to `pos2df`).
    output_dir : Path, optional
        Output folder for `.pos` results. Defaults to <cwd>/DGT_output/PPK_result.
    RTKLIB : Path, optional
        Optional RTKLIB location passed to `get_rtklib_executable()` to locate `rnx2rtkp`.
    overwrite : bool, default False
        If True, re-run rnx2rtkp even if the output .pos already exists.
    base_position : dict, optional
        Pre-resolved base station position from
        :func:`~dji_geotagger.ppk.base_position.resolve_base_position`. Supply
        this to use a base position obtained from automated CSRS-PPP
        submission or from manually entered coordinates, rather than from a
        .sum file on disk. Takes priority over `sum_file_path` / `base_obs`,
        and is resolved once and reused for both the RTKLIB config and the
        covariance propagation.
    auto_install : bool, optional
        Controls what happens when the required RTKLIB executable is missing.
        ``True`` downloads it, ``False`` refuses, ``None`` (default) asks on
        the console but only when running interactively. Non-interactive
        callers such as a GUI get an error instead of a hung prompt; see
        :func:`~dji_geotagger.tools.install_RTKLIB.get_rtklib_executable`.
    check_overlap : bool, default True
        Verify that the base station observed while the rover was flying,
        before invoking RTKLIB. Raises when they do not overlap. Set False
        only if you have already checked, or deliberately want to attempt a
        solve anyway.

    Returns
    -------
    pd.DataFrame
        Parsed rover trajectory table produced by `pos2df()`. Typically includes per-epoch:
        GPS time (week/tow), ECEF XYZ, geodetic lat/lon/hgt, and covariance/sigma fields.

    Raises
    ------
    FileNotFoundError
        If required input files (base_obs/base_nav/rover_obs or user-supplied ephemeris_files) are missing.
    RuntimeError
        If rnx2rtkp execution fails.
    ValueError
        If base position cannot be resolved from PPP inputs or manual entries in user_conf.
    """

    # Check input
    base_obs = Path(base_obs)
    base_nav = Path(base_nav)
    rover_obs = Path(rover_obs)
    ephemeris_files = [Path(p) for p in ephemeris_files] if ephemeris_files else None
    output_dir = Path(output_dir) if output_dir else Path.cwd() / "DGT_output" / "PPK_result"
    output_dir.mkdir(parents=True, exist_ok=True)
    user_conf = user_conf or {}

    # Check files exist
    for file in [base_obs, base_nav, rover_obs]:
        if file and not file.exists():
            raise FileNotFoundError(f"[ERROR] File not found: {file}")

    if ephemeris_files:
        for file in ephemeris_files:
            if not file.exists():
                raise FileNotFoundError(f"[ERROR] Ephemeris file not found: {file}")
    

    progress = as_progress(progress)
    progress.check()

    # PPK is differential, so a rover epoch with no simultaneous base data
    # cannot be resolved. Checked here rather than after the solve: RTKLIB
    # would not refuse the job, it would just return a shorter trajectory.
    if check_overlap:
        check_time_overlap(base_obs, rover_obs)

    # Check rnx2rtkp
    rnx2rtkp = get_rtklib_executable("rnx2rtkp", RTKLIB, auto_install)

    # Handle base station configuration. Resolved once here and reused for both
    # the RTKLIB config and the covariance propagation in pos2df, so the .sum
    # is not parsed twice per rover file.
    base_conf = base_pos_to_rtklib_conf(base_obs, sum_file_path, user_conf,
                                        base_position=base_position)
    user_conf.update(base_conf)
    conf_file = override_rtklib_config(user_conf)

    # Download ephemeris data (.clk and .sp3)
    if not ephemeris_files:
        ephemeris_files = download_igs_data(base_obs_path=base_obs)   
    
    # start ppk
    output_pos = output_dir / f"{rover_obs.stem}.pos"

    need_solve = True
    if output_pos.exists():
        logger.warning(f"{output_pos.name} already exists.")
        need_solve = overwrite
        if not need_solve:
            logger.info(f"Using existing: {output_pos.name}")

    if need_solve:
        cmd = [
            str(rnx2rtkp),
            "-k", str(conf_file),
            "-o", str(output_pos),
            str(rover_obs),
            str(base_obs),
            str(base_nav),
            *[str(f) for f in ephemeris_files],
        ]

        logger.info(f"Solving: {rover_obs.name} ...")
        progress.update("ppk", f"Solving {rover_obs.name}")
        returncode = progress.run_subprocess(cmd)
        if returncode != 0:
            raise RuntimeError(
                f"[ERROR] Failed to process: {rover_obs.name} "
                f"(rnx2rtkp exit {returncode})")
        logger.info(f"Finished: {output_pos.name}")

    # .pos -> df
    df = pos2df(
            pos_file=output_pos,
            base_obs=base_obs,
            sum_file_path=sum_file_path,
            base_error_propagation_on=base_error_propagation_on,
            base_position=base_position)
    return df


def base_pos_to_rtklib_conf(
    base_obs: Path = None,
    sum_file_path: str | Path = None,
    user_conf: dict | None = None,
    base_position: dict | None = None
    ) -> dict:
    """
    Resolve base station ECEF XYZ coordinates and return RTKLIB config entries.

    Resolution priority
    -------------------
    0) If `base_position` is provided, use it. This is a base position already
       resolved by
       :func:`~dji_geotagger.ppk.base_position.resolve_base_position`, and may
       originate from any source, not only a .sum file.
    1) If `sum_file_path` is provided, parse the CSRS-PPP .sum file directly.
    2) If `base_obs` is provided (without explicit `sum_file_path`), attempt to
       auto-locate a .sum file using `sum_file_parser()`.
    3) Otherwise, use manual XYZ from `user_conf` keys: ant2-pos1, ant2-pos2, ant2-pos3.

    Parameters
    ----------
    base_obs : Path, optional
        Base station RINEX observation file path used to help locate the PPP .sum result.
        Default is None.
    sum_file_path : str | Path, optional
        Explicit PPP .sum file path. Default is None.
    user_conf : dict, optional
        User config dictionary that may contain manual base XYZ entries (metres):
        - ant2-pos1: X coordinate (metres)
        - ant2-pos2: Y coordinate (metres)
        - ant2-pos3: Z coordinate (metres)
        Default is None.

    Returns
    -------
    dict
        RTKLIB config entries for base position:
        - ant2-postype: "xyz"
        - ant2-pos1: X coordinate (metres)
        - ant2-pos2: Y coordinate (metres)
        - ant2-pos3: Z coordinate (metres)

    Raises
    ------
    ValueError
        If neither PPP inputs (sum_file_path/base_obs) nor manual XYZ (ant2-pos1/2/3) are provided.
    """

    required = {"ant2-pos1", "ant2-pos2", "ant2-pos3"} # for Priority 3
    user_conf = user_conf or {}
    # Priority 0: caller already resolved the base position (any source)
    if base_position is not None:
        X, Y, Z = base_position["X"], base_position["Y"], base_position["Z"]
    # Priority 1 & 2: .sum file
    elif sum_file_path or base_obs:
        PPP_result = sum_file_parser(base_obs=base_obs, sum_file_path=sum_file_path)
        X, Y, Z = PPP_result["X"], PPP_result["Y"], PPP_result["Z"]
    # Priority 3: manual user_conf
    elif required.issubset(user_conf):
        logger.info("Using manual base coordinates from user_conf.")
        X, Y, Z = user_conf["ant2-pos1"], user_conf["ant2-pos2"], user_conf["ant2-pos3"]    
    # No input
    else:
        raise ValueError("[ERROR] Base position not provided. Supply sum_file_path, base_obs, or ant2-pos1/2/3 in user_conf.")
    
    conf = {
        "ant2-postype": "xyz",
        "ant2-pos1": X,
        "ant2-pos2": Y,
        "ant2-pos3": Z,
    }

    # The coordinate above describes the marker, not the antenna, whenever the
    # observation header carries a delta - CSRS-PPP subtracts it before
    # reporting. RTKLIB has to be told the same offset or it will place the
    # antenna at the marker and drag every rover position down with it.
    #
    # Read back from the header rather than taken as an argument, so it holds
    # for observations this package did not convert.
    delta = read_antenna_delta(base_obs) if base_obs else None
    if delta is not None and any(abs(v) > 1e-6 for v in delta):
        up, east, north = delta
        conf.update({"ant2-antdelu": up,
                     "ant2-antdele": east,
                     "ant2-antdeln": north})
        logger.info(
            f"Base antenna delta from the RINEX header: H={up:.4f} "
            f"E={east:.4f} N={north:.4f} m (position refers to the marker).")

    return conf


def read_antenna_delta(obs_file: str | Path) -> tuple[float, float, float] | None:
    """
    Read ``ANTENNA: DELTA H/E/N`` from a RINEX observation header.

    Returns ``(up, east, north)`` in metres, or None when the file has no such
    line. The values are the offset from the marker to the antenna reference
    point, which is the same convention RTKLIB uses for ``ant2-antdel*``, so
    they transfer directly.
    """
    try:
        with Path(obs_file).open("r", errors="ignore") as handle:
            for line in handle:
                if "ANTENNA: DELTA H/E/N" in line:
                    values = line[:60].split()
                    if len(values) >= 3:
                        return tuple(float(v) for v in values[:3])
                    return None
                if "END OF HEADER" in line:
                    break
    except (OSError, ValueError):
        return None
    return None


def no_base_pos_warn():
    """
    Generate a formatted warning message for missing base position configuration.

    Returns
    -------
    str
        Formatted error message with instructions for providing base coordinates.
    """
    return("""
[ERROR] No base position provided.
        Please provide base coordinates via one of the following methods:
        
          Option 1 - Provide a CSRS-PPP .sum file (recommended):
              process_ppk(..., sum_file_path='path/to/result.sum')
        
          Option 2 - Manually specify ECEF coordinates via user_conf:
              user_conf = {
                  'ant2-postype': 'xyz',
                  'ant2-pos1': -2418456.789,  # X (metres)
                  'ant2-pos2':  5385936.123,  # Y (metres)
                  'ant2-pos3':  2405716.456,  # Z (metres)
              }
              process_ppk(..., user_conf=user_conf)
    """)









