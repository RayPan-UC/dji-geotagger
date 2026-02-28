from pathlib import Path
import datetime as dt
import pandas as pd
import numpy as np
from PIL import Image
from tqdm import tqdm
from dji_geotagger.tools.tools import utc2gps

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
    roll = np.radians(roll)
    pitch = np.radians(pitch)
    yaw = np.radians(yaw)

    if abs(pitch + np.pi / 2) < np.radians(1.0):  # pitch ≈ -90°
        if abs(abs(roll) - np.pi) < np.radians(1.0):  # roll ≈ ±180°
            roll = 0.0  # roll flip: set to 0
        yaw = (yaw + np.pi) % (2 * np.pi)  # yaw flip but we will use flight yaw for final out put
    return np.degrees(roll), np.degrees(pitch), np.degrees(yaw)

def combine_all_img_info(
    photo_folder: Path,
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
    """
    metadata_list = []
    with ExifToolHelper(executable=exiftool_path) as et:
        for img_path in tqdm(image_list, desc="[INFO] Gathering image metadata (EXIF/XMP)"):
            metadata = et.get_metadata(str(img_path))
            metadata_list.extend(metadata)
    """
    # Check dependency
    try:
        import defusedxml
    except ImportError:
        raise ImportError("[ERROR] Missing dependency: defusedxml. Please install with `pip install defusedxml`")

    records = []
    for img_path in tqdm(image_list, desc="[INFO] Gathering image metadata (EXIF/XMP via Pillow)"):
        try:
            with Image.open(img_path) as im:
                exif_data = im.getexif()
                xmp_data = im.getxmp()

            if xmp_data is None:
                raise ValueError("No XMP metadata found; ")
            
            desc = xmp_data['xmpmeta']['RDF']['Description']

            utc_str = desc["UTCAtExposure"]
            dt_obj = dt.datetime.strptime(utc_str, "%Y-%m-%dT%H:%M:%S.%f")

            # Convert UTC → GPS time
            yyyy, doy, gps_week, gps_day, gps_tow = utc_to_gps(dt_obj)

            # Raw flight attitude (aircraft)
            flight_roll  = float(desc["FlightRollDegree"])
            flight_pitch = float(desc["FlightPitchDegree"])
            flight_yaw   = float(desc["FlightYawDegree"])

            # Gimbal attitude (camera)
            gimbal_roll  = float(desc["GimbalRollDegree"])
            gimbal_pitch = float(desc["GimbalPitchDegree"])
            gimbal_yaw   = float(desc["GimbalYawDegree"])

            # correct DJI gimbal lock problem
            roll, pitch, yaw = correct_dji_gimbal_lock(gimbal_roll, gimbal_pitch, gimbal_yaw)

            # Correct DJI-style pitch/roll/yaw for photogrammetry (nadir = pitch +90°)
            pitch = pitch + 90  # DJI defines -90° as nadir
            yaw = flight_yaw    # DJI flight yaw is typically correct



            records.append({
                "FileName":               img_path.name,
                "UTCAtExposure":          utc_str,
                "GPS_week":               gps_week,
                "GPS_time":               gps_tow,
                "GPSLatitude":            float(desc["GpsLatitude"]),
                "GPSLongitude":           float(desc["GpsLongitude"]),
                "AbsoluteAltitude":       float(desc["AbsoluteAltitude"]),
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
            print(f"[WARNING] Skipped {img_path.name} due to error: {e}")
            continue

    df = pd.DataFrame(records)
    print(f"[INFO] Parsed {len(df)} image records.")
    return df