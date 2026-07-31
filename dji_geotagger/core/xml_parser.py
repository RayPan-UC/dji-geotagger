from pathlib import Path
import pandas as pd
from PIL import Image
from tqdm import tqdm
from dji_geotagger.tools.logging_setup import get_logger

logger = get_logger(__name__)


def parse_img_info(
        img: str,
        add_format_orientation: bool = True
    ) -> dict | None:
    """
    Parse a single DJI image's XMP metadata into a flat dictionary.

    This function reads XMP metadata via Pillow (Image.getxmp) and extracts DJI fields:
      - exposure timestamp: UTCAtExposure (string, UTC)
      - GNSS position: GpsLatitude / GpsLongitude / AbsoluteAltitude
      - aircraft attitude: FlightYawDegree / FlightPitchDegree / FlightRollDegree
      - gimbal attitude:   GimbalYawDegree / GimbalPitchDegree / GimbalRollDegree

    If `add_format_orientation=True`, it also appends normalized gimbal angles:
      - DGT_YawDegree / DGT_PitchDegree / DGT_RollDegree
    produced by `_format_orientation()` to reduce DJI 0/180 roll flip discontinuities
    and convert DJI nadir pitch (-90°) into a photogrammetry-friendly convention (0°).

    Parameters
    ----------
    img : str | Path
        Path to an image file (e.g., .jpg or .tif) containing DJI-style XMP keys.
    add_format_orientation : bool, default True
        Whether to include the normalized DGT_* orientation fields.

    Returns
    -------
    dict | None
        Metadata dictionary on success; otherwise None.
        Returns None if:
          - XMP metadata is missing, or
          - required DJI keys are missing / schema differs, or
          - any exception occurs during parsing.

    Notes
    -----
    This parser is DJI-XMP specific. Non-DJI images or differing firmware schemas may not contain
    the expected keys and will be skipped.
    """
    img = Path(img)
    
    try:
        with Image.open(img) as im:
            xmp_data = im.getxmp()
        if not xmp_data:
            logger.warning(f"No metadata found at {img}. Skipped")
            return None
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
        fmt_ori = None
        if add_format_orientation:
            fmt_ori = _format_orientation(gimbal_yaw_deg, gimbal_pitch_deg, gimbal_roll_deg)
          

        meta_dict = {
        "FileName":               img.name,
        "UTCAtExposure":          utc_str,
        "GpsLatitude":            float(desc["GpsLatitude"]),
        "GpsLongitude":           float(desc["GpsLongitude"]),
        "AbsoluteAltitude":       float(desc["AbsoluteAltitude"]),
        "FlightYawDegree":        flight_yaw_deg,
        "FlightPitchDegree":      flight_pitch_deg,
        "FlightRollDegree":       flight_roll_deg,
        "GimbalYawDegree":        gimbal_yaw_deg,
        "GimbalPitchDegree":      gimbal_pitch_deg,
        "GimbalRollDegree":       gimbal_roll_deg,
        }
        
    except Exception as e:
        logger.warning(f"Error occurred while parsing image's metadata. {e}")
        return None


    return meta_dict | fmt_ori if fmt_ori else meta_dict

def parse_img_dir(
        img_dir: str,
        add_format_orientation: bool = True
    ) -> pd.DataFrame:
    """
    Parse DJI image XMP metadata for all images under a flight folder.

    This function scans the directory for:
      - *.jpg
      - *.tif
    (sorted lexicographically), parses each image via `parse_img_info()`, and
    returns a DataFrame of the successfully extracted records.

    Parameters
    ----------
    img_dir : str | Path
        Directory containing DJI images with XMP metadata.
    add_format_orientation : bool, default True
        Passed to `parse_img_info()`. If True, includes DGT_* orientation fields.

    Returns
    -------
    pd.DataFrame
        One row per successfully parsed image. The set of columns depends on the
        available XMP keys and whether `add_format_orientation` is enabled.

    Notes
    -----
    - Images without XMP metadata or missing required DJI keys are skipped.
    - The printed summary shows (total images found, successfully parsed records).
    """
    img_dir = Path(img_dir)
    image_files = sorted(img_dir.glob("*.jpg")) + sorted(img_dir.glob("*.tif"))

    if not image_files:
        logger.warning(f"No images found in {img_dir}")
        return pd.DataFrame()

    records = []
    for img in tqdm(image_files, desc="[INFO] Gathering image metadata (EXIF/XMP via Pillow)"):
        img_info = parse_img_info(img, add_format_orientation)
        if img_info is not None:
            records.append(img_info)

    # Save as Dataframe
    df = pd.DataFrame(records)
    logger.info(f"Parsed {len(image_files)} images ({len(df)} records)")
    return df



def _wrap180(a: float) -> float:
    """Wrap an angle in degrees to the range [-180, 180)."""
    return (a + 180.0) % 360.0 - 180.0

def _wrap360(a: float) -> float:
    """Wrap an angle in degrees to the range [0, 360)."""
    return (a + 360.0) % 360.0


def _format_orientation(yaw: float, pitch: float, roll: float) -> dict:
    """
    Normalize DJI gimbal yaw/pitch/roll into a stable, photogrammetry-friendly form.

    DJI gimbal angles can exhibit Euler-angle non-uniqueness, commonly seen as roll values
    near 0° or 180°. A ~180° roll often indicates an equivalent flipped representation.
    To reduce discontinuities (especially when converting to OPK), this function:
      1) Wraps yaw/pitch/roll into [-180°, 180°).
      2) If |roll| > 90°, absorbs a 180° flip by shifting yaw and roll by 180° (then re-wrap).
      3) Converts DJI pitch convention (nadir ≈ -90°) into a photogrammetry-style pitch
         where nadir ≈ 0° via: pitch_corrected = pitch + 90°.
      4) Wraps yaw into [0°, 360°).

    Parameters
    ----------
    yaw, pitch, roll : float
        DJI gimbal yaw/pitch/roll in degrees.

    Returns
    -------
    dict
        Dictionary with normalized angles:
        - DGT_YawDegree   (0–360 degrees)
        - DGT_PitchDegree (degrees; nadir ≈ 0)
        - DGT_RollDegree  (degrees; near 0 preferred over 180)
    """
    yaw, pitch, roll = (_wrap180(yaw), _wrap180(pitch), _wrap180(roll))

    if abs(roll) > 90:
        yaw, pitch, roll = (_wrap180(yaw + 180.0), _wrap180(pitch), _wrap180(roll + 180.0))

    # Convert DJI nadir pitch (-90°) to photogrammetry convention (0° = nadir)
    pitch = pitch + 90.0

    # yaw 0~360
    yaw = _wrap360(yaw)

    return {
        "DGT_YawDegree":        yaw,
        "DGT_PitchDegree":      pitch,
        "DGT_RollDegree":       roll,
    }