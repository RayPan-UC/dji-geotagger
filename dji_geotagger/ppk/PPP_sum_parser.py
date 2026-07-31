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


def _parse_pos_header(parts: list[str], sum_file_path: Path) -> dict:
    """
    Read the POS block column layout from its header line.

    CSRS-PPP emits two different POS headers. Without epoch propagation there
    is a single sigma column::

        POS CRD SYST EPOCH A_PRIORI ESTIMATED DIFF SIGMA(95%) CORRELATIONS

    When the solution is propagated to a fixed epoch - ``sysref="NAD83"`` with
    ``nad83_epoch`` other than ``NAD83_CURR`` - a second sigma column appears::

        POS CRD SYST EPOCH A_PRIORI ESTIMATED DIFF SIG_PPP(95%) SIG_TOT(95%) CORRELATIONS

    ``SIG_TOT`` is the total: the PPP solution uncertainty combined with the
    velocity-grid interpolation error incurred by moving the coordinate in
    time. On a 15.6-year propagation it contributed 0.75-1.10 cm (1-sigma),
    i.e. the same order as the PPP solution itself, so it is not negligible.

    The extra column shifts every correlation one place right. Reading fixed
    offsets against a propagated .sum silently returns ``SIG_TOT`` where
    ``rho_XY`` is expected - a plausible-looking float, so nothing raises and
    the covariance matrix is quietly wrong. Hence this function.

    Parameters
    ----------
    parts : list[str]
        Whitespace-split tokens of the ``POS CRD`` header line.
    sum_file_path : Path
        Only used to name the file in error messages.

    Returns
    -------
    dict
        Token indices ``syst``, ``epoch``, ``est``, ``sigma``, ``corr``, and
        ``sigma_tot`` (``None`` when the file has a single sigma column).
        Data rows use the same offsets as the header, except the DMS rows -
        see :func:`sum_file_parser`.

    Raises
    ------
    ValueError
        If a column this parser depends on is absent, which means the .sum
        format has changed in a way that cannot be handled by shifting
        offsets.
    """
    def index_of(name: str) -> int:
        try:
            return parts.index(name)
        except ValueError:
            raise ValueError(
                f"[ERROR] The POS header of {sum_file_path.name} has no "
                f"{name!r} column.\n"
                f"        Header: {' '.join(parts)}\n"
                "        CSRS-PPP may have changed the .sum format; parsing "
                "was stopped rather than guessing column positions."
            ) from None

    # SIG_PPP/SIG_TOT replace SIGMA when the epoch is propagated.
    if "SIG_TOT(95%)" in parts:
        sigma = index_of("SIG_PPP(95%)")
        sigma_tot = index_of("SIG_TOT(95%)")
    else:
        sigma = index_of("SIGMA(95%)")
        sigma_tot = None

    return {
        "syst": index_of("SYST"),
        "epoch": index_of("EPOCH"),
        "est": index_of("ESTIMATED"),
        "sigma": sigma,
        "sigma_tot": sigma_tot,
        "corr": index_of("CORRELATIONS"),
    }


