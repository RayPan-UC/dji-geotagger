import pandas as pd
import numpy as np
import pymap3d as pm
from dji_geotagger.tools.logging_setup import get_logger

logger = get_logger(__name__)


def compute_camera_position(
    mrk_df: pd.DataFrame,
    img_df: pd.DataFrame,
    pos_df: pd.DataFrame,
    full_output: bool = False,
    max_gap_s: float | None = None,
) -> pd.DataFrame:
    """
    Compute corrected camera center positions for each exposure epoch.

    This function builds an exposure-level table by:
      1) `match_mrk_xml`              : merge MRK records and image metadata by exposure time
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
    exposure_df = interpolate_pos_at_exposure(pos_df, exposure_df,
                                              max_gap_s=max_gap_s)

    # Step 3
    exposure_df = apply_gimbal_correction(exposure_df)

    # Step 4
    exposure_df = format_output(exposure_df, full_output=full_output)

    logger.info(f"Camera position computed for {len(exposure_df)} images")
    return exposure_df


def _sequence_from_names(names: pd.Series) -> pd.Series:
    """
    DJI's own exposure number for each image, read from the file name.

    The name is ``DJI_<14-digit timestamp>_<4-digit exposure>[_<band>].<ext>``,
    and it is the four-digit field that the MRK's first column counts. Taken
    together they pair the two tables without assuming either is complete or
    starts at one.

    Returns None if any name does not carry the field, leaving the caller to
    pair on the exposure time instead - which is the only thing left to go on
    once the names have been changed.
    """
    numbers = names.str.extract(r"_(\d{14})_(\d{4})(?:\D|$)")[1]
    if numbers.isna().any():
        return None
    return numbers.astype(int)


# The check below allows this share of one exposure interval before it calls a
# pairing wrong. Off by a single record puts a photo a whole interval from its
# MRK time, so anything under half is unambiguous; the rest of the margin
# absorbs whatever slop another payload's XMP may carry.
#
# Measured here across 1,998 exposures from a P1 and an L2, the XMP timestamp
# and the MRK agree to a microsecond - DJI writes the same instant into both.
# That is one aircraft and one firmware generation, which is why the tolerance
# is derived from the data rather than fixed at what this hardware happens to
# achieve.
_EXPOSURE_TOLERANCE_SHARE = 0.4

# Floor and ceiling, for a burst so fast that 40% of it is noise, and for an
# interval so long that 40% of it would let a real mispairing through.
_EXPOSURE_TOLERANCE_MIN_S = 0.05
_EXPOSURE_TOLERANCE_MAX_S = 2.0


def _mrk_seconds(df: pd.DataFrame) -> pd.Series:
    """GPS week and time of week as one continuous scale."""
    return (df["GPS_week"].astype(float) * 604800.0
            + df["GPS_time"].astype(float))


def _xmp_seconds(img_df: pd.DataFrame) -> pd.Series | None:
    """
    The images' own exposure instants, on the same scale as `_mrk_seconds`.

    DJI's ``UTCAtExposure`` XMP field holds GPS time despite its name - it
    equals the MRK's week and time of week exactly, to a microsecond - so it
    lands on that scale with no leap second applied and nothing to convert.
    See `docs/output.md`.

    Returns None when the field is absent or unreadable, which costs the
    cross-check and nothing else.
    """
    if "UTCAtExposure" not in img_df.columns:
        return None
    stamps = pd.to_datetime(img_df["UTCAtExposure"], errors="coerce")
    if stamps.isna().any():
        return None
    return (stamps - pd.Timestamp("1980-01-06")).dt.total_seconds()


def _match_on_time(mrk_df: pd.DataFrame, img_df: pd.DataFrame) -> pd.DataFrame:
    """
    Pair each photo with the MRK record fired at the same instant.

    Time is what the two files genuinely have in common: the MRK carries GPS
    week and time of week, and DJI writes the same instant into the image's
    ``UTCAtExposure`` XMP field. Neither has to be complete, numbered from
    one, or still under its original file name.

    Nearest match within `_exposure_tolerance`, so a photo whose instant sits
    between two records - or outside the MRK altogether - is dropped rather
    than attached to whichever was closest.

    Falls back to DJI's exposure number when the images carry no readable
    timestamp, which is the only other thing the two files share.
    """
    times = _xmp_seconds(img_df)
    if times is None:
        numbers = _sequence_from_names(img_df["FileName"])
        if numbers is None:
            raise ValueError(
                "[ERROR] The images carry neither a readable UTCAtExposure "
                "nor a DJI exposure number in their file names, so they "
                "cannot be paired with the MRK.")
        logger.info("No readable image timestamps; pairing with the MRK by "
                    "DJI exposure number instead.")
        img_df = img_df.assign(seq=numbers)
        return pd.merge(mrk_df, img_df, on="seq", how="inner",
                        suffixes=("_mrk", "_xml"))

    left = img_df.assign(_t=times.values).sort_values("_t")
    right = mrk_df.assign(_t=_mrk_seconds(mrk_df).values).sort_values("_t")
    tolerance = _exposure_tolerance(right["_t"])

    paired = pd.merge_asof(left, right, on="_t", direction="nearest",
                           tolerance=tolerance, suffixes=("_xml", "_mrk"))
    lost = int(paired["seq"].isna().sum()) if "seq" in paired else 0
    if lost:
        logger.warning(
            "[WARN] %d of %d photos have no MRK record within %.3f s of their "
            "own timestamp and were dropped.", lost, len(paired), tolerance)
    return paired.dropna(subset=["seq"]).drop(columns="_t").reset_index(
        drop=True)


def _check_pairing_numbers(paired: pd.DataFrame) -> None:
    """
    Confirm DJI's own exposure numbers agree with what the times paired up.

    Time is the physical truth and is what the join uses, but it is matched
    within a tolerance, so a payload whose XMP is offset by a constant would
    be paired confidently and wrongly - every photo shifted by the same
    number of records. The file name carries DJI's own count for the same
    exposure, and it is exact, so the two disagree the moment that happens.
    """
    if "seq" not in paired.columns or "FileName" not in paired.columns:
        return
    numbers = _sequence_from_names(paired["FileName"])
    if numbers is None:
        return                      # renamed images: nothing to check against

    off = numbers.values != paired["seq"].values
    if not off.any():
        return

    names = paired.loc[off, "FileName"].head(3).tolist()
    logger.warning(
        "[WARN] %d of %d photos were paired by time with an MRK record whose "
        "exposure number does not match their own: %s. If the whole flight is "
        "affected, the camera's timestamps are offset against the MRK and the "
        "positions will be shifted by whole exposures.",
        int(off.sum()), len(paired), ", ".join(names))


def _exposure_tolerance(stamps: pd.Series) -> float:
    """
    How far a photo's timestamp may sit from its MRK record, from the interval
    the flight was actually shot at.

    A fixed figure cannot serve both a 0.6 s survey interval and a slow
    inspection run: tight enough for the first is a false alarm on the second,
    loose enough for the second lets an off-by-one through the first.
    """
    intervals = np.diff(np.sort(stamps.values))
    intervals = intervals[intervals > 0]
    if intervals.size == 0:
        return _EXPOSURE_TOLERANCE_MAX_S
    return float(np.clip(_EXPOSURE_TOLERANCE_SHARE * np.median(intervals),
                         _EXPOSURE_TOLERANCE_MIN_S,
                         _EXPOSURE_TOLERANCE_MAX_S))


def match_mrk_xml(mrk_df: pd.DataFrame, img_df: pd.DataFrame) -> pd.DataFrame:
    """
    Merge MRK records with image metadata by the instant each was exposed.

    Notes
    -----
    - The MRK carries GPS week and time of week; DJI writes the same instant
      into the image's ``UTCAtExposure`` XMP field, to a microsecond across
      the flights measured here. Time survives renamed files, a folder that
      does not start at 0001, and a count that differs on the two sides.
    - Matched nearest within `_exposure_tolerance`, which is derived from the
      flight's own exposure interval. A photo with no record inside it is
      dropped and counted, not attached to whichever was closest.
    - DJI's exposure number takes no part in the pairing but is compared
      afterwards by `_check_pairing_numbers`, as an exact witness against a
      camera clock offset from the MRK.
    - Two fallbacks: images with no readable timestamp are paired on that
      exposure number; images with neither are refused. Sorted position is
      not one of them - it always appears to succeed.

    Parameters
    ----------
    mrk_df : pd.DataFrame
        Must contain `seq`, `GPS_week` and `GPS_time`.
    img_df : pd.DataFrame
        Must contain `FileName`, and `UTCAtExposure` for the usual path.

    Returns
    -------
    pd.DataFrame
        Merged exposure metadata table with MRK + image fields.
    """
    img_df = img_df.sort_values("FileName").reset_index(drop=True)
    exposure_meta_df = _match_on_time(mrk_df, img_df)
    _check_pairing_numbers(exposure_meta_df)

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
    max_gap_s: float | None = None,
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

    # A trajectory assembled from several flights is continuous in index but
    # not in time: between two folders of the same flight the join is a
    # fraction of a second, but between two missions it can be an hour. Being
    # inside the overall span is therefore no longer sufficient - without this,
    # an exposure sitting in a real gap would be interpolated straight across
    # it and come back as a confident position that was never observed.
    if max_gap_s is not None and len(pos_t) > 1:
        right = np.searchsorted(pos_t, exp_t, side="left")
        left = np.clip(right - 1, 0, len(pos_t) - 1)
        right = np.clip(right, 0, len(pos_t) - 1)
        spanned = pos_t[right] - pos_t[left]
        in_a_gap = (spanned > max_gap_s) & ~outside_mask
        if in_a_gap.any():
            logger.warning(
                f"{int(in_a_gap.sum())} exposure(s) fall in a trajectory gap "
                f"longer than {max_gap_s:g} s and were not interpolated.")
            outside_mask = outside_mask | in_a_gap

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
    # Provenance of the reference frame. Needed by transform_coordinates(),
    # and worth keeping in the output so a result can be traced later.
    for col in ("epoch", "epoch_decimal_year", "base_source"):
        if col in pos_df.columns:
            exposure_df[col] = pos_df[col].iloc[0]

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
            'FileName', 'UTCAtExposure', 'coord_sys', 'epoch',
            'cam_lat', 'cam_lon', 'cam_h',
            'cam_X', 'cam_Y', 'cam_Z',
            'sigma_E', 'sigma_N', 'sigma_U',
            'DGT_YawDegree', 'DGT_PitchDegree', 'DGT_RollDegree',
            'rtk_status'
        ]
        df = df[keep_cols]

    return df