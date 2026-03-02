
from pathlib import Path
import pandas as pd
import numpy as np
from numpy import sign
from tqdm import tqdm
from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser
from dji_geotagger.tools.tools import ECEF2ENU_vec
import pymap3d as pm


def pos_cov_wrapper(sdx, sdy, sdz, sdxy, sdyz, sdzx) -> np.ndarray:
    """
    Reconstruct a 3x3 ECEF covariance matrix from RTKLIB .pos output fields.

    RTKLIB encodes covariance terms using a signed square-root convention:
        - Diagonal terms (variances): stored as standard deviations (sdx, sdy, sdz)
        - Off-diagonal terms (covariances): stored as sign(cov) * sqrt(|cov|)

    Reconstruction:
        P_xx = sdx²,  P_yy = sdy²,  P_zz = sdz²
        P_xy = sdxy * |sdxy|
        P_yz = sdyz * |sdyz|
        P_zx = sdzx * |sdzx|

    Parameters:
    sdx : float
        Standard deviation of X (m)
    sdy : float
        Standard deviation of Y (m)
    sdz : float
        Standard deviation of Z (m)
    sdxy : float
        Signed sqrt covariance of X-Y (m)
    sdyz : float
        Signed sqrt covariance of Y-Z (m)
    sdzx : float
        Signed sqrt covariance of Z-X (m)

    Returns:
    np.ndarray
        3x3 symmetric covariance matrix in ECEF frame (m²)
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
    base_error_propogation_on: bool = True
    ) -> pd.DataFrame:
    """
    Parse a single RTKLIB .pos file into a DataFrame with coordinates and covariance matrices.

    Reads ECEF XYZ positions and their covariance from the .pos file.
    Converts coordinates to geodetic (LLA) using pymap3d and transforms
    the covariance matrix to ENU frame.

    If cov_PPP_ECEF is provided, the PPP base station covariance is added to
    each epoch's PPK covariance to propagate base position uncertainty into
    the rover solution (error propagation: cov_total = cov_PPK + cov_PPP).

    Expected .pos header line:
        %  GPST  x-ecef(m)  y-ecef(m)  z-ecef(m)  Q  ns  sdx(m)  sdy(m)  sdz(m)  sdxy(m)  sdyz(m)  sdzx(m)  age(s)  ratio

    Parameters:
    pos_file : str or Path
        Path to the RTKLIB .pos output file.
    base_obs : str or Path, optional
        Path to base station .obs file. Used to auto-locate the corresponding
        .sum file under DGT_output/RINEX/base/PPP/<stem>.sum.
        Ignored if sum_file_path is provided.
    sum_file_path : str or Path, optional
        Direct path to CSRS-PPP .sum file for base station covariance.
        Takes priority over base_obs.
    base_error_propagation_on : bool, optional
        If True (default), loads PPP base covariance and adds it to each
        epoch's covariance (cov_total = cov_PPK + cov_PPP).
        If False, only PPK relative covariance is used.

    Returns:
    pd.DataFrame
        One row per epoch with columns:
            GPS_week        : GPS week number
            GPS_time        : GPS time of week (seconds)
            X, Y, Z         : ECEF coordinates (m)
            lat_dd          : Geodetic latitude (decimal degrees)
            lon_dd          : Geodetic longitude (decimal degrees)
            hgt             : Ellipsoidal height (m)
            cov_total_ECEF  : 3x3 total covariance matrix in ECEF (m²)
            cov_total_ENU   : 3x3 total covariance matrix in ENU (m²)
    """
    # Input
    pos_file = Path(pos_file)
    if not pos_file.exists():
        raise FileNotFoundError(f"[ERROR] File not found: {pos_file}")  

    cov_PPP_ECEF = None
    if base_error_propogation_on:
        PPP_dict = sum_file_parser(base_obs, sum_file_path)
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
            print(f"[ERROR] Line: {line.strip()} → {e}")
            continue
    
    # Save as Dataframe
    df = pd.DataFrame(records)
    print(f"[INFO] Parsed {pos_file.stem}.pos ({len(df)} records)")
    return df

def validate_pos_file(pos_file: Path, min_gpst_decimals: int = 6) -> None:
    """
    Validate RTKLIB .pos file:
    - has GPST header
    - has ECEF header (x-ecef)
    - has at least one data record
    - GPST decimal seconds precision >= min_gpst_decimals
    """
    pos_file = Path(pos_file)
    if not pos_file.exists():
        raise FileNotFoundError(f"[ERROR] File not found: {pos_file}")

    has_gpst_header = False
    has_ecef_header = False
    data_line_found = False
    gpst_precision_checked = False

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

            # Check GPST precision from HH:MM:SS.ssssss (parts[1])
            time_str = parts[1]
            if "." not in time_str:
                raise ValueError("[ERROR] GPST has no fractional seconds (no '.').")

            sec_fraction = time_str.split(".", 1)[1]
            if len(sec_fraction) < min_gpst_decimals:
                raise ValueError(
                    f"[ERROR] GPST precision must be >= {min_gpst_decimals} decimal places."
                )

            gpst_precision_checked = True

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
    if not gpst_precision_checked:
        raise ValueError("[ERROR] No GPST data lines found to check precision.")
    if not data_line_found:
        raise ValueError("[ERROR] No valid ECEF data records found in POS file.")

def pos_df_merger(
        ppk_dfs: list[pd.DataFrame]
    ):
    """
    Parse and merge multiple RTKLIB .pos files into a single time-sorted DataFrame.

    Optionally loads PPP base station results from a .sum file and propagates
    base position uncertainty into each rover epoch's covariance matrix.

    Parameters:
    pos_files : list of Path
        List of .pos file paths to parse and merge.


    Returns:
    pd.DataFrame
        Merged DataFrame sorted by GPS_week and GPS_time with duplicates removed.
        Columns are identical to parse_pos() output.
    """
    
    # Merge all pos df
    if not ppk_dfs:
        raise RuntimeError("[ERROR] All .pos files failed to parse.")
    
    merged = pd.concat(ppk_dfs, ignore_index=True)
    merged = merged.sort_values(["GPS_week", "GPS_time"]).reset_index(drop=True)
    merged = merged.drop_duplicates(subset=["GPS_week", "GPS_time"]).reset_index(drop=True)

    print(f"[INFO] Merged {len(ppk_dfs)} .pos files ({len(merged)} records in total).")
    return merged