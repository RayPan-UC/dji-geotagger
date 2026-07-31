from pathlib import Path
from calendar import isleap
import numpy as np
from dji_geotagger.tools.tools import ECEF2ENU_vec
from dji_geotagger.tools.logging_setup import get_logger

logger = get_logger(__name__)


def parse_ppp_epoch(epoch_str: str) -> tuple[str, float | None]:
    """
    Convert a CSRS-PPP epoch token into a decimal year.

    The .sum POS records carry the reference epoch as ``YY:DDD:SSSSS``
    (two-digit year, day-of-year, second-of-day), e.g. ``25:211:68415``.

    A coordinate is meaningless without its epoch: in a plate-fixed frame such
    as NAD83(CSRS) the North American plate moves 1-2 cm/yr, so a decade of
    unstated epoch drift is a decimetre of unexplained bias.

    Parameters
    ----------
    epoch_str : str
        Raw epoch token from a POS line.

    Returns
    -------
    tuple[str, float | None]
        The raw token, and its decimal year (``None`` if unparseable).
    """
    try:
        yy, ddd, sod = epoch_str.split(":")
        # CSRS-PPP is a GPS-era product; a two-digit year is unambiguous.
        year = 2000 + int(yy)
        days = 366.0 if isleap(year) else 365.0
        decimal_year = year + ((int(ddd) - 1) + int(sod) / 86400.0) / days
        return epoch_str, decimal_year
    except (ValueError, AttributeError):
        return epoch_str, None

def resolve_ppp_sum_file(
    base_obs: str | None = None,
    sum_file_path: str | None = None,
) -> Path:
    """
    Resolve PPP summary (.sum) file path.

    Resolution Priority
    -------------------
    1. If user explicitly provides `sum_file_path` → use it directly.
    2. If only `base_obs` provided → auto-detect .sum file in same directory.
    3. Otherwise → raise error.

    Parameters
    ----------
    base_obs : str | Path, optional
        Path to base station RINEX observation file. Used for auto-detecting .sum file
        by matching the stem (filename without extension). Default is None.
    sum_file_path : str | Path, optional
        Explicit path to PPP summary (.sum) file. Takes priority over auto-detection.
        Default is None.

    Returns
    -------
    Path
        Resolved .sum file path.

    Raises
    ------
    FileNotFoundError
        If explicit `sum_file_path` does not exist, or if auto-detection from `base_obs`
        finds no matching .sum file.
    ValueError
        If neither `sum_file_path` nor `base_obs` is provided.
    """
    if base_obs is not None:
        base_obs = Path(base_obs)

    if sum_file_path is not None:
        sum_file_path = Path(sum_file_path)

    # 1. User explicitly provided path → highest priority
    if sum_file_path is not None:
        sum_file_path = Path(sum_file_path)

        if not sum_file_path.exists():
            raise FileNotFoundError(
                f"[ERROR] PPP summary file (.sum) not found: {sum_file_path}"
            )

        if base_obs is not None:
            logger.info("Both base_obs and sum_file_path provided. "
                        f"Using user-specified .sum file. {sum_file_path}")

        return sum_file_path

    # 2. Auto-detect from base_obs
    if base_obs is not None:
        base_obs = Path(base_obs)
        matches = list(base_obs.parent.glob(f"{base_obs.stem}.sum"))

        if matches:
            logger.info(f"Auto-detected PPP summary file: {matches[0]}")
            return matches[0]

        raise FileNotFoundError(
            f"[ERROR] No .sum file found for base: {base_obs.stem}"
        )

    # 3. Nothing provided
    raise ValueError(
        "[ERROR] Must provide either sum_file_path or base_obs"
    )


