from pathlib import Path
import shutil
import datetime as dt
import pandas as pd
from pyproj import Transformer, CRS
import numpy as np
from numpy import sin, cos, arcsin, arctan2, sqrt, degrees, radians, sign, atan2, asin, pi


def clean_temp_dirs(
    temp_root: Path = Path("temp"),
    subfolders: list[str] = ["ephemeris", "ppk_result", "rinex_base", "rinex_rover"]
):
    for name in subfolders:
        subdir = temp_root / name
        if subdir.exists():
            try:
                shutil.rmtree(subdir)
                print(f"[INFO] Cleared: {subdir}")
            except Exception as e:
                print(f"[WARNING] Failed to clear {subdir}: {e}")

def utc_to_gps(dt_obj: dt.datetime):
    """
    Convert UTC datetime to:
    - Gregorian year
    - Day-of-year (DDD)
    - GPS week
    - GPS day (0=Sunday, ..., 6=Saturday)
    - GPS time-of-week (seconds)

    Returns:
        Tuple: (yyyy, ddd, gps_week, gps_day, gps_tow)
    """
    gps_start = dt.datetime(1980, 1, 6)
    delta = (dt_obj - gps_start).total_seconds()

    gps_week = int(delta // 604800)
    gps_tow = round(delta % 604800, 6)

    # Fix gps_day: shift weekday() to GPS format
    # datetime.weekday(): Mon=0 ... Sun=6
    # GPS: Sun=0 ... Sat=6
    gps_day = (dt_obj.weekday() + 1) % 7

    yyyy = dt_obj.year
    ddd = dt_obj.timetuple().tm_yday

    return yyyy, ddd, gps_week, gps_day, gps_tow


def vector_enu_to_ecef(lat: float, lon: float, dE: float, dN: float, dU: float) -> np.ndarray:
    """
    Converts a local correction vector from ENU (East-North-Up) to ECEF (Earth-Centered, Earth-Fixed).

    Parameters:
        lat -- geodetic latitude in radians
        lon -- geodetic longitude in radians
        dE  -- correction in East direction (meters)
        dN  -- correction in North direction (meters)
        dU  -- correction in Up direction (meters)

    Returns:
        (3, 1) numpy array -- correction vector in ECEF coordinates (ΔX, ΔY, ΔZ)
    """
    R = np.array([
        [-sin(lon),              cos(lon),             0],
        [-sin(lat)*cos(lon), -sin(lat)*sin(lon),  cos(lat)],
        [ cos(lat)*cos(lon),  cos(lat)*sin(lon),  sin(lat)]
    ])
    enu_vector = np.array([[dE], [dN], [dU]])
    ecef_vector = R.T @ enu_vector
    return ecef_vector



def covariance_ecef_to_enu(cov_ecef: np.ndarray, lon_deg: float, lat_deg: float) -> np.ndarray:
    lon_rad = np.radians(lon_deg)
    lat_rad = np.radians(lat_deg)
    R = np.array([
        [-np.sin(lon_rad),               np.cos(lon_rad),              0],
        [-np.sin(lat_rad)*np.cos(lon_rad), -np.sin(lat_rad)*np.sin(lon_rad), np.cos(lat_rad)],
        [ np.cos(lat_rad)*np.cos(lon_rad),  np.cos(lat_rad)*np.sin(lon_rad), np.sin(lat_rad)]
    ])
    return R @ cov_ecef @ R.T

def get_crs_igb20() -> CRS:
    """
    Returns a pyproj CRS object representing the IGb20 reference frame.
    Equivalent to EPSG:10783. https://epsg.io/10783

    Returns:
        pyproj.CRS: Ellipsoidal 3D geographic CRS for IGb20 (lat/lon/height).
    """
    return CRS.from_wkt("""
        GEOCCS["IGb20",
            DATUM["IGb20",
                SPHEROID["GRS 1980",6378137,298.257222101,
                    AUTHORITY["EPSG","7019"]],
                AUTHORITY["EPSG","1400"]],
            PRIMEM["Greenwich",0,
                AUTHORITY["EPSG","8901"]],
            UNIT["metre",1,
                AUTHORITY["EPSG","9001"]],
            AXIS["Geocentric X",OTHER],
            AXIS["Geocentric Y",OTHER],
            AXIS["Geocentric Z",NORTH],
            AUTHORITY["EPSG","10783"]]
    """)
def transform_coordinates(
    df: pd.DataFrame,
    source_crs,
    target_crs,
    x_col: str = "lon",
    y_col: str = "lat",
    z_col: str | None = None,
    out_x: str = "x_tgt",
    out_y: str = "y_tgt",
    out_z: str = "z_tgt",
    cov_ecef2enu: bool = False
) -> pd.DataFrame:
    """
    Transform coordinates from a source CRS to a target CRS.

    Parameters:
        df (pd.DataFrame): The input DataFrame containing coordinate columns.
        source_crs (int | str | pyproj.CRS): The source coordinate reference system (e.g., EPSG:4326, "EPSG:10784", CRS object).
        target_crs (int | str | pyproj.CRS): The target coordinate reference system (e.g., EPSG:32612 for UTM Zone 12N).
        x_col (str): The column name in `df` for the X coordinate in the source CRS (e.g., "lon" or "ECEF_X").
        y_col (str): The column name in `df` for the Y coordinate in the source CRS (e.g., "lat" or "ECEF_Y").
        z_col (str | None): Optional. Column name for Z coordinate (e.g., "height" or "ECEF_Z"). If None, only 2D transformation is performed.
        out_x (str): Output column name for transformed X (e.g., "E_UTM" or "x_tgt").
        out_y (str): Output column name for transformed Y (e.g., "N_UTM" or "y_tgt").
        out_z (str): Output column name for transformed Z (if z_col is provided).
        cov_ecef2enu (bool): If True, also transforms covariance matrix in 'cov_matrix' column to ENU.

    Returns:
        pd.DataFrame:
            A copy of the input DataFrame with added transformed coordinate columns:
            - out_x, out_y (always)
            - out_z (only if z_col is provided)
            - sd_E, sd_N, sd_U (if cov_ecef2enu is True and 'cov_matrix' exists)
    """
    from pyproj import CRS, Transformer

    src = CRS.from_user_input(source_crs)
    tgt = CRS.from_user_input(target_crs)
    transformer = Transformer.from_crs(src, tgt, always_xy=True)

    if z_col and z_col in df.columns:
        x_t, y_t, z_t = transformer.transform(
            df[x_col].values,
            df[y_col].values,
            df[z_col].values
        )
        df[out_x] = x_t
        df[out_y] = y_t
        df[out_z] = z_t
    else:
        x_t, y_t = transformer.transform(
            df[x_col].values,
            df[y_col].values
        )
        df[out_x] = x_t
        df[out_y] = y_t

    if cov_ecef2enu and 'cov_matrix' in df.columns and 'lat' in df.columns and 'lon' in df.columns:
        sd_E, sd_N, sd_U = [], [], []
        for _, row in df.iterrows():
            cov = np.array(eval(row['cov_matrix']))
            enu_cov = covariance_ecef_to_enu(cov, row['lon'], row['lat'])
            sd_E.append(np.sqrt(enu_cov[0, 0]))
            sd_N.append(np.sqrt(enu_cov[1, 1]))
            sd_U.append(np.sqrt(enu_cov[2, 2]))
        df['sd_E'] = sd_E
        df['sd_N'] = sd_N
        df['sd_U'] = sd_U

    return df