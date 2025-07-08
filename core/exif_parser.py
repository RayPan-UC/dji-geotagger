from pathlib import Path
import datetime as dt
import pandas as pd
import numpy as np
from exiftool import ExifToolHelper
from tqdm import tqdm
from tools.tools import utc_to_gps

def correct_dji_gimbal_lock(roll: float, pitch: float, yaw: float) -> tuple[float, float, float]:
    """
    Corrects DJI gimbal lock issue that causes roll flipping when pitch ≈ ±90°.

    Parameters:
        roll  -- camera roll angle in radians
        pitch -- camera pitch angle in radians
        yaw   -- camera yaw angle in radians

    Returns:
        (roll, pitch, yaw) -- corrected angles in radians
    """
    if abs(pitch) >= np.pi / 2 - 0.05:
        roll = -roll
        yaw = (yaw + np.pi) % (2 * np.pi)  # Wrap to [0, 2π]
    return roll, pitch, yaw

def combine_all_img_info(
    photo_folder: Path,
    exiftool_path: Path = Path(r"tools\exiftool-13.31_64\exiftool(-k).exe")
) -> pd.DataFrame:
    """
    Extracts image capture metadata from DJI images using ExifTool.

    Parameters:
        photo_folder  -- path to folder containing .JPG or .JPEG images
        exiftool_path -- path to ExifTool executable

    Returns:
        DataFrame with columns:
            - FileName: str
            - UTCAtExposure: str
            - GPS_week: int
            - GPS_time: float (seconds of week)
            - GPSLatitude, GPSLongitude: float
            - AbsoluteAltitude: float (meters)
            - flightRoll / Pitch / Yaw: float (deg)
            - gimbalRoll / Pitch / Yaw: float (deg)
            - roll / pitch / yaw: float (deg) [camera angles for photogrammetry]
    """
    
    image_list = list(photo_folder.rglob("*.JPG")) + list(photo_folder.rglob("*.JPEG"))
    image_list = sorted(image_list)
    print(f"[INFO] {len(image_list)} images were found in {photo_folder}")
    
    metadata_list = []
    with ExifToolHelper(executable=exiftool_path) as et:
        for img_path in tqdm(image_list, desc="[INFO] Gathering image metadata (EXIF/XMP)"):
            metadata = et.get_metadata(str(img_path))
            metadata_list.extend(metadata)

    records = []
    for metadata in metadata_list:
        try:
            # Parse UTC time string to datetime
            utc_str = metadata.get("XMP:UTCAtExposure")
            dt_obj = dt.datetime.strptime(utc_str, "%Y:%m:%d %H:%M:%S.%f")

            # Convert UTC → GPS time
            yyyy, doy, gps_week, gps_day, gps_tow = utc_to_gps(dt_obj)

            # Raw flight attitude (aircraft)
            flight_roll  = float(metadata.get("XMP:FlightRollDegree"))
            flight_pitch = float(metadata.get("XMP:FlightPitchDegree"))
            flight_yaw   = float(metadata.get("XMP:FlightYawDegree"))

            # Gimbal attitude (camera)
            gimbal_roll  = float(metadata.get("XMP:GimbalRollDegree"))
            gimbal_pitch = float(metadata.get("XMP:GimbalPitchDegree"))
            gimbal_yaw   = float(metadata.get("XMP:GimbalYawDegree"))

            # correct DJI gimbal lock problem
            roll, pitch, yaw = correct_dji_gimbal_lock(gimbal_roll, gimbal_pitch, gimbal_yaw)

            # Correct DJI-style pitch/roll/yaw for photogrammetry (nadir = pitch +90°)
            roll = roll
            pitch = pitch + 90  # DJI defines -90° as nadir
            yaw = yaw           # DJI yaw is typically correct



            records.append({
                "FileName":               metadata.get('File:FileName'),
                "UTCAtExposure":          utc_str,
                "GPS_week":               gps_week,
                "GPS_time":               gps_tow,
                "GPSLatitude":            float(metadata.get("XMP:GPSLatitude")),
                "GPSLongitude":           float(metadata.get("XMP:GPSLongitude")),
                "AbsoluteAltitude":       float(metadata.get("XMP:AbsoluteAltitude")),
                "flightRoll":             flight_roll,
                "flightPitch":            flight_pitch,
                "flightYaw":              flight_yaw,
                "gimbalRoll":             gimbal_roll,
                "gimbalPitch":            gimbal_pitch,
                "gimbalYaw":              gimbal_yaw,
                "dji_geotagger_roll":     roll,
                "dji_geotagger_pitch":    pitch,
                "dji_geotagger_yaw":      yaw,
            })

        except Exception as e:
            print(f"[WARNING] Skipped {metadata.get('File:FileName')} due to error: {e}")
            continue

    df = pd.DataFrame(records)
    print(f"[INFO] Parsed {len(df)} image records.")
    return df