def sum_file_parser(
        base_obs: str | Path = None,
        sum_file_path: str | Path = None,
        print_report: bool = False):
    """
    Parse CSRS-PPP .sum file to extract final estimated ECEF position and covariance matrix.

    **Validation Note:**
    Compared PPP report with pymap3d transformation results:
    - Base LLH (PPP)     : 55.2942365333°, -114.6254905917°, 560.7136000000 m
    - Base LLH (pymap3d) : 55.2942365320°, -114.6254905912°, 560.7135461102 m
    - Difference: sub-millimeter level (both methods absolutely reliable)

    Parameters
    ----------
    base_obs : str | Path, optional
        Path to base station RINEX observation file. Used to auto-detect .sum file
        if `sum_file_path` is not explicitly provided. Default is None.
    sum_file_path : str | Path, optional
        Explicit path to PPP .sum file. Takes priority over auto-detection from `base_obs`.
        Default is None.
    print_report : bool, optional
        If True, print detailed coordinate and uncertainty report to console.
        Default is False.

    Returns
    -------
    dict
        Dictionary containing parsed PPP results:
        
        Provenance
        ----------
        - source : "csrs-ppp-sum"
        - source_detail : path of the .sum file used

        Coordinates
        -----------
        - X, Y, Z : ECEF coordinates (metres)
        - lat_dd : latitude in decimal degrees
        - lon_dd : longitude in decimal degrees
        - hgt : **ellipsoidal** height (metres), not orthometric
        - coord_sys : coordinate system string (e.g., "IGb20")
        - epoch : raw reference epoch token (e.g., "25:211:68415")
        - epoch_decimal_year : same epoch as a decimal year, or None

        Covariance & Uncertainty
        ------------------------
        - cov_PPP_ECEF : 3×3 covariance matrix in ECEF (m²)
        - cov_PPP_ENU : 3×3 covariance matrix in ENU (m²)
        - PPP_sigma_ECEF : 1-sigma standard deviations [σX, σY, σZ] (metres)
        - PPP_sigma_ENU : 1-sigma standard deviations [σE, σN, σU] (metres)

    Raises
    ------
    FileNotFoundError
        If .sum file cannot be found or resolved.
    ValueError
        If required POS entries are missing or could not be parsed from .sum file.
    """
    # Check exist
    sum_file_path = resolve_ppp_sum_file(base_obs, sum_file_path)
    
    # Placeholders
    est_X = est_Y = est_Z = None
    sigma_X = sigma_Y = sigma_Z = None
    rho_XY = rho_XZ = rho_YZ = None
    lat_dd = lon_dd = hgt = None
    coord_sys = None
    epoch_raw = None
    mode = None

    with open(sum_file_path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split()

            if len(parts) < 2:
                continue

            if parts[0] == "MOD":
                # MOD precedes the POS block, so this guard runs before any
                # attempt to read columns a non-static solution does not have.
                mode = parts[1].upper()
                if mode != "STATIC":
                    raise ValueError(
                        f"[ERROR] PPP summary was produced in {mode} mode, "
                        f"but a base station requires a STATIC solution.\n"
                        f"        {sum_file_path.name}\n"
                        "        A kinematic .sum reports only a priori "
                        "coordinates - no estimated position, sigma or "
                        "correlations - because the solution is a per-epoch "
                        "trajectory in the .pos file, not a single point.\n"
                        "        Reprocess with CSRS-PPP in Static mode."
                    )

            elif parts[0] == "POS" and parts[1] == "X":
                coord_sys = str((parts[2])) #coordinate system
                epoch_raw = str(parts[3])  # YY:DDD:SSSSS reference epoch
                est_X = float(parts[5])
                sigma_X = float(parts[7])  # 95%
                

            elif parts[0] == "POS" and parts[1] == "Y":
                est_Y = float(parts[5])
                sigma_Y = float(parts[7])
                rho_XY = float(parts[8])

            elif parts[0] == "POS" and parts[1] == "Z":
                est_Z = float(parts[5])
                sigma_Z = float(parts[7]) 
                rho_XZ = float(parts[8])
                rho_YZ = float(parts[9])

            elif parts[0] == "POS" and parts[1] == "LAT":
                lat_d = float(parts[7])
                lat_m = float(parts[8])
                lat_s = float(parts[9])
                lat_dd = np.sign(lat_d) * (abs(lat_d) + lat_m / 60 + lat_s / 3600)

            elif parts[0] == "POS" and parts[1] == "LON":
                lon_d = float(parts[7])
                lon_m = float(parts[8])
                lon_s = float(parts[9])
                lon_dd = np.sign(lon_d) * (abs(lon_d) + lon_m / 60 + lon_s / 3600)

            elif parts[0] == "POS" and parts[1] == "HGT":
                hgt = float(parts[5])

    # Check all parsed
    if None in (est_X, est_Y, est_Z, sigma_X, sigma_Y, sigma_Z, rho_XY, rho_XZ, rho_YZ, lat_dd, lon_dd, hgt, coord_sys):
        raise ValueError("[WARNING] Some POS entries missing or could not be parsed")

    # Covariance Matrix Calculation
    # sigma
    PPP_sigma_ECEF = np.array([sigma_X, sigma_Y, sigma_Z]) / 1.96 # 95% -> 1 sigma
    # correlation (ECEF)
    corr = np.array([
                        [    1.0,  rho_XY,  rho_XZ],
                        [ rho_XY,     1.0,  rho_YZ],
                        [ rho_XZ,  rho_YZ,     1.0]
                    ])
    # Covariance Matrix (ECEF)
    cov_PPP_ECEF = np.diag(PPP_sigma_ECEF) @ corr @ np.diag(PPP_sigma_ECEF)
    # Covariance Matrix (ENU)
    cov_PPP_ENU = ECEF2ENU_vec(cov_ecef=cov_PPP_ECEF, lat_deg=lat_dd, lon_deg=lon_dd)
    PPP_sigma_ENU = np.sqrt(np.diag(cov_PPP_ENU))

    # Reference epoch. Not included in the fatal check above: a missing epoch
    # degrades provenance but does not invalidate the coordinates themselves.
    epoch_str, epoch_decimal_year = parse_ppp_epoch(epoch_raw)

    # Summary
    if print_report:
        logger.info(f"Coord system : {coord_sys}"
              + (f" @ {epoch_decimal_year:.4f}" if epoch_decimal_year else ""))
        logger.info(f"Base ECEF    : ({est_X:.4f}, {est_Y:.4f}, {est_Z:.4f}) m")
        logger.info(f"Base LLH     : ({lat_dd:.7f}°, {lon_dd:.7f}°, {hgt:.4f} m)")
        logger.info(f"Base 1σ ENU  : E={PPP_sigma_ENU[0]*100:.2f} cm, N={PPP_sigma_ENU[1]*100:.2f} cm, U={PPP_sigma_ENU[2]*100:.2f} cm")
        logger.info(f"Base 1σ ECEF : X={PPP_sigma_ECEF[0]*100:.2f} cm, Y={PPP_sigma_ECEF[1]*100:.2f} cm, Z={PPP_sigma_ECEF[2]*100:.2f} cm")

    return {
        "source": "csrs-ppp-sum",
        "source_detail": str(sum_file_path),
        "mode": mode,
        "coord_sys": coord_sys,
        "epoch": epoch_str,
        "epoch_decimal_year": epoch_decimal_year,
        "X": est_X,
        "Y": est_Y,
        "Z": est_Z,
        "lat_dd": lat_dd,
        "lon_dd": lon_dd,
        "hgt": hgt,
        # A .sum always carries sigmas, so propagation is always possible here.
        "uncertainty_available": True,
        "cov_PPP_ECEF": cov_PPP_ECEF,
        "cov_PPP_ENU": cov_PPP_ENU,
        "PPP_sigma_ECEF": PPP_sigma_ECEF,
        "PPP_sigma_ENU": PPP_sigma_ENU,
    }
