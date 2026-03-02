from pathlib import Path
import datetime as dt
import pandas as pd
from pyproj import Transformer, CRS
import numpy as np
from numpy import sin, cos
from astropy.time import Time

def ECEF2ENU_vec(
        cov_ecef: np.ndarray, 
        lon_deg: float, 
        lat_deg: float
    ) -> np.ndarray:
    """
    Transform a 3×3 covariance matrix from ECEF to ENU (East-North-Up) frame.

    Uses the Jacobian rotation matrix to convert position uncertainty covariance
    from Earth-Centered, Earth-Fixed (ECEF) coordinates to local topocentric 
    East-North-Up (ENU) coordinates at a given geographic location.

    Parameters
    ----------
    cov_ecef : np.ndarray
        3×3 covariance matrix in ECEF frame (m²).
    lon_deg : float
        Geodetic longitude in decimal degrees.
    lat_deg : float
        Geodetic latitude in decimal degrees.

    Returns
    -------
    np.ndarray
        3×3 covariance matrix in ENU frame (m²).
        Diagonal elements [0,0], [1,1], [2,2] represent σE², σN², σU² respectively.

    Notes
    -----
    Transformation formula: Cov_ENU = R @ Cov_ECEF @ R^T
    where R is the rotation matrix from ECEF to ENU at (lat, lon).
    """
    lon_rad = np.radians(lon_deg)
    lat_rad = np.radians(lat_deg)
    R = np.array([
        [                -np.sin(lon_rad),                  np.cos(lon_rad),               0],
        [-np.sin(lat_rad)*np.cos(lon_rad), -np.sin(lat_rad)*np.sin(lon_rad), np.cos(lat_rad)],
        [ np.cos(lat_rad)*np.cos(lon_rad),  np.cos(lat_rad)*np.sin(lon_rad), np.sin(lat_rad)]
    ])
    return R @ cov_ecef @ R.T


def NED2ECEF_vec(dN, dE, dD, lat_deg, lon_deg) -> np.ndarray:
    """
    Convert a vector from local NED frame to ECEF frame (vector only, no translation).

    Performs rotation from North-East-Down (NED) local topocentric frame to Earth-Centered,
    Earth-Fixed (ECEF) frame. This is a pure rotation; no translation is applied.

    Parameters
    ----------
    dN : float | np.ndarray
        North component (metres).
    dE : float | np.ndarray
        East component (metres).
    dD : float | np.ndarray
        Down component (metres, positive downward).
    lat_deg : float
        Geodetic latitude in decimal degrees.
    lon_deg : float
        Geodetic longitude in decimal degrees.

    Returns
    -------
    np.ndarray
        3-element vector in ECEF coordinates (ΔX, ΔY, ΔZ) in metres.

    Notes
    -----
    NED Frame: North, East, Down (positive downward)
    ECEF Frame: X, Y, Z (Earth-centered, Earth-fixed)
    
    The transformation uses a rotation matrix that depends on local latitude and longitude.
    Assumes NED with D = Down (positive downward).
    """
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)

    R = np.array([
        [-np.sin(lat)*np.cos(lon), -np.sin(lon), -np.cos(lat)*np.cos(lon)],
        [-np.sin(lat)*np.sin(lon),  np.cos(lon), -np.cos(lat)*np.sin(lon)],
        [ np.cos(lat),              0.0,         -np.sin(lat)],
    ], dtype=float)

    vec_ned = np.array([dN, dE, dD], dtype=float)
    return R @ vec_ned


def utc2gps(dt_obj: dt.datetime) -> dict:
    """
    Convert UTC datetime to GPS time parameters.

    Converts a UTC datetime object to GPS week number, GPS day of week, and GPS time-of-week (TOW).
    Includes leap seconds in the conversion using astropy.

    Parameters
    ----------
    dt_obj : datetime.datetime
        UTC datetime object to convert.

    Returns
    -------
    dict
        Dictionary with GPS time components:
        
        - yyyy (int): Gregorian year
        - ddd (int): Day-of-year (1-366)
        - gps_week (int): GPS week number (since 1980-01-06)
        - gps_day (int): GPS day of week (0=Sunday, 6=Saturday)
        - gps_tow (float): GPS time-of-week in seconds (0-604800)

    Notes
    -----
    - GPS time includes leap seconds (uses astropy Time with scale='utc')
    - GPS week 0 started on 1980-01-06
    - GPS day numbering: Sunday=0, Monday=1, ..., Saturday=6
    - Datetime weekday() returns: Monday=0, ..., Sunday=6 (converted internally)
    """
    t = Time(dt_obj, scale="utc")
    delta = t.gps  # GPS seconds since 1980/1/6, leap seconds included

    gps_week = int(delta // 604800)
    gps_tow  = round(float(delta % 604800), 6)

    # datetime.weekday(): Mon=0 ... Sun=6
    # GPS day:            Sun=0 ... Sat=6
    gps_day = (dt_obj.weekday() + 1) % 7

    yyyy = dt_obj.year
    ddd  = dt_obj.timetuple().tm_yday

    return {
        "yyyy":     yyyy,
        "ddd":      ddd,
        "gps_week": gps_week,
        "gps_day":  gps_day,
        "gps_tow":  gps_tow,
    }
    

def vector_enu2ecef(lat_dd: float, lon_dd: float, dE: float, dN: float, dU: float) -> np.ndarray:
    """
    Convert a local correction vector from ENU to ECEF coordinates.

    Transforms a vector expressed in the local East-North-Up (ENU) topocentric frame
    to Earth-Centered, Earth-Fixed (ECEF) coordinates. This is a pure vector rotation
    with no translation.

    Parameters
    ----------
    lat_dd : float
        Geodetic latitude in decimal degrees.
    lon_dd : float
        Geodetic longitude in decimal degrees.
    dE : float | np.ndarray
        Correction in East direction (metres).
    dN : float | np.ndarray
        Correction in North direction (metres).
    dU : float | np.ndarray
        Correction in Up direction (metres).

    Returns
    -------
    np.ndarray
        (3, 1) numpy array with correction vector in ECEF (ΔX, ΔY, ΔZ) in metres.

    Notes
    -----
    The transformation uses the transpose of the rotation matrix from ECEF to ENU:
    ΔP_ECEF = R^T @ [dE, dN, dU]^T
    
    Coordinate Systems:
    - ENU: East (X), North (Y), Up (Z) in local tangent plane
    - ECEF: X, Y, Z in Earth-centered, Earth-fixed frame
    """
    lat = np.radians(lat_dd)
    lon = np.radians(lon_dd)

    R = np.array([
        [         -sin(lon),           cos(lon),         0],
        [-sin(lat)*cos(lon), -sin(lat)*sin(lon),  cos(lat)],
        [ cos(lat)*cos(lon),  cos(lat)*sin(lon),  sin(lat)]
    ])
    enu_vector = np.array([[dE], [dN], [dU]])
    ecef_vector = R.T @ enu_vector
    return ecef_vector