def sum_file_parser(
        base_obs: str | Path = None,
        sum_file_path: str | Path = None,
        print_report: bool = False):
    """
    Parse CSRS-PPP .sum file to extract final estimated ECEF position and covariance matrix.

    **Validation Note:**
    The geodetic coordinates reported in the .sum header were compared against
    converting the .sum's own ECEF coordinates with pymap3d. The two agree at
    the sub-millimetre level, so either may be used interchangeably.

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
        - coord_sys : coordinate system string, ``"IGb20"`` for an ITRF solve
          and ``"NAD83"`` for a NAD83 one. Note it carries no version number;
          the realization is NAD83(CSRS)v8, confirmed both by the ``VLM`` line
          and by agreement with EPSG:10412 to 0.055 mm.
        - epoch : raw reference epoch token (e.g., "25:211:68415")
        - epoch_decimal_year : same epoch as a decimal year, or None
        - velocity_model : ``VLM`` token (e.g. "NAD83v80VG"), or None. Present
          in every .sum, so it names the model available - not the frame used.
        - epoch_propagated : True when the solution was moved to a fixed epoch

        Covariance & Uncertainty
        ------------------------
        - cov_PPP_ECEF : 3×3 covariance matrix in ECEF (m²)
        - cov_PPP_ENU : 3×3 covariance matrix in ENU (m²)
        - PPP_sigma_ECEF : 1-sigma standard deviations [σX, σY, σZ] (metres)
        - PPP_sigma_ENU : 1-sigma standard deviations [σE, σN, σU] (metres)

        When ``epoch_propagated`` is True these include the velocity-grid
        interpolation error (CSRS-PPP's ``SIG_TOT``) as well as the solution
        error; see :func:`_parse_pos_header`.

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
    sigma_tot_X = sigma_tot_Y = sigma_tot_Z = None
    rho_XY = rho_XZ = rho_YZ = None
    lat_dd = lon_dd = hgt = None
    coord_sys = None
    epoch_raw = None
    mode = None
    velocity_model = None
    layout = None

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

            elif parts[0] == "POS" and parts[1] == "CRD":
                # Column header. Read the layout from it rather than assuming
                # fixed offsets: when CSRS-PPP propagates the epoch it inserts
                # a second sigma column, which shifts every correlation one
                # place to the right. See _parse_pos_header.
                layout = _parse_pos_header(parts, sum_file_path)

            elif parts[0] == "POS" and parts[1] == "X":
                if layout is None:
                    raise ValueError(
                        f"[ERROR] POS records appear before the 'POS CRD' "
                        f"column header in {sum_file_path.name}. The column "
                        "layout cannot be determined, and guessing it risks "
                        "reading sigmas as correlations."
                    )
                coord_sys = str(parts[layout["syst"]])  # coordinate system
                epoch_raw = str(parts[layout["epoch"]])  # YY:DDD:SSSSS
                est_X = float(parts[layout["est"]])
                sigma_X = float(parts[layout["sigma"]])  # 95%
                if layout["sigma_tot"] is not None:
                    sigma_tot_X = float(parts[layout["sigma_tot"]])

            elif parts[0] == "POS" and parts[1] == "Y":
                est_Y = float(parts[layout["est"]])
                sigma_Y = float(parts[layout["sigma"]])
                if layout["sigma_tot"] is not None:
                    sigma_tot_Y = float(parts[layout["sigma_tot"]])
                rho_XY = float(parts[layout["corr"]])

            elif parts[0] == "POS" and parts[1] == "Z":
                est_Z = float(parts[layout["est"]])
                sigma_Z = float(parts[layout["sigma"]])
                if layout["sigma_tot"] is not None:
                    sigma_tot_Z = float(parts[layout["sigma_tot"]])
                rho_XZ = float(parts[layout["corr"]])
                rho_YZ = float(parts[layout["corr"] + 1])

            elif parts[0] == "POS" and parts[1] == "LAT":
                # DMS rows split each coordinate into three tokens, so the
                # estimated value starts two places after the scalar offset.
                i = layout["est"] + 2
                lat_d, lat_m, lat_s = (float(v) for v in parts[i:i + 3])
                lat_dd = np.sign(lat_d) * (abs(lat_d) + lat_m / 60 + lat_s / 3600)

            elif parts[0] == "POS" and parts[1] == "LON":
                i = layout["est"] + 2
                lon_d, lon_m, lon_s = (float(v) for v in parts[i:i + 3])
                lon_dd = np.sign(lon_d) * (abs(lon_d) + lon_m / 60 + lon_s / 3600)

            elif parts[0] == "POS" and parts[1] == "HGT":
                hgt = float(parts[layout["est"]])

            elif parts[0] == "VLM":
                # Velocity model used for epoch propagation, e.g. NAD83v80VG.
                # Present in every .sum, not only propagated ones, so it
                # identifies the model available - not the output frame.
                velocity_model = str(parts[1])

    # Check all parsed
    if None in (est_X, est_Y, est_Z, sigma_X, sigma_Y, sigma_Z, rho_XY, rho_XZ, rho_YZ, lat_dd, lon_dd, hgt, coord_sys):
        raise ValueError("[WARNING] Some POS entries missing or could not be parsed")

    # Covariance Matrix Calculation
    # sigma
    sigma_solution = np.array([sigma_X, sigma_Y, sigma_Z]) / 1.96 # 95% -> 1 sigma
    # correlation (ECEF)
    corr = np.array([
                        [    1.0,  rho_XY,  rho_XZ],
                        [ rho_XY,     1.0,  rho_YZ],
                        [ rho_XZ,  rho_YZ,     1.0]
                    ])
    # Covariance Matrix (ECEF)
    cov_PPP_ECEF = np.diag(sigma_solution) @ corr @ np.diag(sigma_solution)

    # Epoch-propagation error, when CSRS-PPP reported it (SIG_TOT column).
    #
    # Only one correlation set is printed, and it is identical between a
    # propagated and a non-propagated solve of the same data - so those are the
    # solution correlations, and nothing is published about how the
    # velocity-grid error correlates across X/Y/Z. It is therefore added as an
    # independent, diagonal term rather than by rescaling the solution
    # correlations, which would fabricate off-diagonals that were never
    # measured. This reproduces SIG_TOT exactly on the diagonal, keeps the
    # matrix positive semi-definite, and matches how the rest of the pipeline
    # combines independent error sources (cov_total = cov_PPK + cov_PPP).
    if sigma_tot_X is not None:
        sigma_total = np.array([sigma_tot_X, sigma_tot_Y, sigma_tot_Z]) / 1.96
        # Guard against a negative under the root if the columns ever disagree.
        var_prop = np.maximum(sigma_total ** 2 - sigma_solution ** 2, 0.0)
        cov_PPP_ECEF = cov_PPP_ECEF + np.diag(var_prop)

    # Derived from the covariance rather than from the sigma columns directly,
    # so it stays consistent whether or not the propagation term was added.
    PPP_sigma_ECEF = np.sqrt(np.diag(cov_PPP_ECEF))

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
        "velocity_model": velocity_model,
        # True when CSRS-PPP propagated the solution to a fixed epoch and
        # reported SIG_TOT; the returned covariance then includes that error.
        "epoch_propagated": sigma_tot_X is not None,
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
