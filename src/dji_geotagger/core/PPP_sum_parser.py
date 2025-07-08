
from pathlib import Path
import numpy as np


def sum_file_parser(sum_file_path: Path):
    """
    Parse RTKLIB .sum file to extract final estimated ECEF position and covariance matrix.

    Parameters:
        sum_file_path -- path to .sum file (RTKLIB output)

    Returns:
        (X, Y, Z, lat, lon, hgt, 3x3 covariance matrix in ECEF)
    """

    # Placeholders
    est_X = est_Y = est_Z = None
    sigma_X = sigma_Y = sigma_Z = None
    rho_XY = rho_XZ = rho_YZ = None
    lat = lon = hgt = None

    with open(sum_file_path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()

            if len(parts) < 2:
                continue

            if parts[0] == "POS" and parts[1] == "X":
                cor_sys = str((parts[2])) #coordinate system
                est_X = float(parts[5])
                sigma_X = float(parts[7]) / 1.96  # 95% ➜ 1σ
                

            elif parts[0] == "POS" and parts[1] == "Y":
                est_Y = float(parts[5])
                sigma_Y = float(parts[7]) / 1.96
                rho_XY = float(parts[8])

            elif parts[0] == "POS" and parts[1] == "Z":
                est_Z = float(parts[5])
                sigma_Z = float(parts[7]) / 1.96
                rho_XZ = float(parts[8])
                rho_YZ = float(parts[9])

            elif parts[0] == "POS" and parts[1] == "LAT":
                lat_d = float(parts[7])
                lat_m = float(parts[8])
                lat_s = float(parts[9])
                lat = (lat_d + lat_m / 60 + lat_s / 3600) * np.pi / 180  # rad

            elif parts[0] == "POS" and parts[1] == "LON":
                lon_d = float(parts[7])
                lon_m = float(parts[8])
                lon_s = float(parts[9])
                lon = (lon_d + lon_m / 60 + lon_s / 3600) * np.pi / 180  # rad

            elif parts[0] == "POS" and parts[1] == "HGT":
                hgt = float(parts[5])

    # Check all parsed
    if None in (est_X, est_Y, est_Z, sigma_X, sigma_Y, sigma_Z, rho_XY, rho_XZ, rho_YZ):
        raise ValueError("[WARNING] Some POS entries missing or could not be parsed")

    cov = np.array([
        [sigma_X**2,            rho_XY * sigma_X * sigma_Y, rho_XZ * sigma_X * sigma_Z],
        [rho_XY * sigma_X * sigma_Y, sigma_Y**2,            rho_YZ * sigma_Y * sigma_Z],
        [rho_XZ * sigma_X * sigma_Z, rho_YZ * sigma_Y * sigma_Z, sigma_Z**2]
    ])

    return est_X, est_Y, est_Z, lat, lon, hgt, cor_sys, cov