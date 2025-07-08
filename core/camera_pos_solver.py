from pathlib import Path
import pandas as pd
import numpy as np
from tqdm import tqdm
from core.mrk_parser import combine_all_mrk
from core.exif_parser import combine_all_img_info
from core.pos_parser import combine_all_pos

def match_img_by_mrk_nearest(df_img_info, df_mrk, tolerance=0.002):
    """
    Match each image to its closest MRK correction by GPS time (within same GPS week).
    """
    matched_rows = []

    for _, img in df_img_info.iterrows():
        same_week = df_mrk[df_mrk["GPS_week"] == img["GPS_week"]].copy()
        same_week["time_diff"] = (same_week["GPS_time"] - img["GPS_time"]).abs()

        nearest = same_week.sort_values("time_diff").head(1)
        if nearest.empty or nearest["time_diff"].values[0] > tolerance:
            continue  # skip if no close MRK match

        match = img.to_dict()
        match.update({
            "gimbal_dx": nearest["gimbal_dX"].values[0],
            "gimbal_dy": nearest["gimbal_dY"].values[0],
            "gimbal_dz": nearest["gimbal_dZ"].values[0],
        })
        matched_rows.append(match)

    return pd.DataFrame(matched_rows)


def interpolate_between(before, after, ratio):
    """
    Interpolate position and covariance between two time points.
    """
    result = {}
    for key in ["X", "Y", "Z", "Covariance_total"]:
        if key == "Covariance_total":
            result[key] = (1 - ratio) * np.array(before[key]) + ratio * np.array(after[key])
        else:
            result[key] = (1 - ratio) * before[key] + ratio * after[key]
    return result

def apply_gimbal_offset(position_xyz, offset_xyz):
    """
    Apply MRK correction vector to PPK interpolated position.
    """
    return tuple(np.array(position_xyz) + np.array(offset_xyz))


def compute_camera_positions(df_img_info, df_mrk, df_pos):
    """
    Interpolates camera positions using PPK trajectory and MRK corrections.

    Parameters:
        df_img_info -- DataFrame containing image metadata (FileName, GPS_week, GPS_time, attitude, etc.)
        df_mrk      -- DataFrame containing MRK correction vectors (ECEF)
        df_pos      -- DataFrame of interpolated PPK positions with covariance matrices

    Returns:
        DataFrame containing image camera center in ECEF with estimated uncertainties.
    """

    df_merge = match_img_by_mrk_nearest(df_img_info, df_mrk)
    results = []

    for _, row in tqdm(df_merge.iterrows(), desc="[INFO] Interpolating image positions using PPK and MRK data", total=len(df_merge)):
        gps_week, gps_time = row["GPS_week"], row["GPS_time"]
        df_pos_week = df_pos[df_pos["GPS_week"] == gps_week]

        before = df_pos_week[df_pos_week["GPS_time"] <= gps_time].sort_values("GPS_time").tail(1)
        after  = df_pos_week[df_pos_week["GPS_time"] >= gps_time].sort_values("GPS_time").head(1)
        if before.empty or after.empty:
            continue

        t0, t1 = before["GPS_time"].values[0], after["GPS_time"].values[0]
        ratio = (gps_time - t0) / (t1 - t0) if t1 != t0 else 0

        interp = interpolate_between(before.iloc[0], after.iloc[0], ratio)
        corrected_xyz = apply_gimbal_offset(
            (interp["X"], interp["Y"], interp["Z"]),
            (row["gimbal_dx"], row["gimbal_dy"], row["gimbal_dz"])
        )

        cov_ecef = interp["Covariance_total"]
        sd_xyz = np.sqrt(np.diag(cov_ecef))  # [sd_x, sd_y, sd_z]

        results.append({
            "file_name": row["FileName"],
            "gps_week": gps_week,
            "gps_time": gps_time,
            "x_ecef": corrected_xyz[0],
            "y_ecef": corrected_xyz[1],
            "z_ecef": corrected_xyz[2],
            "sd_x_ecef": sd_xyz[0],
            "sd_y_ecef": sd_xyz[1],
            "sd_z_ecef": sd_xyz[2],
            "cov_matrix": cov_ecef,
            "flight_roll": row["flightRoll"],
            "flight_pitch": row["flightPitch"],
            "flight_yaw": row["flightYaw"],
            "gimbal_roll": row["gimbalRoll"],
            "gimbal_pitch": row["gimbalPitch"],
            "gimbal_yaw": row["gimbalYaw"],
            "dji_geotagger_roll": row["dji_geotagger_roll"],
            "dji_geotagger_pitch": row["dji_geotagger_pitch"],
            "dji_geotagger_yaw": row["dji_geotagger_yaw"]
        })

    return pd.DataFrame(results)


def load_and_compute_camera_positions(
    mrk_dir: Path,
    img_dir: Path,
    pos_dir: Path,
    base_sum_file: Path,
) -> pd.DataFrame:
    """
    Load all image/MRK/POS data and compute camera centers.

    Parameters:
        mrk_dir (Path): Folder containing .MRK files.
        img_dir (Path): Folder containing images files.
        pos_file  (Path): Interpolated PPK or PPP .pos/.sum file.

    Returns:
        DataFrame with interpolated camera positions (ECEF + uncertainties).
    """
    df_mrk = combine_all_mrk(mrk_dir)
    df_img_info = combine_all_img_info(img_dir)
    df_pos = combine_all_pos(base_sum_file = base_sum_file, pos_folder=pos_dir)

    final_df = compute_camera_positions(df_img_info, df_mrk, df_pos)
    return final_df