
from pathlib import Path
import numpy as np
from dji_geotagger.tools.tools import ECEF2ENU


def sum_file_parser(sum_file_path: Path):
    """
    Parse CSRS-PPP .sum file to extract final estimated ECEF position and covariance matrix.

    Parameters:
        sum_file_path : path to .sum file

    Returns:
        dict with keys:
            X, Y, Z         : ECEF coordinates (m)
            lat_dd, lon_dd  : decimal degrees
            hgt             : ellipsoidal height (m)
            cov_ECEF        : 3x3 covariance matrix in ECEF (m^2)
            cov_ENU         : 3x3 covariance matrix in ENU (m^2)
            sigma_ENU       : 1-sigma [sE, sN, sU] (m)
            coord_sys       : coordinate system string (e.g. IGb20)
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
                coord_sys = str((parts[2])) #coordinate system
                est_X = float(parts[5])
                sigma_X = float(parts[7])  # 95% 
                

            elif parts[0] == "POS" and parts[1] == "Y":
                est_Y = float(parts[5])
                sigma_Y = float(parts[7])
                rho_XY = float(parts[8])

            elif parts[0] == "POS" and parts[1] == "Z":
                est_Z = float(parts[5])
                sigma_Z = float(parts[7]) 
                rho_XZ = float(parts[8])
                rho_YZ = float(parts[9])

            elif parts[0] == "POS" and parts[1] == "LAT":
                lat_d = float(parts[7])
                lat_m = float(parts[8])
                lat_s = float(parts[9])
                lat_dd = np.sign(lat_d) * (abs(lat_d) + lat_m / 60 + lat_s / 3600)

            elif parts[0] == "POS" and parts[1] == "LON":
                lon_d = float(parts[7])
                lon_m = float(parts[8])
                lon_s = float(parts[9])
                lon_dd = np.sign(lon_d) * (abs(lon_d) + lon_m / 60 + lon_s / 3600)

            elif parts[0] == "POS" and parts[1] == "HGT":
                hgt = float(parts[5])

    # Check all parsed
    if None in (est_X, est_Y, est_Z, sigma_X, sigma_Y, sigma_Z, rho_XY, rho_XZ, rho_YZ):
        raise ValueError("[WARNING] Some POS entries missing or could not be parsed")

    # Covariance Matrix Calculation
    # sigma
    sigma = np.array([sigma_X, sigma_Y, sigma_Z]) / 1.96 # 95% -> 1 sigma
    # correlation (ECEF)
    corr = np.array([
                        [    1.0,  rho_XY,  rho_XZ],
                        [ rho_XY,     1.0,  rho_YZ],
                        [ rho_XZ,  rho_YZ,     1.0]
                    ])
    # Covariance Matrix (ECEF)
    cov_PPP_ECEF = np.diag(sigma) @ corr @ np.diag(sigma)

    # Covariance Matrix (ENU)
    cov_PPP_ENU = ECEF2ENU(cov_ecef=cov_ECEF, lat_deg=lat_dd, lon_deg=lon_dd)
    sigma_ENU = np.sqrt(np.diag(cov_ENU))

    # Summary
    print(f"[INFO] Coord system : {coord_sys}")
    print(f"[INFO] Base ECEF    : ({est_X:.4f}, {est_Y:.4f}, {est_Z:.4f}) m")
    print(f"[INFO] Base LLH     : ({lat_dd:.7f}°, {lon_dd:.7f}°, {hgt:.4f} m)")
    print(f"[INFO] 1σ ENU       : E={sigma_ENU[0]*100:.2f} cm, N={sigma_ENU[1]*100:.2f} cm, U={sigma_ENU[2]*100:.2f} cm")

    return {
        "X": est_X,
        "Y": est_Y,
        "Z": est_Z,
        "lat_dd": lat_dd,
        "lon_dd": lon_dd,
        "hgt": hgt,
        "coord_sys": coord_sys,
        "cov_PPP_ECEF": cov_PPP_ECEF,
        "cov_PPP_ENU": cov_PPP_ENU,
        "sigma_ENU": sigma_ENU,
    }