
from numpy import sin, cos, arcsin, arctan2, sqrt, degrees, radians, sign, atan2, asin, pi
import pandas as pd
from pathlib import Path
from tools.tools import vector_enu_to_ecef



def parse_mrk_line(line: str) -> dict | None:
    """
    Parses a single line of a DJI .MRK file and returns a dictionary containing the gimbal correction in ECEF.

    Parameters:
        line -- a string from the .MRK file (tab-delimited)

    Returns:
        dict with:
            - GPS_week: int
            - GPS_time: float (seconds into GPS week)
            - gimbal_dX, gimbal_dY, gimbal_dZ: float (ECEF correction in meters)
        or None if line is header or invalid
    """
    parts = line.strip().split('\t')
    if len(parts) < 11:
        return None
    try:
        gps_time = float(parts[1])
        gps_week = int(parts[2].strip('[]'))
        dN =  float(parts[3].split(',')[0]) / 1000.0  # mm → m
        dE =  float(parts[4].split(',')[0]) / 1000.0
        dU = -float(parts[5].split(',')[0]) / 1000.0  # Down → Up (sign flipped)
        lat = radians(float(parts[6].split(',')[0]))  # degrees → radians
        lon = radians(float(parts[7].split(',')[0]))

        dXYZ_ecef = vector_enu_to_ecef(lat, lon, dE, dN, dU)

        return {
            "GPS_week": gps_week,
            "GPS_time": gps_time,
            "gimbal_dE": dE,
            "gimbal_dN": dN,
            "gimbal_dU": dU,
            "gimbal_dX": dXYZ_ecef[0, 0],
            "gimbal_dY": dXYZ_ecef[1, 0],
            "gimbal_dZ": dXYZ_ecef[2, 0]
        }

    except Exception:
        return None
    
    
def combine_all_mrk(mrk_folder: Path) -> pd.DataFrame:
    """
    Reads all DJI .MRK files in a folder (recursively), parses them, and merges into a single DataFrame.

    Parameters:
        mrk_folder -- Path object pointing to the folder containing .MRK files

    Returns:
        DataFrame with columns:
            - GPS_week
            - GPS_time
            (In ENU system)
            - gimbal_dE
            - gimbal_dN
            - gimbal_dU
            (In ECEF/ITRF system)
            - gimbal_dX
            - gimbal_dY
            - gimbal_dZ
    """

    print(f"[INFO] Searching for .mrk files in: {mrk_folder}")
    all_mrk_files = list(mrk_folder.rglob("*.mrk"))
    all_records = []

    for mrk_file in all_mrk_files:
        with open(mrk_file, "r") as f:
            for line in f:
                parsed = parse_mrk_line(line)
                if parsed:
                    all_records.append(parsed)

    df = pd.DataFrame(all_records)
    print(f"[INFO] Parsed {len(all_mrk_files)} .MRK files, total {len(df)} records.")
    return df