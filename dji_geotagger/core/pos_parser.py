
from pathlib import Path
import pandas as pd
import numpy as np
from numpy import sign
from tqdm import tqdm
from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser
from dji_geotagger.tools.tools import ECEF2ENU_vec
import pymap3d as pm
from dji_geotagger.tools.logging_setup import get_logger

logger = get_logger(__name__)


def pos_cov_wrapper(sdx, sdy, sdz, sdxy, sdyz, sdzx) -> np.ndarray:
    """
    Reconstruct a 3x3 ECEF covariance matrix (m²) from RTKLIB .pos output fields.

    RTKLIB uses a signed square-root encoding for covariance terms:
      - Diagonal: standard deviations sdx/sdy/sdz (metres)
      - Off-diagonal: sign(cov) * sqrt(|cov|) in metres

    This function reconstructs the symmetric covariance matrix for the ECEF axes
    in the order [X, Y, Z] corresponding to x-ecef, y-ecef, z-ecef.

    Returns
    -------
    np.ndarray
        3x3 covariance matrix in ECEF frame (m²).
    """
    return np.array([
        [          sdx**2,  sdxy * abs(sdxy), sdzx * abs(sdzx)],
        [sdxy * abs(sdxy),            sdy**2, sdyz * abs(sdyz)],
        [sdzx * abs(sdzx),  sdyz * abs(sdyz),           sdz**2]
    ])

def pos2df(
    pos_file: str,
    base_obs: str = None,
    sum_file_path: str = None,
    base_error_propagation_on: bool = True,
    base_position: dict = None
    ) -> pd.DataFrame:
    """
    Parse a single RTKLIB .pos file (ECEF solution) into a DataFrame with coordinates and covariance.

    This parser expects an ECEF-format RTKLIB .pos where each data row begins with:
        GPS_week  GPS_time_of_week  X  Y  Z  ...

    It reconstructs the per-epoch ECEF covariance matrix using RTKLIB's signed-sqrt convention
    (sdx/sdy/sdz + sdxy/sdyz/sdzx), and converts:
      - ECEF XYZ -> geodetic (lat/lon/hgt) via pymap3d
      - covariance ECEF -> ENU via a rotation at the epoch's lat/lon

    Optional base error propagation (PPP):
    - If `base_error_propogation_on=True`, this function loads the base station PPP covariance
      from a CSRS-PPP .sum file (via `sum_file_parser`) and adds it to each epoch:
          cov_total_ECEF = cov_PPK_ECEF + cov_PPP_ECEF
      NOTE: In this mode, you must provide either `sum_file_path` or `base_obs`
      (so `sum_file_parser` can locate the .sum file). Otherwise an error will occur.

    Parameters
    ----------
    pos_file : str | Path
        RTKLIB .pos file path (ECEF + GPST week/tow format).
    base_obs : str | Path, optional
        Base .obs path used to auto-locate the corresponding PPP .sum file.
        Ignored if `sum_file_path` is provided.
    sum_file_path : str | Path, optional
        Direct path to CSRS-PPP .sum file (takes priority over `base_obs`).
    base_error_propagation_on : bool, default True
        If True, add PPP base covariance to rover covariance per epoch.
        If False, only rover (relative) PPK covariance is used.
    base_position : dict, optional
        Pre-resolved base position from
        :func:`~dji_geotagger.ppk.base_position.resolve_base_position`. Takes
        priority over `base_obs` / `sum_file_path`, and is the only way to use
        a base position that did not come from a .sum file.

    Returns
    -------
    pd.DataFrame
        One row per epoch with columns:
        - GPS_week, GPS_time
        - coord_sys (from PPP .sum when propagation is enabled)
        - X, Y, Z (ECEF metres)
        - lat_dd, lon_dd (degrees), hgt (metres)
        - cov_total_ECEF (3x3, m²), cov_total_ENU (3x3, m²)
        - sigma_total_ECEF (len-3, metres), sigma_total_ENU (len-3, metres)

    Notes
    -----
    - Lines that fail to parse are reported and skipped (the function does not raise on a bad line).
    - File format validation is performed by `validate_pos_file` prior to parsing.
    """
    # Input
    pos_file = Path(pos_file)
    if not pos_file.exists():
        raise FileNotFoundError(f"[ERROR] File not found: {pos_file}")  

    cov_PPP_ECEF = None
    coord_sys = None
    if base_error_propagation_on:
        # Prefer a base position the caller already resolved: it may come from
        # a source other than a .sum file (manually entered coordinates), and
        # resolving once avoids re-parsing per rover file.
        PPP_dict = base_position if base_position is not None else \
            sum_file_parser(base_obs=base_obs, sum_file_path=sum_file_path)
        cov_PPP_ECEF = PPP_dict.get("cov_PPP_ECEF")
        coord_sys =  PPP_dict.get("coord_sys")

    # Check pos file validation (exist/ECEF/GPST/deciminal>=6)
    validate_pos_file(pos_file)

    # Parse file
    with open(pos_file) as f:
            lines = f.readlines()

    data_started = False
    records = []

    for line in lines:
        if not data_started:
            if line.startswith("%  GPST"):
                data_started = True
            continue
        if line.strip() == "":
            continue

        try:
            # Retrieve data
            parts = line.strip().split()
            gps_week = int(parts[0])
            gps_tow = float(parts[1])
            x = float(parts[2])
            y = float(parts[3])
            z = float(parts[4])
            sdx  = float(parts[7])
            sdy  = float(parts[8])
            sdz  = float(parts[9])
            sdxy = float(parts[10])
            sdyz = float(parts[11])
            sdzx = float(parts[12])

            # Covariance (if PPP cov provide, conduct error propogation)
            cov_PPK_ECEF = pos_cov_wrapper(sdx, sdy, sdz, sdxy, sdyz, sdzx) # Construct covariance matrix
            cov_total_ECEF = cov_PPK_ECEF + cov_PPP_ECEF if cov_PPP_ECEF is not None else cov_PPK_ECEF

            # ECEF -> LLA (WGS84)
            lat_dd, lon_dd, hgt = pm.ecef2geodetic(x, y, z) # Coordinates
            cov_total_ENU = ECEF2ENU_vec(cov_total_ECEF, lon_deg=lon_dd, lat_deg=lat_dd) # Covariance

            # sigma
            sigma_total_ECEF = np.sqrt(np.diag(cov_total_ECEF)) # 1-sigma in ECEF (m)
            sigma_total_ENU = np.sqrt(np.diag(cov_total_ENU)) # 1-sigma in ENU (m)

            # Save as dict in list
            records.append({
                    "GPS_week": gps_week,
                    "GPS_time": gps_tow,
                    "coord_sys": coord_sys,
                    "X": x,
                    "Y": y,
                    "Z": z,
                    "lat_dd": lat_dd,
                    "lon_dd": lon_dd,
                    "hgt": hgt,
                    "cov_total_ECEF": cov_total_ECEF,
                    "cov_total_ENU": cov_total_ENU,
                    "sigma_total_ECEF": sigma_total_ECEF,
                    "sigma_total_ENU": sigma_total_ENU
                })

        except Exception as e:
            logger.error(f"Line: {line.strip()} → {e}")
            continue
    
    # Save as Dataframe
    df = pd.DataFrame(records)
    logger.info(f"Parsed {pos_file.name} ({len(df)} records)")
    return df

