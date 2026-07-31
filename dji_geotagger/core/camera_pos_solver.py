import pandas as pd
import numpy as np
import pymap3d as pm
from dji_geotagger.tools.logging_setup import get_logger

logger = get_logger(__name__)


def compute_camera_position(
    mrk_df: pd.DataFrame,
    img_df: pd.DataFrame,
    pos_df: pd.DataFrame,
    full_output: bool = False    
) -> pd.DataFrame:
    """
    Compute corrected camera center positions for each exposure epoch.

    This function builds an exposure-level table by:
      1) `match_mrk_xml`              : merge MRK records and image metadata by sequence index (seq)
      2) `interpolate_pos_at_exposure`: interpolate rover PPK antenna ECEF positions to exposure epochs
      3) `apply_gimbal_correction`    : apply lever-arm (ECEF) to shift antenna phase center -> camera center
      4) `format_output`             : reshape output columns (e.g., split ENU sigma) and optionally filter columns

    Parameters
    ----------
    mrk_df : pd.DataFrame
        Parsed MRK table. Must include:
        - seq, GPS_time
        - gimbal_dX, gimbal_dY, gimbal_dZ (lever-arm in ECEF, metres)
        - rtk_status (optional but recommended)
    img_df : pd.DataFrame
        Parsed image metadata table. Must include:
        - FileName, UTCAtExposure
        - (optional) DGT_YawDegree, DGT_PitchDegree, DGT_RollDegree
    pos_df : pd.DataFrame
        Rover PPK trajectory (ECEF). Must include:
        - GPS_time, X, Y, Z
        - cov_total_ECEF, sigma_total_ECEF, sigma_total_ENU (as object arrays)
        - coord_sys (string; usually from PPP .sum)
    full_output : bool, default False
        If True, return all intermediate columns (MRK + image metadata + interpolated fields).
        If False, return a compact set of core fields for geotagging / photogrammetry import.

    Returns
    -------
    pd.DataFrame
        Exposure-level table. Always contains camera center coordinates:
        - cam_X, cam_Y, cam_Z (ECEF metres)
        - cam_lat, cam_lon (degrees), cam_h (ellipsoidal height, metres)

        When `full_output=False`, the output is reduced to a core subset (see `format_output`).
        When `full_output=True`, all merged columns are retained.
    """
    # Step 1
    exposure_df = match_mrk_xml(mrk_df, img_df)

    # Step 2
    exposure_df = interpolate_pos_at_exposure(pos_df, exposure_df)

    # Step 3
    exposure_df = apply_gimbal_correction(exposure_df)

    # Step 4
    exposure_df = format_output(exposure_df, full_output=full_output)

    logger.info(f"Camera position computed for {len(exposure_df)} images")
    return exposure_df


