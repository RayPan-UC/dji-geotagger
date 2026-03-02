
from pathlib import Path
import numpy as np
from dji_geotagger.tools.tools import ECEF2ENU_vec


def sum_file_parser(
        base_obs: Path = None,
        sum_file_path: str = None,
        print_report: bool = False):
    """
    Parse CSRS-PPP .sum file to extract final estimated ECEF position and covariance matrix.

    I also compared the result between PPP report and pymap3d transformation:
    [INFO] Base LLH               : (55.2942365333°, -114.6254905917°, 560.7136000000 m)
    [INFO] Base LLH (pymap3d)     : (55.2942365320°, -114.6254905912°, 560.7135461102 m)
    * The difference is at the sub-millimeter level. Both are absolutely reliable.

    Parameters:
        base_obs : path to base's .obs file
        sum_file_path : path to .sum file

    Returns:
        dict with keys:
            X, Y, Z         : ECEF coordinates (m)
            lat_dd, lon_dd  : decimal degrees
            hgt             : ellipsoidal height (m)
            cov_PPP_ECEF        : 3x3 covariance matrix in ECEF (m^2)
            cov_PPP_ENU         : 3x3 covariance matrix in ENU (m^2)
            sigma_ENU       : 1-sigma [sE, sN, sU] (m)
            coord_sys       : coordinate system string (e.g. IGb20)
    """

    # Check exist
    if sum_file_path:
        sum_file_path = Path(sum_file_path)
        if not sum_file_path.exists():
            raise FileNotFoundError(f"[ERROR] PPP summary file (.sum) not found: {sum_file_path}")
    elif base_obs:
        matches = list(base_obs.parent.glob(f"{base_obs.stem}.sum"))
        if matches:
            sum_file_path = matches[0]
            print(f"[INFO] Auto-detected PPP summary file: {sum_file_path}")
        else:
            raise FileNotFoundError(f"[ERROR] No .sum file found for: {base_obs.stem}")
    else:
        raise ValueError("[ERROR] Must provide either base_obs or sum_file_path")
    
    # Placeholders
    est_X = est_Y = est_Z = None
    sigma_X = sigma_Y = sigma_Z = None
    rho_XY = rho_XZ = rho_YZ = None
    lat_dd = lon_dd = hgt = None
    coord_sys = None

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
    if None in (est_X, est_Y, est_Z, sigma_X, sigma_Y, sigma_Z, rho_XY, rho_XZ, rho_YZ, lat_dd, lon_dd, hgt, coord_sys):
        raise ValueError("[WARNING] Some POS entries missing or could not be parsed")

    # Covariance Matrix Calculation
    # sigma
    PPP_sigma_ECEF = np.array([sigma_X, sigma_Y, sigma_Z]) / 1.96 # 95% -> 1 sigma
    # correlation (ECEF)
    corr = np.array([
                        [    1.0,  rho_XY,  rho_XZ],
                        [ rho_XY,     1.0,  rho_YZ],
                        [ rho_XZ,  rho_YZ,     1.0]
                    ])
    # Covariance Matrix (ECEF)
    cov_PPP_ECEF = np.diag(PPP_sigma_ECEF) @ corr @ np.diag(PPP_sigma_ECEF)
    # Covariance Matrix (ENU)
    cov_PPP_ENU = ECEF2ENU_vec(cov_ecef=cov_PPP_ECEF, lat_deg=lat_dd, lon_deg=lon_dd)
    PPP_sigma_ENU = np.sqrt(np.diag(cov_PPP_ENU))

    # Summary
    if print_report:
        print(f"[INFO] Coord system : {coord_sys}")
        print(f"[INFO] Base ECEF    : ({est_X:.4f}, {est_Y:.4f}, {est_Z:.4f}) m")
        print(f"[INFO] Base LLH     : ({lat_dd:.7f}°, {lon_dd:.7f}°, {hgt:.4f} m)")
        print(f"[INFO] Base 1σ ENU  : E={PPP_sigma_ENU[0]*100:.2f} cm, N={PPP_sigma_ENU[1]*100:.2f} cm, U={PPP_sigma_ENU[2]*100:.2f} cm")
        print(f"[INFO] Base 1σ ECEF : X={PPP_sigma_ECEF[0]*100:.2f} cm, Y={PPP_sigma_ECEF[1]*100:.2f} cm, Z={PPP_sigma_ECEF[2]*100:.2f} cm")

    return {
        "coord_sys": coord_sys,
        "X": est_X,
        "Y": est_Y,
        "Z": est_Z,
        "lat_dd": lat_dd,
        "lon_dd": lon_dd,
        "hgt": hgt,
        "cov_PPP_ECEF": cov_PPP_ECEF,
        "cov_PPP_ENU": cov_PPP_ENU,
        "PPP_sigma_ECEF": PPP_sigma_ECEF,
        "PPP_sigma_ENU": PPP_sigma_ENU,
    }
