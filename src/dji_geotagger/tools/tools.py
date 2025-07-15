from pathlib import Path
import shutil
import datetime as dt
import pandas as pd
from pyproj import Transformer, CRS
import numpy as np
from numpy import sin, cos, arcsin, arctan2, sqrt, degrees, radians, sign, atan2, asin, pi

def get_library_root_path() -> Path:
    root_path = Path(__file__).resolve().parent.parent
    return root_path

def get_rtklib_executable(tool_name: str) -> Path:
    """
    Returns the full path to a RTKLIB tool (e.g. convbin, rnx2rtkp) in the library's RTKLIB/bin folder.
    """
    return get_library_root_path() / "tools" / "RTKLIB" / "bin" / f"{tool_name}.exe"

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

def flatten_matrix(matrix) -> str:
    """
    Flatten a 3x3 matrix or 9-element list into a space-separated string.
    """
    arr = np.array(matrix).flatten()
    return " ".join(f"{v:.8f}" for v in arr)

def unflatten_matrix(text: str) -> np.ndarray:
    """
    Parse a space-separated 9-element string into a 3x3 numpy array.
    """
    values = [float(v) for v in text.strip().split()]
    if len(values) != 9:
        raise ValueError("Input string must contain exactly 9 float values.")
    return np.array(values).reshape(3, 3)

def transform_coordinates(
    df: pd.DataFrame,
    target_crs,
    source_crs = get_crs_igb20(),
    x_col: str = "x_ecef",
    y_col: str = "y_ecef",
    z_col: str = "z_ecef",
    out_x: str = "x_tgt",
    out_y: str = "y_tgt",
    out_z: str = "z_tgt",
    cov_ecef2enu: bool = False,
    drop_original: bool = False
) -> pd.DataFrame:
    """
    Transform coordinates from a source CRS to a target CRS and optionally convert ECEF covariance to ENU standard deviations.

    This function transforms 3D coordinates (typically from ECEF) into a target CRS (e.g., UTM).
    If `cov_ecef2enu` is True and the input DataFrame contains a flattened ECEF covariance matrix (`cov_ecef_flat`),
    it will also compute ENU-direction standard deviations using per-point latitude/longitude converted from ECEF.

    Parameters
    ----------
    df : pd.DataFrame
        Input DataFrame containing coordinates and optionally covariance data.
    source_crs : int, str, or pyproj.CRS
        Source coordinate reference system (e.g., 4978 for ECEF).
    target_crs : int, str, or pyproj.CRS
        Target coordinate reference system (e.g., 32612 for UTM Zone 12N).
    x_col : str, default "x_ecef"
        Column name for X-coordinate in source CRS.
    y_col : str, default "y_ecef"
        Column name for Y-coordinate in source CRS.
    z_col : str, default "z_ecef"
        Column name for Z-coordinate in source CRS.
    out_x : str, default "x_tgt"
        Output column name for transformed X.
    out_y : str, default "y_tgt"
        Output column name for transformed Y.
    out_z : str, default "z_tgt"
        Output column name for transformed Z.
    cov_ecef2enu : bool, default False
        Whether to convert ECEF covariance to ENU standard deviations (`sd_E`, `sd_N`, `sd_U`).
        Requires a `cov_ecef_flat` column (list or string of 9 elements representing 3×3 ECEF covariance).

    Returns
    -------
    pd.DataFrame
        DataFrame with additional columns:
        - out_x, out_y, (and out_z if z_col is provided): Transformed coordinates in target CRS.
        - sd_E, sd_N, sd_U (if cov_ecef2enu is True): ENU standard deviations derived from ECEF covariance.
    """
    src = CRS.from_user_input(source_crs)
    tgt = CRS.from_user_input(target_crs)
    transformer = Transformer.from_crs(src, tgt, always_xy=True)

    # Transform coordinates
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

    # Covariance transformation (ECEF → ENU)
    if cov_ecef2enu and 'cov_ecef_flat' in df.columns:
        sd_E, sd_N, sd_U = [], [], []

        #  ECEF → LLH 
        transformer_llh = Transformer.from_crs("EPSG:4978", "EPSG:4326", always_xy=True)

        for _, row in df.iterrows():
            # cov_flat to 3x3 matrix
            cov_flat = row['cov_ecef_flat']
            cov_ecef_matrix = unflatten_matrix(cov_flat)

            # transfer to LLH
            lon, lat, _ = transformer_llh.transform(
                row[x_col], row[y_col], row[z_col] if z_col else 0.0
            )

            # cov ecef to ENU
            enu_cov = covariance_ecef_to_enu(cov_ecef_matrix, lon, lat)
            cov_enu_flat = flatten_matrix(enu_cov)
            sd_E.append(np.sqrt(enu_cov[0, 0]))
            sd_N.append(np.sqrt(enu_cov[1, 1]))
            sd_U.append(np.sqrt(enu_cov[2, 2]))

        df['sd_E'] = sd_E
        df['sd_N'] = sd_N
        df['sd_U'] = sd_U
        df["cov_enu_flat"] = cov_enu_flat

        if drop_original:
            df = df.drop(columns=["x_ecef", "y_ecef", "z_ecef", "sd_x_ecef", "sd_y_ecef", "sd_z_ecef", "cov_ecef_flat"], errors="ignore")
            
    return df