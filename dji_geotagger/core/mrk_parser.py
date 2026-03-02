import pandas as pd
import numpy as np
from pathlib import Path
from dji_geotagger.tools.tools import NED2ECEF_vec


def mrk2df(mrk_file: str | Path) -> pd.DataFrame:
    """
    Parse a DJI *.MRK file into a cleaned, geodetically usable DataFrame.

    The DJI MRK file stores, per exposure epoch:
    - an exposure sequence index (`seq`)
    - GNSS time (GPS week + time-of-week)
    - a lever-arm offset from antenna phase center to camera CMOS center (in local NED, millimetres)
    - camera geodetic position (lat/lon/ellh; often with DJI suffix tags)
    - RTK quality flag

    This function performs:
      1) Read raw tab-separated MRK records (no header).
      2) Clean string fields by taking the token before the first comma
         (e.g., "55.123,Lat" -> "55.123") and stripping bracket tags for GPS_week.
      3) Convert lever-arm offsets from millimetres to metres.
      4) Convert lever-arm vectors from local NED (N,E,D; D is positive Down) to ECEF (dX,dY,dZ)
         using per-epoch latitude/longitude.
      5) Map RTK flag codes into a human-readable solution status.

    Parameters
    ----------
    mrk_file : str | Path
        Path to a DJI MRK file. The file is expected to be tab-separated and follow DJI's standard layout.

    Returns
    -------
    pd.DataFrame
        Cleaned MRK table with at least the following columns:
        - seq (int)
        - GPS_time (float; GPS time-of-week seconds)
        - GPS_week (int)
        - lat, lon (float; degrees)
        - ellh (float; metres)
        - gimbal_dN, gimbal_dE, gimbal_dD (float; metres; local NED, D is Down)
        - gimbal_dX, gimbal_dY, gimbal_dZ (float; metres; ECEF lever-arm)
        - rtk_status (str; Single / Float / Fixed / Unknown)
        - stddev (raw field from MRK; currently preserved as-is and not decomposed into N/E/D components)

    Notes
    -----
    - DJI lever-arm is expressed in local NED with D = Down (positive downward).
    - ECEF lever-arm conversion is performed per record using that record's lat/lon.
    - RTK flag mapping:
        0 or 16 -> Single
        34      -> Float
        50      -> Fixed
        otherwise -> Unknown
    - Any parsing failure raises RuntimeError with the original exception message.

    -------------------------------------------------------------------------
    Raw .MRK Columns (DJI Standard Layout)
    -------------------------------------------------------------------------
    1   Sequence            : Image index
    2   GPS Time (TOW)      : GPS time-of-week at exposure (seconds)
    3   GPS Week            : GPS week number
    4   North Offset (mm)   : Antenna phase center → CMOS center (North)
    5   East Offset (mm)    : Antenna phase center → CMOS center (East)
    6   Down Offset (mm)    : Antenna phase center → CMOS center (Down)
    7   Latitude (deg)      : CMOS center latitude (may include suffix text)
    8   Longitude (deg)     : CMOS center longitude (may include suffix text)
    9   Ellipsoid Height    : Ellipsoidal height in metres (may include suffix)
    10  StdDev N            : North position standard deviation (m)
    11  StdDev E            : East position standard deviation (m)
    12  StdDev D            : Down position standard deviation (m)
    13  RTK Flag            : RTK solution quality indicator

    Note: DJI MRK "StdDev" fields can vary by firmware/model. This function currently
    preserves the raw `stddev` field as-is and does not decompose N/E/D stddev terms.

    -------------------------------------------------------------------------
    Coordinate Frame Definition
    -------------------------------------------------------------------------
    Lever-arm offsets in DJI .MRK are expressed in a local NED frame:

        N (North)  : positive toward geographic north
        E (East)   : positive toward geographic east
        D (Down)   : positive downward (opposite of Up)

    This NED frame is a local tangent coordinate system whose orientation
    depends on each epoch's geodetic latitude and longitude.

    For integration with PPK outputs (typically provided in ECEF),
    the lever-arm vectors (dN, dE, dD) are converted to ECEF components
    (dX, dY, dZ) using the corresponding (lat, lon) per record.

    This allows direct vector combination:

        camera_center_ecef = antenna_ecef + leverarm_ecef

    -------------------------------------------------------------------------
    RTK Flag Mapping
    -------------------------------------------------------------------------
        0 or 16   → Single
        34        → Float
        50        → Fixed
        otherwise → Unknown
    """
    # Input
    mrk_file = Path(mrk_file)
    if not mrk_file.exists():
        raise FileNotFoundError(f"[ERROR] MRK file not found: {mrk_file}")
    # Parse file
    df = pd.read_csv(mrk_file, sep='\t', header=None)

    # columns
    df.columns = ['seq', 'GPS_time', 'GPS_week', 'gimbal_dN', 'gimbal_dE', 'gimbal_dD',
                   'lat', 'lon', 'ellh', 'stddev', 'rtk_flag']

    try:
        # clean columns (strip, split, convert type)
        df['GPS_week']  = df['GPS_week'].str.strip('[]').astype(int)
        df['lat']       = df['lat'].str.strip().str.split(',').str[0].astype(float)
        df['lon']       = df['lon'].str.strip().str.split(',').str[0].astype(float)
        df['ellh']      = df['ellh'].str.strip().str.split(',').str[0].astype(float)
        df['gimbal_dN'] = df['gimbal_dN'].str.strip().str.split(',').str[0].astype(float) * 0.001  # mm → m
        df['gimbal_dE'] = df['gimbal_dE'].str.strip().str.split(',').str[0].astype(float) * 0.001  # mm → m
        df['gimbal_dD'] = df['gimbal_dD'].str.strip().str.split(',').str[0].astype(float) * 0.001  # mm → m
        df['rtk_flag']  = df['rtk_flag'].str.strip().str.split(',').str[0].astype(int)

        # level arm -> ECEF
        ecef_vecs = np.array([
            NED2ECEF_vec(n, e, d, lat, lon)
            for n, e, d, lat, lon in zip(
                df['gimbal_dN'], df['gimbal_dE'], df['gimbal_dD'],
                df['lat'], df['lon']
            )
        ])

        df['gimbal_dX'] = ecef_vecs[:, 0]
        df['gimbal_dY'] = ecef_vecs[:, 1]
        df['gimbal_dZ'] = ecef_vecs[:, 2]

        # map RTK flag to status 
        rtk_map = {0: 'Single', 16: 'Single', 34: 'Float', 50: 'Fixed'}
        df['rtk_status'] = df['rtk_flag'].map(rtk_map).fillna('Unknown')
        df = df.drop(columns=['rtk_flag'])
        

    except Exception as e:
        raise RuntimeError(f"[ERROR] Failed to parse .MRK file: {mrk_file}. {e}")
    
    print(f"[INFO] Parsed {mrk_file.name} ({len(df)} records)")
    return df