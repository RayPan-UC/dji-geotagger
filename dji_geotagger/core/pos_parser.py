
from pathlib import Path
import pandas as pd
import numpy as np
from numpy import sign
from tqdm import tqdm
from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser
from dji_geotagger.tools.tools import ECEF2ENU
import pymap3d as pm


    


def pos_cov_wrapper(sdx, sdy, sdz, sdxy, sdyz, sdzx) -> np.array:
    """
    Reconstruct covariance matrix P from RTKLIB POS output fields.
    POS file provides:
    sdx, sdy, sdz    → standard deviations (meters)
    sdxy, sdyz, sdzx → signed square-root covariance terms
    Diagonal elements (variances):
    P_xx = (sdx)^2
    P_yy = (sdy)^2
    P_zz = (sdz)^2
    Off-diagonal elements (covariances):
    POS stores signed sqrt(covariance).
    Therefore:
        P_xy = sdxy * abs(sdxy)
        P_yz = sdyz * abs(sdyz)
        P_zx = sdzx * abs(sdzx)
    This preserves the original covariance sign.
    """
    return np.array([
        [          sdx**2,  sdxy * abs(sdxy), sdzx * abs(sdzx)],
        [sdxy * abs(sdxy),            sdy**2, sdyz * abs(sdyz)],
        [sdzx * abs(sdzx),  sdyz * abs(sdyz),           sdz**2]
    ])

def parse_pos(
    pos_file: str,
    cov_PPP_ECEF: np.ndarray  = None    
    ) -> pd.DataFrame:
    """
    pos file example
    Headings...
    %  GPST                 x-ecef(m)      y-ecef(m)      z-ecef(m)   Q  ns   sdx(m)   sdy(m)   sdz(m)  sdxy(m)  sdyz(m)  sdzx(m) age(s)  ratio
    2377 325876.400000  -1516785.2671  -3309022.4681   5220640.1128   1   9   0.0039   0.0060   0.0076   0.0036  -0.0046  -0.0026   0.40    5.2
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
                })

        except Exception as e:
            print(f"[ERROR] Line: {line.strip()} → {e}")
            continue
        
    
    
    # Save as Dataframe
    df = pd.DataFrame(records)
    print(f"[INFO] Successfully parsed {pos_file} .pos files with a total of {len(df)} position records.")
    return df


    
    
def combine_all_pos(
        base_sum_file: Path, # PPP result (sum file), Optional
        pos_folder: Path = Path(r"temp\ppk_result"),   # PPK result (pos files)
        ):
    
    all_pos_files = list(pos_folder.rglob("*.pos"))
    records = []

    
    print(f"[INFO] {len(all_pos_files)} .pos files were found. Start merging...")

    if base_sum_file:
        X, Y, Z, lat, lon, hgt, cor_sys, cov_base = sum_file_parser(base_sum_file)

        if cor_sys not in ["IGb20", "IGS20", "ITRF2020", "IGS14", "ITRF2014"]:
            raise ValueError(f"[ERROR] Unexpected base coordinate system '{cor_sys}'. Please convert to IGS-compatible frame (e.g., IGS20) before use.")

    else:
        print("[INFO] No base .sum file provided, skip error propagation from base.")


    for pos_file in tqdm(all_pos_files, desc="[INFO] Parsing .pos files"):
        with open(pos_file, "r") as f:
            lines = f.readlines()

        data_started = False

        for line in lines:
            if not data_started:
                if line.startswith("%  GPST"):
                    data_started = True
                continue
            if line.strip() == "":
                continue

            try:
                parts = line.strip().split()
                gps_week = int(parts[0])
                gps_tow = float(parts[1])

                x, y, z = float(parts[2]), float(parts[3]), float(parts[4])

                # Construct covariance matrix (ECEF)
                sdX, sdY, sdZ, sdXY, sdYZ, sdZX= float(parts[7]), float(parts[8]), float(parts[9]), float(parts[10]), float(parts[11]), float(parts[12])
                cov_xy = sign(sdXY) * (sdXY)**2
                cov_yz = sign(sdYZ) * (sdYZ)**2
                cov_zx = sign(sdZX) * (sdZX)**2
                cov_rover = np.array([
                    [sdX**2,    cov_xy, cov_zx],
                    [cov_xy,    sdY**2, cov_yz],
                    [cov_zx,    cov_yz, sdZ**2]
                ])

                # Combine with base covariance if input contain sum file
                if cov_base is not None:
                    cov_total = cov_rover + cov_base  
                else:
                    cov_total = cov_rover
                    


                records.append({
                    "GPS_week": gps_week,
                    "GPS_time": gps_tow,
                    "X": x,
                    "Y": y,
                    "Z": z,
                    "Covariance_total": cov_total
                })

            except Exception as e:
                print(f"[WARNING] Failed to parse {pos_file.name}: {e}")
                continue

    df = pd.DataFrame(records)
    print(f"[INFO] Successfully merged {len(all_pos_files)} .pos files with a total of {len(df)} position records.")
    return df