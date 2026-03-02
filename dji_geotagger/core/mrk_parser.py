import pandas as pd
import numpy as np
from pathlib import Path
from dji_geotagger.tools.tools import NED2ECEF_vec


def mrk2df(mrk_file: str) -> pd.DataFrame:
    """
    Parse a DJI .MRK file into a cleaned, geodetically consistent DataFrame.

    The .MRK file stores GNSS position and antenna-to-camera lever-arm offsets
    recorded at each exposure epoch. This function performs:

        1) Reading raw tab-separated MRK records
        2) Cleaning string fields (removing suffix tags such as ",Lat", ",Lon")
        3) Unit conversion (lever-arm offsets: millimetres → metres)
        4) Converting lever-arm vectors from local NED to global ECEF
        5) Mapping RTK quality flags to human-readable solution status

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

    -------------------------------------------------------------------------
    Output DataFrame Columns
    -------------------------------------------------------------------------
    seq              : image sequence index
    GPS_time         : GPS time-of-week (seconds)
    GPS_week         : GPS week number (int)
    lat, lon         : geodetic coordinates (degrees)
    ellh             : ellipsoidal height (metres)
    gimbal_dN/E/D    : lever-arm offsets in local NED frame (metres)
    gimbal_dX/Y/Z    : lever-arm offsets in ECEF frame (metres)
    rtk_status       : RTK solution type (Single / Float / Fixed / Unknown)

    Notes
    -----
    - NED uses positive Down (not Up).
    - ECEF conversion is performed per epoch.
    - The function assumes WGS84-compatible geodetic coordinates.
    - Any parsing failure raises RuntimeError.

    Returns
    -------
    pandas.DataFrame
        Cleaned MRK data ready for PPK interpolation and
        camera center correction workflows.
    """
    # Input
    mrk_file = Path(mrk_file)

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
        df['gimbal_dN'] = df['gimbal_dN'].str.strip().str.split(',').str[0].astype(int) * 0.001  # mm → m
        df['gimbal_dE'] = df['gimbal_dE'].str.strip().str.split(',').str[0].astype(int) * 0.001  # mm → m
        df['gimbal_dD'] = df['gimbal_dD'].str.strip().str.split(',').str[0].astype(int) * 0.001  # mm → m
        df['rtk_flag']  = df['rtk_flag'].str.strip().str.split(',').str[0].astype(int)

        # level arm -> ECEF
        ecef_vecs = np.array([
            NED2ECEF_vec(row.gimbal_dN, row.gimbal_dE, row.gimbal_dD, row.lat, row.lon)
            for _, row in df.iterrows()
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
    
    print(f"[INFO] Parsed {mrk_file.stem}.mrk ({len(df)} records)")
    return df