
from pathlib import Path
import pandas as pd
import numpy as np
from numpy import sign
from tqdm import tqdm
from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser


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