def validate_pos_file(pos_file: Path) -> None:
    """
    Validate an RTKLIB .pos file for this project’s expected ECEF/GPST layout.

    Checks:
    - Header contains "GPST"
    - Header indicates ECEF output ("x-ecef")
    - At least one valid numeric ECEF data record exists (X/Y/Z columns)

    Notes
    -----
    This is a lightweight validator intended to catch common mis-configurations
    (wrong time system, wrong coordinate output, empty/garbled file).
    The main parser (`pos2df`) assumes data rows are in GPS week + time-of-week seconds format.
    """
    pos_file = Path(pos_file)
    if not pos_file.exists():
        raise FileNotFoundError(f"[ERROR] File not found: {pos_file}")

    has_gpst_header = False
    has_ecef_header = False
    data_line_found = False

    with pos_file.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")

            # Header lines
            if line.startswith("%"):
                if "GPST" in line:
                    has_gpst_header = True
                if "x-ecef" in line.lower():
                    has_ecef_header = True
                continue

            # Skip empty lines
            if not line.strip():
                continue

            # Data line
            parts = line.split()
            if len(parts) < 5:
                # Typically: date time x y z ... ; if too short, skip
                continue


            # (Optional but recommended) Ensure ECEF columns are numeric
            # parts[2], parts[3], parts[4] should be x, y, z for x-ecef format
            try:
                float(parts[2]); float(parts[3]); float(parts[4])
            except ValueError:
                # Not a valid numeric row (could be malformed)
                continue

            data_line_found = True
            break  # one valid record is enough

    # Final validation checks (MUST be after reading)
    if not has_gpst_header:
        raise ValueError("[ERROR] POS file is not in GPST format (header missing).")
    if not has_ecef_header:
        raise ValueError("[ERROR] POS file is not ECEF solution (x-ecef missing).")
    if not data_line_found:
        raise ValueError("[ERROR] No valid ECEF data records found in POS file.")