import pandas as pd
import numpy as np
import pymap3d as pm


def compute_camera_position(
    mrk_df: pd.DataFrame,
    img_df: pd.DataFrame,
    pos_df: pd.DataFrame,
    full_output: bool = False    
) -> pd.DataFrame:
    """
    Compute corrected camera center ECEF position for each exposure epoch.

    Pipeline:
        1. match_mrk_xml        : merge MRK and image XML metadata by seq
        2. interpolate_pos_at_exposure : interpolate PPK antenna position to exposure epochs
        3. apply_gimbal_correction     : shift antenna ECEF to camera center ECEF

    Returns
    -------
    pd.DataFrame
        One row per image with columns:
            cam_X, cam_Y, cam_Z     : camera center ECEF (m)
            cam_lat, cam_lon, cam_h : camera center LLH (deg, deg, m)
            cov_total_ECEF          : covariance matrix (m²)
            sigma_total_ENU         : 1-sigma ENU (m)
            ... (all MRK + XML metadata columns)
    """
    # Step 1
    exposure_df = match_mrk_xml(mrk_df, img_df)

    # Step 2
    exposure_df = interpolate_pos_at_exposure(pos_df, exposure_df)

    # Step 3
    exposure_df = apply_gimbal_correction(exposure_df)

    # Step 4
    exposure_df = format_output(exposure_df, full_output=full_output)

    print(f"[INFO] Camera position computed for {len(exposure_df)} images")
    return exposure_df


def match_mrk_xml(mrk_df: pd.DataFrame, img_df: pd.DataFrame) -> pd.DataFrame:
    """
    Match MRK records with image XML metadata by sequence index.

    MRK seq is 1-based. img_df is sorted by filename and assigned seq accordingly.

    Returns
    -------
    pd.DataFrame
        Merged DataFrame with MRK GNSS data + XML metadata per image.
    """
    # img_df 
    img_df = img_df.sort_values("FileName").reset_index(drop=True)
    img_df["seq"] = img_df.index + 1  # 1-based

    # merge
    exposure_meta_df = pd.merge(mrk_df, img_df, on="seq", how="inner", suffixes=("_mrk", "_xml"))

    # check
    n_mrk = len(mrk_df)
    n_img = len(img_df)
    n_matched = len(exposure_meta_df)

    if n_matched != n_mrk or n_matched != n_img:
        print(f"[WARNING] Match count mismatch: MRK={n_mrk}, XML={n_img}, Matched={n_matched}")
    else:
        print(f"[INFO] Matched {n_matched} records (MRK <-> XML)")

    return exposure_meta_df