def match_mrk_xml(mrk_df: pd.DataFrame, img_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge MRK records with image metadata by DJI exposure sequence index (`seq`).

    Notes
    -----
    - DJI MRK `seq` is 1-based.
    - This function sorts `img_df` by `FileName` and assigns `seq = index + 1`.
      (i.e., it assumes filename order matches exposure order.)
    - Uses an inner join; unmatched rows on either side are dropped.

    Parameters
    ----------
    mrk_df : pd.DataFrame
        Must contain `seq`.
    img_df : pd.DataFrame
        Must contain `FileName`. Will be sorted and assigned a new `seq` column.

    Returns
    -------
    pd.DataFrame
        Merged exposure metadata table with MRK + image fields.
    """
    img_df = img_df.sort_values("FileName").reset_index(drop=True)
    img_df["seq"] = img_df.index + 1  # 1-based

    # merge
    exposure_meta_df = pd.merge(mrk_df, img_df, on="seq", how="inner", suffixes=("_mrk", "_xml"))

    # check
    n_mrk = len(mrk_df)
    n_img = len(img_df)
    n_matched = len(exposure_meta_df)

    if n_matched != n_mrk or n_matched != n_img:
        logger.warning(f"Match count mismatch: MRK={n_mrk}, XML={n_img}, Matched={n_matched}")
    else:
        logger.info(f"Matched {n_matched} records (MRK <-> XML)")

    return exposure_meta_df


def interpolate_pos_at_exposure(
    pos_df: pd.DataFrame,
    exposure_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Interpolate rover PPK antenna ECEF positions to exposure epochs.

    Position interpolation:
    - Uses linear interpolation for X/Y/Z over GPS time-of-week (seconds).

    Covariance / sigma handling:
    - Covariance is NOT interpolated.
    - For exposures inside the PPK time span, covariance/sigma are copied from the
      nearest PPK epoch (nearest-neighbor assignment).

    Out-of-coverage handling:
    - If an exposure epoch lies outside the PPK trajectory time range, this function
      sets X/Y/Z and covariance/sigma fields to NaN to prevent accidental use of
      endpoint-extrapolated values.

    Parameters
    ----------
    pos_df : pd.DataFrame
        PPK trajectory with at least:
        - GPS_time, X, Y, Z
        - cov_total_ECEF, sigma_total_ECEF, sigma_total_ENU
        - coord_sys (string)
    exposure_df : pd.DataFrame
        Exposure table with:
        - GPS_time (from MRK)

    Returns
    -------
    pd.DataFrame
        `exposure_df` with added columns:
        - X, Y, Z (interpolated antenna ECEF, metres)
        - cov_total_ECEF (3x3, object dtype), sigma_total_ECEF (len-3), sigma_total_ENU (len-3)
        - coord_sys (copied from pos_df first record)
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
        logger.warning(
                       f"{n_outside} exposure epochs outside PPK trajectory range "
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

    # Propagate coordinate system label from PPK trajectory
    exposure_df["coord_sys"] = pos_df["coord_sys"].iloc[0]

    logger.info(f"Interpolated PPK position for {len(exposure_df)} exposure epochs")
    return exposure_df

def apply_gimbal_correction(exposure_df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply lever-arm correction (ECEF) to shift antenna phase center -> camera center.

    The lever-arm vector (gimbal_dX, gimbal_dY, gimbal_dZ) must already be in ECEF metres,
    typically converted from DJI MRK NED offsets using per-epoch lat/lon.

        cam_ecef = antenna_ecef + leverarm_ecef

    Parameters
    ----------
    exposure_df : pd.DataFrame
        Must contain:
        - X, Y, Z (antenna ECEF, metres)
        - gimbal_dX, gimbal_dY, gimbal_dZ (lever-arm ECEF, metres)

    Returns
    -------
    pd.DataFrame
        Adds camera center fields:
        - cam_X, cam_Y, cam_Z (ECEF metres)
        - cam_lat, cam_lon (degrees), cam_h (ellipsoidal height, metres)

    Notes
    -----
    - If antenna coordinates are NaN (e.g., out-of-coverage exposures), camera outputs will also be NaN.
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

    logger.info(f"Gimbal correction applied for {len(exposure_df)} images")
    return exposure_df

def format_output(df: pd.DataFrame, full_output: bool = False) -> pd.DataFrame:
    """
    Format the exposure table into an output-ready DataFrame.

    Operations
    ----------
    1) Split `sigma_total_ENU` (array-like: [sE, sN, sU]) into three scalar columns:
       - sigma_E, sigma_N, sigma_U
       If `sigma_total_ENU` is missing or not array-like, NaN is assigned.
    2) Drop the original `sigma_total_ENU` column.
    3) If `full_output=False`, keep only a compact set of core columns for export.

    Parameters
    ----------
    df : pd.DataFrame
        Exposure table produced by the previous pipeline steps.
    full_output : bool, default False
        If True, return all columns (minus `sigma_total_ENU`).
        If False, return only a predefined subset of core columns.

    Returns
    -------
    pd.DataFrame
        Formatted output table.

    Raises
    ------
    KeyError
        If `full_output=False` and any required core columns are missing in `df`.
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