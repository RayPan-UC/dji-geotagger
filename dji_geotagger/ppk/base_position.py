"""
Base-station position sources.

The PPK pipeline needs one thing from the base station: a position with a
covariance. Where that comes from is the user's choice, and all sources return
the same dictionary so nothing downstream has to care:

============  ==============================================================
``sum``       An existing CSRS-PPP ``.sum`` file (parsed by
              :func:`~dji_geotagger.ppk.PPP_sum_parser.sum_file_parser`).
``online``    Submitted to CSRS-PPP automatically, ``.sum`` fetched back.
``manual``    Coordinates entered directly - typically a published control
              point or CORS position, which can be *more* accurate than a
              short PPP session rather than a fallback from one.
============  ==============================================================

Manual entry is the only source that can be wrong without saying so, because a
base-station error translates the whole photo block uniformly: the relative
geometry stays consistent, PPK still reports clean fixes, and the residuals
never mention it. :func:`build_base_position` is therefore deliberately strict
about the three inputs that cause silent, systematic error - height type,
uncertainty, and reference frame.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pyproj import Transformer

from dji_geotagger.tools.tools import ECEF2ENU_vec, ENU2ECEF_vec
from dji_geotagger.tools.logging_setup import get_logger

logger = get_logger(__name__)

# EPSG:4978 geocentric (ECEF) <-> EPSG:4979 geographic 3D. Both are GRS80/WGS84
# ellipsoid; the frames this tool handles (IGb20, ITRF20xx, NAD83(CSRS)) share
# that ellipsoid, so this conversion is purely geometric and introduces no
# datum shift of its own.
_ECEF_TO_LLH = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)
_LLH_TO_ECEF = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)

VALID_SOURCES = ("sum", "online", "manual")


def build_base_position(
    X: float | None = None,
    Y: float | None = None,
    Z: float | None = None,
    lat_dd: float | None = None,
    lon_dd: float | None = None,
    hgt: float | None = None,
    *,
    sigma_ENU,
    coord_sys: str,
    epoch: str | None = None,
    corr_ENU: np.ndarray | None = None,
    height_type: str = "ellipsoidal",
    print_report: bool = False,
) -> dict:
    """
    Build a base-station position dictionary from user-entered coordinates.

    Accepts either ECEF ``X/Y/Z`` **or** geodetic ``lat_dd/lon_dd/hgt``, never
    both. The missing representation is derived, and both are reported, so a
    mistyped digit is visible at a glance.

    Parameters
    ----------
    X, Y, Z : float, optional
        ECEF coordinates in metres. Mutually exclusive with the geodetic set.
    lat_dd, lon_dd : float, optional
        Geodetic latitude / longitude in decimal degrees.
    hgt : float, optional
        **Ellipsoidal** height in metres. See `height_type`.
    sigma_ENU : array_like or None
        1-sigma standard deviations ``[σE, σN, σU]`` in metres, strictly
        positive. Pass ``None`` only if the uncertainty is genuinely unknown;
        base error propagation is then switched off for the whole run and the
        result is labelled accordingly. See Notes.
    coord_sys : str
        Reference frame of the coordinates, e.g. ``"IGb20"``,
        ``"NAD83(CSRS)"``, ``"ITRF2020"``. Recorded verbatim; no datum
        transformation is performed.
    epoch : str, optional
        Reference epoch of the coordinates, e.g. ``"2010.0"``. Strongly
        recommended for plate-fixed frames such as NAD83(CSRS).
    corr_ENU : np.ndarray, optional
        3×3 correlation matrix in ENU. Defaults to the identity (uncorrelated),
        which is what control-point datasheets normally imply since they
        publish standard deviations only.
    height_type : {"ellipsoidal"}, optional
        Guard rail, not a conversion. Only ``"ellipsoidal"`` is accepted.
    print_report : bool, optional
        Print the position in both representations for visual verification.

    Returns
    -------
    dict
        Same structure as
        :func:`~dji_geotagger.ppk.PPP_sum_parser.sum_file_parser`, with
        ``source`` set to ``"manual"``.

    Raises
    ------
    ValueError
        If the coordinate set is absent, incomplete, or doubly specified; if
        `sigma_ENU` is not strictly positive; if `corr_ENU` is not a valid
        correlation matrix; or if `height_type` is not ``"ellipsoidal"``.

    Notes
    -----
    **Why sigma is not defaulted.** The pipeline adds this covariance to the
    PPK per-epoch covariance (``cov_total = cov_PPK + cov_PPP``). Silently
    defaulting a missing sigma to zero would assert that the base station is
    perfectly known, and every reported uncertainty downstream would be
    optimistic by exactly the amount the user failed to state - a silent error
    in the one number a user checks to decide whether to trust the result.

    ``sigma_ENU=None`` is therefore handled by *disabling* base error
    propagation rather than by assuming zero. The reported sigmas then cover
    the PPK solution only, and ``uncertainty_available`` is False so the
    limitation travels with the data instead of being forgotten. A stated but
    conservative estimate is still better than no estimate; ``None`` is for
    when nothing at all is known.

    **Why orthometric heights are refused rather than converted.** Published
    elevations are usually orthometric (CGVD28 / CGVD2013); ellipsoidal and
    orthometric heights differ by the geoid separation, tens of metres in
    Canada. Entering one for the other shifts the entire block vertically
    while PPK continues to report clean fixes. Converting would require a
    geoid model this tool does not ship, and a half-correct conversion is
    worse than a refusal, so the caller is asked to convert first.
    """
    if height_type != "ellipsoidal":
        raise ValueError(
            f"[ERROR] height_type={height_type!r} is not supported. Only "
            "ellipsoidal heights are accepted.\n"
            "        Published elevations are usually orthometric (CGVD28 / "
            "CGVD2013) and differ from ellipsoidal height by the geoid "
            "separation - tens of metres in Canada.\n"
            "        Convert first, e.g. with NRCan GPS-H: "
            "https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/gpsh.php"
        )

    have_ecef = None not in (X, Y, Z)
    have_llh = None not in (lat_dd, lon_dd, hgt)
    any_ecef = any(v is not None for v in (X, Y, Z))
    any_llh = any(v is not None for v in (lat_dd, lon_dd, hgt))

    if have_ecef and have_llh:
        raise ValueError(
            "[ERROR] Provide either ECEF (X, Y, Z) or geodetic "
            "(lat_dd, lon_dd, hgt), not both. Supplying both invites a silent "
            "mismatch between them."
        )
    if not have_ecef and not have_llh:
        if any_ecef or any_llh:
            raise ValueError(
                "[ERROR] Incomplete coordinates. Provide all three of "
                "(X, Y, Z) or all three of (lat_dd, lon_dd, hgt)."
            )
        raise ValueError(
            "[ERROR] No base coordinates provided. Provide either "
            "(X, Y, Z) or (lat_dd, lon_dd, hgt)."
        )

    # Uncertainty: either explicitly unknown, or strictly positive. Never zero.
    uncertainty_available = sigma_ENU is not None
    if uncertainty_available:
        sigma_ENU = np.asarray(sigma_ENU, dtype=float).ravel()
        if sigma_ENU.size != 3:
            raise ValueError(
                f"[ERROR] sigma_ENU must have 3 elements [σE, σN, σU], "
                f"got {sigma_ENU.size}."
            )
        if not np.all(np.isfinite(sigma_ENU)) or np.any(sigma_ENU <= 0):
            raise ValueError(
                f"[ERROR] sigma_ENU must be strictly positive, got "
                f"{sigma_ENU}.\n"
                "        A zero sigma asserts a perfectly known base station "
                "and makes every downstream uncertainty optimistic.\n"
                "        Use the values from the control-point datasheet, a "
                "deliberately conservative estimate such as "
                "(0.02, 0.02, 0.04) m, or sigma_ENU=None to disable base "
                "error propagation entirely."
            )

    if corr_ENU is None:
        corr_ENU = np.eye(3)
    else:
        corr_ENU = np.asarray(corr_ENU, dtype=float)
        if corr_ENU.shape != (3, 3):
            raise ValueError(
                f"[ERROR] corr_ENU must be 3×3, got {corr_ENU.shape}.")
        if not np.allclose(corr_ENU, corr_ENU.T):
            raise ValueError("[ERROR] corr_ENU must be symmetric.")
        if not np.allclose(np.diag(corr_ENU), 1.0):
            raise ValueError("[ERROR] corr_ENU must have unit diagonal.")
        if np.any(np.linalg.eigvalsh(corr_ENU) <= 0):
            raise ValueError(
                "[ERROR] corr_ENU is not positive definite; the implied "
                "covariance would not describe a real distribution.")

    # Complete whichever representation the caller did not supply.
    if have_llh:
        X, Y, Z = _LLH_TO_ECEF.transform(lon_dd, lat_dd, hgt)
    else:
        lon_dd, lat_dd, hgt = _ECEF_TO_LLH.transform(X, Y, Z)

    if uncertainty_available:
        # Covariance is defined in ENU (how datasheets state it) and converted
        # to ECEF (how the pipeline combines it).
        cov_ENU = np.diag(sigma_ENU) @ corr_ENU @ np.diag(sigma_ENU)
        cov_ECEF = ENU2ECEF_vec(cov_enu=cov_ENU, lat_deg=lat_dd, lon_deg=lon_dd)
        sigma_ECEF = np.sqrt(np.diag(cov_ECEF))
        # Recompute rather than reuse the input, so the returned ENU covariance
        # is the one actually implied by what the pipeline will consume.
        cov_ENU_out = ECEF2ENU_vec(cov_ecef=cov_ECEF, lat_deg=lat_dd,
                                   lon_deg=lon_dd)
        sigma_ENU_out = np.sqrt(np.diag(cov_ENU_out))
    else:
        # Deliberately None rather than zero: downstream treats None as
        # "do not propagate", whereas zero would claim a perfect base.
        cov_ECEF = cov_ENU_out = sigma_ECEF = sigma_ENU_out = None

    if print_report:
        epoch_note = f" @ {epoch}" if epoch else " (epoch not stated)"
        logger.info("Base position (manual entry) - verify before proceeding")
        logger.info(f"Frame        : {coord_sys}{epoch_note}")
        logger.info(f"Base ECEF    : ({X:.4f}, {Y:.4f}, {Z:.4f}) m")
        logger.info(f"Base LLH     : ({lat_dd:.7f}°, {lon_dd:.7f}°, "
                    f"{hgt:.4f} m ellipsoidal)")
        if uncertainty_available:
            logger.info(f"Base 1σ ENU  : E={sigma_ENU_out[0]*100:.2f} cm, "
                        f"N={sigma_ENU_out[1]*100:.2f} cm, "
                        f"U={sigma_ENU_out[2]*100:.2f} cm  [user-supplied]")
        else:
            logger.warning(
                "No base uncertainty supplied. Base error propagation is "
                "DISABLED for this run.")
            logger.warning(
                "Reported sigmas will cover the PPK solution only and will "
                "understate the true uncertainty of the camera positions.")
        if not epoch:
            logger.warning("No epoch stated. For plate-fixed frames such as "
                           "NAD83(CSRS) this leaves the coordinate ambiguous at "
                           "1-2 cm/yr.")

    return {
        "source": "manual",
        "source_detail": "user-entered coordinates",
        # No processing mode applies to a directly-entered coordinate; the key
        # is present so every source returns an identical set.
        "mode": None,
        "coord_sys": coord_sys,
        "epoch": epoch,
        "epoch_decimal_year": _epoch_to_decimal_year(epoch),
        # Both describe how CSRS-PPP moved a solution through time, which does
        # not apply here: whatever the user typed is taken at its stated epoch.
        "velocity_model": None,
        "epoch_propagated": False,
        "X": float(X),
        "Y": float(Y),
        "Z": float(Z),
        "lat_dd": float(lat_dd),
        "lon_dd": float(lon_dd),
        "hgt": float(hgt),
        "uncertainty_available": uncertainty_available,
        "cov_PPP_ECEF": cov_ECEF,
        "cov_PPP_ENU": cov_ENU_out,
        "PPP_sigma_ECEF": sigma_ECEF,
        "PPP_sigma_ENU": sigma_ENU_out,
    }


def resolve_base_position(
    mode: str = "sum",
    *,
    base_obs: str | None = None,
    sum_file_path: str | None = None,
    email: str | None = None,
    ppp_out_dir: str | None = None,
    ppp_kwargs: dict | None = None,
    manual_kwargs: dict | None = None,
    print_report: bool = True,
) -> dict:
    """
    Obtain the base-station position from the source the user selected.

    Parameters
    ----------
    mode : {"sum", "online", "manual"}
        Which source to use.

        ``sum``
            Parse an existing CSRS-PPP ``.sum``, located explicitly via
            `sum_file_path` or automatically next to `base_obs`.
        ``online``
            Submit `base_obs` to CSRS-PPP and use the returned ``.sum``.
            Requires `email`.
        ``manual``
            Build the position from coordinates in `manual_kwargs`.
    base_obs : str, optional
        Base-station RINEX file. Used to locate the ``.sum`` in ``sum`` mode
        and as the upload in ``online`` mode.
    sum_file_path : str, optional
        Explicit ``.sum`` path for ``sum`` mode.
    email : str, optional
        Email address, required for ``online`` mode. No CSRS account is
        needed - the service validates the format only.
    ppp_out_dir : str, optional
        Where ``online`` mode writes downloaded results. Defaults to a ``PPP``
        directory beside `base_obs`.
    ppp_kwargs : dict, optional
        Extra arguments for
        :func:`~dji_geotagger.ppk.ppp_service.run_online_ppp`, e.g.
        ``{"sysref": "NAD83", "nad83_epoch": "NAD83_CURR"}``.
    manual_kwargs : dict, optional
        Arguments for :func:`build_base_position`.
    print_report : bool, optional
        Print the resulting position for verification.

    Returns
    -------
    dict
        The base-position dictionary, identical in structure regardless of
        source, with ``source`` recording which one was used.

    Raises
    ------
    ValueError
        If `mode` is unknown or its required arguments are missing.

    Notes
    -----
    **There is no automatic fallback between accuracy classes.** If ``online``
    fails, this raises rather than quietly substituting a different source.
    A base-station error translates the entire photo block uniformly - PPK
    still reports clean fixes and the residuals stay clean - so a silent
    downgrade would be invisible in every number a user checks. Choosing a
    different source after a failure is the user's decision to make.
    """
    from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser

    if mode not in VALID_SOURCES:
        raise ValueError(
            f"[ERROR] mode must be one of {VALID_SOURCES}, got {mode!r}")

    if mode == "manual":
        if not manual_kwargs:
            raise ValueError(
                "[ERROR] mode='manual' requires manual_kwargs with at least "
                "coordinates, sigma_ENU and coord_sys.")
        return build_base_position(print_report=print_report, **manual_kwargs)

    if mode == "online":
        from dji_geotagger.ppk.ppp_service import run_online_ppp

        if base_obs is None:
            raise ValueError(
                "[ERROR] mode='online' requires base_obs (the RINEX file to "
                "submit).")
        if not email:
            raise ValueError(
                "[ERROR] mode='online' requires email (the address CSRS-PPP "
                "sends results to). No account is needed - the service checks "
                "the format only - but use an address you can read: the "
                "notification carries the processing key.")

        out_dir = Path(ppp_out_dir) if ppp_out_dir \
            else Path(base_obs).parent / "PPP"
        sum_file_path = run_online_ppp(base_obs, email, out_dir,
                                       **(ppp_kwargs or {}))
        result = sum_file_parser(sum_file_path=sum_file_path,
                                 print_report=print_report)
        result["source"] = "csrs-ppp-online"
        return result

    # mode == "sum"
    return sum_file_parser(base_obs=base_obs, sum_file_path=sum_file_path,
                           print_report=print_report)


def _epoch_to_decimal_year(epoch: str | None) -> float | None:
    """
    Interpret a user-entered epoch as a decimal year.

    Accepts a plain decimal year (``"2010.0"``). CSRS-PPP's ``YY:DDD:SSSSS``
    form is handled by
    :func:`~dji_geotagger.ppk.PPP_sum_parser.parse_ppp_epoch`; here it is
    delegated so both spellings work wherever a user types one.
    """
    if epoch is None:
        return None
    try:
        return float(epoch)
    except ValueError:
        from dji_geotagger.ppk.PPP_sum_parser import parse_ppp_epoch
        return parse_ppp_epoch(epoch)[1]
