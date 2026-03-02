from pathlib import Path
import datetime as dt
import pandas as pd
import numpy as np
from PIL import Image
from tqdm import tqdm


def parse_img_info(
        img: str,
        add_format_orientation: bool = True
    ) -> dict:
    """
    Parse a single DJI image's XMP metadata into a flat dictionary.

    This function reads XMP (via Pillow's Image.getxmp()) and extracts:
      - exposure time (UTCAtExposure)
      - GNSS position (latitude, longitude, altitude)
      - flight attitude (FlightYaw/Pitch/Roll)
      - gimbal attitude (GimbalYaw/Pitch/Roll)

    Optionally, it also computes a "formatted" gimbal orientation (`DGT_*Degree`)
    to reduce DJI gimbal flip discontinuities (e.g., roll jumps between 0/180).

    Parameters:
    img:
        Path to the image file (.jpg/.tif).
    add_format_orientation:
        If True, add DGT_YawDegree / DGT_PitchDegree / DGT_RollDegree derived from
        gimbal angles by `_format_orientation()`.

    Returns:
    dict | None
        Parsed metadata dict. Returns None if:
          - no XMP metadata is found, or
          - any exception occurs during parsing.

    Notes:
    - This expects DJI-style XMP keys (e.g., "UTCAtExposure", "GimbalYawDegree").
      If the image is not from DJI or the schema differs, keys may be missing.
    """
    img = Path(img)

    try:
        with Image.open(img) as im:
            xmp_data = im.getxmp()
        if not xmp_data:
            print(f"[WARNING] No metadata found at {img}. Skipped")
            return
        desc = xmp_data['xmpmeta']['RDF']['Description']

        # Exposure time (UTC)
        utc_str = desc["UTCAtExposure"]
        # Raw flight attitude (aircraft)
        flight_roll_deg  = float(desc["FlightRollDegree"])
        flight_pitch_deg = float(desc["FlightPitchDegree"])
        flight_yaw_deg   = float(desc["FlightYawDegree"])
        # Gimbal attitude (camera)
        gimbal_roll_deg  = float(desc["GimbalRollDegree"])
        gimbal_pitch_deg = float(desc["GimbalPitchDegree"])
        gimbal_yaw_deg   = float(desc["GimbalYawDegree"])    
        # Optional: normalize gimbal orientation (reduce flip discontinuities)
        if add_format_orientation:
            fmt_ori = _format_orientation(gimbal_yaw_deg, gimbal_pitch_deg, gimbal_roll_deg)
          

        meta_dict = {
        "FileName":               img.name,
        "UTCAtExposure":          utc_str,
        "GPSLatitude":            float(desc["GpsLatitude"]),
        "GPSLongitude":           float(desc["GpsLongitude"]),
        "AbsoluteAltitude":       float(desc["AbsoluteAltitude"]),
        "FlightYawDegree":        flight_yaw_deg,
        "FlightPitchDegree":      flight_pitch_deg,
        "FlightRollDegree":       flight_roll_deg,
        "GimbalYawDegree":        gimbal_yaw_deg,
        "GimbalPitchDegree":      gimbal_pitch_deg,
        "GimbalRollDegree":       gimbal_roll_deg,
        }
        
    except Exception as e:
        print(f"[WARNING] Error occurred while parsing image's metadata. {e}")
        return


    return meta_dict | fmt_ori if fmt_ori else meta_dict

def parse_img_dir(
        img_dir: str,
        add_format_orientation: bool = True
    ) -> pd.DataFrame:

    img_dir = Path(img_dir)
    image_files = sorted(img_dir.glob("*.jpg")) + sorted(img_dir.glob("*.tif"))

    records = []
    for img in tqdm(image_files, desc="[INFO] Gathering image metadata (EXIF/XMP via Pillow)"):
        img_info = parse_img_info(img, add_format_orientation)
        if img_info is not None:
            records.append(img_info)

    # Save as Dataframe
    df = pd.DataFrame(records)
    print(f"[INFO] Parsed {len(image_files)} images ({len(df)} records)")
    return df



def _wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0

def _wrap360(a: float) -> float:
    return (a + 360.0) % 360.0


def _format_orientation(yaw: float, pitch: float, roll: float) -> tuple[float, float, float]:
    """
    Normalize DJI gimbal yaw/pitch/roll to a stable and photogrammetry-friendly form.

    Purpose:
    1. DJI gimbal often outputs roll ≈ 0° or 180°.
       A roll ≈ 180° indicates a 180° flipped solution caused by Euler angle
       non-uniqueness (gimbal lock / equivalent representation).

    2. Because Euler angles are not unique, the following solutions are equivalent:
           (ω, ϕ, κ)  ≡  (ω + 180°, ϕ, κ + 180°)

       Therefore, when |roll| > 90°, we absorb the 180° flip into yaw,
       and shift roll back toward 0°. This prevents large κ (kappa)
       discontinuities during OPK conversion.

    3. DJI pitch definition:
       - Nadir (camera pointing straight down) ≈ -90°
       - Photogrammetry convention expects nadir ≈ 0°

       Therefore, we apply:
           pitch_corrected = pitch + 90°

    Steps:
    A) Wrap yaw/pitch/roll into [-180°, 180°]
    B) If roll indicates flipped solution (|roll| > 90°):
           yaw  += 180°
           roll += 180°
       then wrap again
    C) Convert DJI pitch to photogrammetric pitch (nadir = 0°)
    D) Wrap yaw into [0°, 360°]

    Returns:
    dict:
        DGT_YawDegree
        DGT_PitchDegree
        DGT_RollDegree
    """
    yaw, pitch, roll = (_wrap180(yaw), _wrap180(pitch), _wrap180(roll))

    if abs(roll) > 90:
        yaw, pitch, roll = (_wrap180(yaw + 180.0), _wrap180(pitch), _wrap180(roll + 180.0))

    # ---- (B) pitch nadir 修正：DJI -90 (nadir) -> 0 ----
    pitch = pitch + 90.0

    # yaw 0~360
    yaw = _wrap360(yaw)

    return {
        "DGT_YawDegree":        yaw,
        "DGT_PitchDegree":      pitch,
        "DGT_RollDegree":       roll,
    }