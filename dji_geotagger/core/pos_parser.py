
from pathlib import Path
import pandas as pd
import numpy as np
from numpy import sign
from tqdm import tqdm
from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser
from dji_geotagger.tools.tools import ECEF2ENU
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

def parse_pos(
    pos_file: str,
    cov_PPP_ECEF: np.ndarray = None
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
    cov_PPP_ECEF : np.ndarray, optional
        3x3 PPP base station covariance matrix in ECEF (m²).
        If provided, added to each epoch's covariance for full error propagation.
        If None, only PPK relative covariance is used.

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

    # Check pos file validation (exist/ECEF/GPST/deciminal>=6)

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
            sdx  = float(parts[8])
            sdy  = float(parts[9])
            sdz  = float(parts[10])
            sdxy = float(parts[11])
            sdyz = float(parts[12])
            sdzx = float(parts[13])

            # Covariance (if PPP cov provide, conduct error propogation)
            cov_PPK_ECEF = pos_cov_wrapper(sdx, sdy, sdz, sdxy, sdyz, sdzx) # Construct covariance matrix
            cov_total_ECEF = cov_PPK_ECEF + cov_PPP_ECEF if cov_PPP_ECEF is not None else cov_PPK_ECEF

            # ECEF -> LLA (WGS84)
            lat_dd, lon_dd, hgt = pm.ecef2geodetic(x, y, z) # Coordinates
            cov_total_ENU = ECEF2ENU(cov_total_ECEF, lon_deg=lon_dd, lat_deg=lat_dd) # Covariance

            # sigma
            sigma_total_ECEF = np.sqrt(np.diag(cov_total_ECEF)) # 1-sigma in ECEF (m)
            sigma_total_ENU = np.sqrt(np.diag(cov_total_ENU)) # 1-sigma in ENU (m)
            # Save as dict in list
            records.append({
                    "GPS_week": gps_week,
                    "GPS_time": gps_tow,
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

def pos_df_merger(
        pos_files: list[Path],
        base_obs: str = None,
        sum_file_path: str = None,
        base_error_propogation_on: bool = True,
    ):
    """
    Parse and merge multiple RTKLIB .pos files into a single time-sorted DataFrame.

    Optionally loads PPP base station results from a .sum file and propagates
    base position uncertainty into each rover epoch's covariance matrix.

    Parameters:
    pos_files : list of Path
        List of .pos file paths to parse and merge.
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
        Merged DataFrame sorted by GPS_week and GPS_time with duplicates removed.
        Columns are identical to parse_pos() output.
    """
    # Input Validation
    if not pos_files:
        raise RuntimeError("[ERROR] No .pos files provided.")  

    cov_PPP_ECEF = None
    if base_error_propogation_on:
        PPP_dict = sum_file_parser(base_obs, sum_file_path)
        cov_PPP_ECEF = PPP_dict.get("cov_PPP_ECEF")
    
    
    # Merge all pos file as df
    print(f"\n======= Merging {len(pos_files)} .pos files to dataframe =======")
    ppk_dfs = []
    for pos in pos_files:
        try:
            df = parse_pos(pos, cov_PPP_ECEF=cov_PPP_ECEF)
            ppk_dfs.append(df)
        except Exception as e:
            print(f"[ERROR] Failed to parse {pos.name} → {e}")
            continue

    if not ppk_dfs:
        raise RuntimeError("[ERROR] All .pos files failed to parse.")
    
    merged = pd.concat(ppk_dfs, ignore_index=True)
    merged = merged.sort_values(["GPS_week", "GPS_time"]).reset_index(drop=True)
    merged = merged.drop_duplicates(subset=["GPS_week", "GPS_time"]).reset_index(drop=True)

    print(f"[INFO] Merged {len(ppk_dfs)} .pos files ({len(merged)} records in total).")
    return merged