def interpolate_pos_at_exposure(
    pos_df: pd.DataFrame,
    exposure_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Linearly interpolate PPK rover ECEF position to each exposure epoch.
    Covariance is not interpolated; nearest epoch covariance is used instead.

    Outside-coverage exposures will have NaN for:
        X, Y, Z, cov_total_ECEF, sigma_total_ECEF, sigma_total_ENU
    """

    pos_t = pos_df["GPS_time"].values
    exp_t = exposure_df["GPS_time"].values

    # Interpolate X, Y, Z
    for col in ["X", "Y", "Z"]:
        exposure_df[col] = np.interp(exp_t, pos_t, pos_df[col].values)

    # Coverage mask
    outside_mask = (exp_t < pos_t.min()) | (exp_t > pos_t.max())
    inside_mask = ~outside_mask

    # Initialize cov/sigma columns as NaN (object dtype safe for arrays)
    exposure_df["cov_total_ECEF"] = pd.array([np.nan] * len(exposure_df), dtype=object)
    exposure_df["sigma_total_ECEF"] = pd.array([np.nan] * len(exposure_df), dtype=object)
    exposure_df["sigma_total_ENU"] = pd.array([np.nan] * len(exposure_df), dtype=object)

    # If outside: set XYZ NaN + leave cov/sigma NaN
    if outside_mask.any():
        n_outside = int(outside_mask.sum())
        exposure_df.loc[outside_mask, ["X", "Y", "Z"]] = np.nan
        print(
            f"[WARNING] {n_outside} exposure epochs outside PPK trajectory range "
            f"[{pos_t.min():.3f}, {pos_t.max():.3f}]. "
            f"Check raw GNSS data coverage. Affected images will have NaN position/cov/sigma."
        )

    # For inside: assign nearest epoch cov/sigma (no interpolation)
    if inside_mask.any():
        exp_t_in = exp_t[inside_mask]

        # nearest epoch indices for inside exposures
        idx_right = np.searchsorted(pos_t, exp_t_in, side="left")
        idx_left = np.clip(idx_right - 1, 0, len(pos_t) - 1)
        idx_right = np.clip(idx_right, 0, len(pos_t) - 1)

        # choose nearer one (handles midpoints)
        choose_right = (np.abs(pos_t[idx_right] - exp_t_in) < np.abs(exp_t_in - pos_t[idx_left]))
        nearest_idx = np.where(choose_right, idx_right, idx_left)

        # write back only to inside rows
        inside_rows = np.where(inside_mask)[0]
        exposure_df.loc[inside_rows, "cov_total_ECEF"] = pos_df["cov_total_ECEF"].iloc[nearest_idx].values
        exposure_df.loc[inside_rows, "sigma_total_ECEF"] = pos_df["sigma_total_ECEF"].iloc[nearest_idx].values
        exposure_df.loc[inside_rows, "sigma_total_ENU"] = pos_df["sigma_total_ENU"].iloc[nearest_idx].values

    # Coordinate system from .sum file (still ok)
    exposure_df["coord_sys"] = pos_df["coord_sys"].iloc[0]

    print(f"[INFO] Interpolated PPK position for {len(exposure_df)} exposure epochs")
    return exposure_df

def apply_gimbal_correction(exposure_df: pd.DataFrame) -> pd.DataFrame:
    """
    Shift interpolated PPK antenna phase center to camera CMOS center
    by applying the lever-arm offset in ECEF frame.

    The lever-arm vector (gimbal_dX/Y/Z) is pre-computed in parse_mrk()
    by converting DJI MRK NED offsets to ECEF using per-epoch lat/lon.

        cam_ECEF = antenna_ECEF + leverarm_ECEF

    Parameters
    ----------
    exposure_df : pd.DataFrame
        Must contain:
            X, Y, Z             : interpolated PPK antenna position (ECEF, m)
            gimbal_dX/Y/Z       : lever-arm vector (ECEF, m)

    Returns
    -------
    pd.DataFrame
        exposure_df with additional columns:
            cam_X, cam_Y, cam_Z : camera center ECEF (m)
            cam_lat, cam_lon    : camera center geodetic (deg)
            cam_h               : camera center ellipsoidal height (m)
    """
    exposure_df['cam_X'] = exposure_df['X'] + exposure_df['gimbal_dX']
    exposure_df['cam_Y'] = exposure_df['Y'] + exposure_df['gimbal_dY']
    exposure_df['cam_Z'] = exposure_df['Z'] + exposure_df['gimbal_dZ']

    # ECEF -> LLH
    cam_lat, cam_lon, cam_h = pm.ecef2geodetic(
        exposure_df['cam_X'].values,
        exposure_df['cam_Y'].values,
        exposure_df['cam_Z'].values
    )
    exposure_df['cam_lat'] = cam_lat
    exposure_df['cam_lon'] = cam_lon
    exposure_df['cam_h']   = cam_h

    print(f"[INFO] Gimbal correction applied for {len(exposure_df)} images")
    return exposure_df

def format_output(df: pd.DataFrame, full_output: bool = False) -> pd.DataFrame:
    """
    Format final camera position DataFrame for output.
    Splits sigma_total_ENU array into individual columns and
    optionally filters to core columns only.

    Parameters
    ----------
    df : pd.DataFrame
    full_output : bool
        If True, return all columns. If False (default), return core columns only.
    """
    # Split sigma_total_ENU -> sigma_E, sigma_N, sigma_U
    df['sigma_E'] = df['sigma_total_ENU'].apply(lambda x: x[0] if hasattr(x, '__len__') else np.nan)
    df['sigma_N'] = df['sigma_total_ENU'].apply(lambda x: x[1] if hasattr(x, '__len__') else np.nan)
    df['sigma_U'] = df['sigma_total_ENU'].apply(lambda x: x[2] if hasattr(x, '__len__') else np.nan)
    df = df.drop(columns=['sigma_total_ENU'])

    if not full_output:
        keep_cols = [
            'FileName', 'UTCAtExposure', 'coord_sys',
            'cam_lat', 'cam_lon', 'cam_h',
            'cam_X', 'cam_Y', 'cam_Z',
            'sigma_E', 'sigma_N', 'sigma_U',
            'DGT_YawDegree', 'DGT_PitchDegree', 'DGT_RollDegree',
            'rtk_status'
        ]
        df = df[keep_cols]

    return df