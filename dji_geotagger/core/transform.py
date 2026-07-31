"""
Coordinate transformation for exported geotags.

This is an *export* step, not a pipeline stage. ``geotag()`` deliberately
leaves its output in whatever frame CSRS-PPP solved in, tagged with the
reference epoch, because that pair is lossless: any other frame can still be
derived from it. Overwriting it with a "standard" frame such as WGS 84 throws
away the epoch and cannot be undone.

Transformation is really three separate things, and only the last is safe to
take for granted:

    IGb20 @ 2025.5749                 what CSRS-PPP returns
        |
        | (1) datum transformation    1.63 m at the test site
        v
    NAD83(CSRS)v8 @ 2025.5749
        |
        | (2) epoch propagation       5.19 cm over 15.6 years
        v
    NAD83(CSRS)v8 @ 2010.0
        |
        | (3) projection              pure mathematics, offline, exact
        v
    NAD83(CSRS)v8 / UTM zone 11N

Steps (1) and (3) are done here with pyproj. Step (2) is **not**: it needs the
NAD83 v8.0 velocity grid, which PROJ does not ship (its CDN carries v6 and
v7.0 only). Ask CSRS-PPP for the epoch you want instead - see
:func:`transform_coordinates` - which also returns the propagation uncertainty
that neither pyproj nor NRCan's TRX will give you.

Verification (2026-07-31, base station DRTK3_0038)
--------------------------------------------------
The same RINEX was submitted to CSRS-PPP three ways, and pyproj was checked
against NRCan's own answers:

- datum transformation, pyproj EPSG:9988 -> 10412 versus a native NAD83
  solve of the same data: **0.055 mm**
- projection, versus the ``PRJ`` line CSRS-PPP prints in the .sum, for both
  NAD83@2010 and IGb20@obs: **exact to the printed millimetre**

Why this module refuses things
------------------------------
PROJ never raises when it cannot do what you asked; it degrades. Three
distinct failure modes, all silent, all measured at the test site:

1. **Ballpark fallback.** Asking for a datum with no published rigorous
   transformation returns the coordinates *unchanged*. ITRF2020 -> TWD97
   moved the test point by 0.0 mm. Detected via
   ``operations[].has_ballpark_transformation``.

2. **Datum ensembles.** "WGS 84" is an ensemble with ~2 m internal accuracy,
   so PROJ offers several candidate operations whose stated accuracies all
   cluster near 2 m. It picks the numerically smallest, and at the test site
   that is a route through NAD83(2011) - a **1.60 m** shift mislabelled as
   WGS 84. Neither the ballpark flag nor the accuracy value catches it.

3. **Unversioned CRS codes.** EPSG:2956 "NAD83(CSRS) / UTM zone 12N" carries
   no realization, so it too falls back to ballpark and loses the entire
   1.63 m datum shift. Always use the versioned code (EPSG:22812 for v8).

Each is refused by default and can be overridden only by an explicit keyword,
so the risk is taken on purpose rather than by accident.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pymap3d as pm
from pyproj import CRS, Transformer
from pyproj.crs import GeographicCRS, ProjectedCRS
from pyproj.crs.coordinate_operation import UTMConversion

from dji_geotagger.tools.logging_setup import get_logger
from dji_geotagger.tools.progress import as_progress

logger = get_logger(__name__)


# Frame token as it appears in the .sum POS records -> geocentric CRS.
#
# "NAD83" is what CSRS-PPP writes for a NAD83 solve, with no realization. It
# is NAD83(CSRS)v8: the .sum names its velocity model as NAD83v80VG, and a
# native NAD83 solve agreed with EPSG:10412 to 0.055 mm on 2026-07-31. Should
# NRCan move to v9 this mapping becomes silently stale by a few millimetres,
# so it is logged whenever it is used and `source_crs` overrides it.
FRAME_EPSG = {
    "IGB20": 9988, "IGS20": 9988, "ITRF2020": 9988,
    "IGB14": 7789, "IGS14": 7789, "ITRF2014": 7789,
    "IGB08": 5332, "IGS08": 5332, "ITRF2008": 5332,
    "ITRF2005": 4896,
    "ITRF2000": 4919,
    "ITRF97": 4918,
    "ITRF96": 4917,
    "NAD83": 10412,
}

# Frames whose EPSG code does not pin the realization; see module docstring.
_AMBIGUOUS_TOKENS = {"NAD83"}

# Finite-difference step for the sigma Jacobian, in metres.
#
# Measured noise floor at one metre is ~3e-7 m/m, which couples a 3 cm sigma
# into a neighbouring axis by 9 nm - irrelevant next to the millimetre-level
# uncertainties being transformed. A larger step would push roundoff down
# faster than it raises truncation error (curvature over 100 m is ~1e-10
# relative), but it risks stepping outside a grid file's extent near its
# boundary, and buys precision that is already far past the point of mattering.
_JACOBIAN_STEP_M = 1.0


class TransformError(ValueError):
    """Raised when a transformation cannot be done safely."""


def resolve_source_crs(coord_sys: str) -> CRS:
    """
    Map a ``.sum`` frame token onto a geocentric CRS.

    Parameters
    ----------
    coord_sys : str
        Frame token, e.g. ``"IGb20"`` or ``"NAD83"``. Case-insensitive.

    Returns
    -------
    pyproj.CRS
        Geocentric CRS for that frame.

    Raises
    ------
    TransformError
        If the token is not recognised. Guessing is refused: an unrecognised
        frame that happens to be close to a known one would introduce a
        decimetre-to-metre bias with no symptom.
    """
    if not coord_sys:
        raise TransformError(
            "[ERROR] No source frame given. The output of geotag() carries it "
            "in the 'coord_sys' column; pass source_crs= if it is missing."
        )

    key = str(coord_sys).strip().upper()
    if key not in FRAME_EPSG:
        raise TransformError(
            f"[ERROR] Unrecognised source frame {coord_sys!r}.\n"
            f"        Known: {', '.join(sorted(FRAME_EPSG))}\n"
            "        Pass source_crs=<EPSG code or CRS> to state it "
            "explicitly. It is not guessed, because a near-miss would shift "
            "every coordinate by decimetres without any visible symptom."
        )

    if key in _AMBIGUOUS_TOKENS:
        logger.info(
            f"Frame token {coord_sys!r} carries no realization; reading it as "
            f"EPSG:{FRAME_EPSG[key]} ({CRS.from_epsg(FRAME_EPSG[key]).name}). "
            "Pass source_crs= to override."
        )
    return CRS.from_epsg(FRAME_EPSG[key])


def make_utm_crs(zone: int, datum_crs, south: bool = False) -> CRS:
    """
    Build a UTM CRS on the datum of another CRS.

    EPSG publishes UTM codes only for some datums - there is no ITRF2020 UTM
    zone, for instance - and the generic codes that do exist often carry an
    unversioned datum that sends PROJ down the ballpark path. Deriving the
    projected CRS from a datum you already trust sidesteps both problems.

    Verified against the ``PRJ`` line CSRS-PPP writes in the .sum: an ITRF2020
    UTM 11N built this way reproduced NRCan's easting and northing exactly,
    where ``EPSG:32611`` ("WGS 84 / UTM zone 11N") was 1.60 m away.

    Parameters
    ----------
    zone : int
        UTM zone, 1-60.
    datum_crs : pyproj.CRS or int or str
        Any CRS on the wanted datum; only its datum is used.
    south : bool, default False
        Southern hemisphere.

    Returns
    -------
    pyproj.CRS
        Projected CRS.
    """
    if not 1 <= int(zone) <= 60:
        raise TransformError(f"[ERROR] UTM zone must be 1-60, got {zone!r}")

    datum_crs = CRS.from_user_input(datum_crs)
    base = GeographicCRS(name=f"{datum_crs.name} (geographic)",
                         datum=datum_crs.datum)
    hemi = "S" if south else "N"
    return ProjectedCRS(
        name=f"{datum_crs.name} / UTM zone {int(zone)}{hemi}",
        conversion=UTMConversion(int(zone), hemi),
        geodetic_crs=base,
    )


def rebase_projected_crs(projected_crs, datum_crs) -> CRS:
    """
    Keep a projection's grid definition but put it on a different datum.

    EPSG's coverage of realizations is uneven. Alberta 3TM exists only as
    ``NAD83(CSRS)`` with no version (EPSG:3779-3802), and Alberta is where
    this package is used - so the codes a user reaches for first are exactly
    the ones that fall back to ballpark and discard the 1.63 m datum shift.
    :func:`make_utm_crs` solves that for UTM only; this solves it for any
    projection by lifting the grid definition off one CRS and re-basing it.

    Verified against the ``MTM`` line CSRS-PPP prints in the .sum: an Alberta
    3TM 114 W rebased onto NAD83(CSRS)v8 reproduced NRCan's northing exactly
    (6129551.361), where the unversioned EPSG:3780 was 1.60 m away.

    Parameters
    ----------
    projected_crs : pyproj.CRS or int or str
        Supplies the map projection - central meridian, scale factor, false
        origin. Its datum is discarded.
    datum_crs : pyproj.CRS or int or str
        Supplies the datum, e.g. ``10412`` for NAD83(CSRS)v8.

    Returns
    -------
    pyproj.CRS
        Projected CRS with the one's grid and the other's datum.

    Raises
    ------
    TransformError
        If `projected_crs` is not a projected CRS, so there is no projection
        to lift.

    Examples
    --------
    >>> alberta_3tm_v8 = rebase_projected_crs(3780, 10412)
    >>> transform_coordinates(geotag_df, alberta_3tm_v8)   # doctest: +SKIP
    """
    projected_crs = CRS.from_user_input(projected_crs)
    datum_crs = CRS.from_user_input(datum_crs)

    if not projected_crs.is_projected or projected_crs.coordinate_operation is None:
        raise TransformError(
            f"[ERROR] {projected_crs.name!r} is not a projected CRS, so it "
            "carries no map projection to re-base. Pass the projected CRS "
            "whose grid you want (e.g. EPSG:3780) as the first argument and "
            "the datum you want (e.g. EPSG:10412) as the second."
        )

    base = GeographicCRS(name=f"{datum_crs.name} (geographic)",
                         datum=datum_crs.datum)
    return ProjectedCRS(
        name=f"{datum_crs.name} / {projected_crs.coordinate_operation.name}",
        conversion=projected_crs.coordinate_operation,
        geodetic_crs=base,
    )


def _geographic3d(crs: CRS) -> CRS:
    """
    Geographic 3D CRS on the same datum as ``crs``.

    ``CRS.geodetic_crs`` returns a geocentric CRS unchanged and a 2D
    geographic one for projected CRSs, so neither case can be used directly.
    """
    base = crs.geodetic_crs
    if base is None or base.is_geocentric:
        base = GeographicCRS(name=f"{crs.name} (geographic)", datum=crs.datum)
    return base.to_3d()


def _is_ensemble(crs: CRS) -> bool:
    """Whether the CRS is built on a datum ensemble rather than a datum."""
    datum = crs.datum
    return datum is not None and "ensemble" in datum.type_name.lower()


def _is_ballpark(transformer: Transformer) -> bool:
    """
    Whether PROJ fell back to a ballpark (i.e. no-op) datum shift.

    Two tests because neither alone is sufficient: concatenated pipelines
    expose their steps through ``operations``, while a single resolved
    operation has an empty ``operations`` list and only names itself in
    ``description``. ``description`` is also lazy - it reads "unavailable
    until proj_trans is called" until the transformer has been used - so
    callers must probe first.
    """
    if any(op.has_ballpark_transformation for op in transformer.operations):
        return True
    return "ballpark" in transformer.description.lower()


def _check_pair(source: CRS, target: CRS, *, allow_ballpark: bool,
                allow_datum_ensemble: bool, probe: tuple) -> Transformer:
    """
    Build a transformer and refuse the known silent-failure cases.

    ``probe`` is a representative ``(x, y, z, epoch)`` used to force PROJ to
    resolve the pipeline so its description can be inspected.
    """
    for crs, role in ((source, "Source"), (target, "Target")):
        if _is_ensemble(crs) and not allow_datum_ensemble:
            raise TransformError(
                f"[ERROR] {role} CRS {crs.name!r} is built on a datum "
                f"ensemble ({crs.datum.name}), whose internal accuracy is "
                "about 2 m.\n"
                "        PROJ then has several candidate operations with "
                "near-identical stated accuracies and picks by a margin that "
                "carries no physical meaning. At the reference site that "
                "choice was a 1.60 m shift, reported as neither ballpark nor "
                "low accuracy.\n"
                "        Use a specific realization instead - EPSG:9988 for "
                "ITRF2020, EPSG:10412 for NAD83(CSRS)v8, EPSG:22811 for its "
                "UTM zone 11N - or pass allow_datum_ensemble=True to accept "
                "metre-level ambiguity."
            )

    transformer = Transformer.from_crs(source, target, always_xy=True)
    transformer.transform(*probe)  # resolve the pipeline before reading it

    if _is_ballpark(transformer) and not allow_ballpark:
        raise TransformError(
            f"[ERROR] No rigorous transformation exists from {source.name!r} "
            f"to {target.name!r}; PROJ fell back to a ballpark shift.\n"
            f"        Operation: {transformer.description}\n"
            "        A ballpark shift returns the coordinates essentially "
            "unchanged, so the output would carry the target's label with "
            "the source's values - an error the size of the datum "
            "difference, with no symptom.\n"
            "        If the target has versioned EPSG codes, use one: "
            "EPSG:2956 (NAD83(CSRS) / UTM 12N) is ballpark, EPSG:22812 "
            "(NAD83(CSRS)v8 / UTM 12N) is not. Otherwise export in the "
            "source frame, or pass allow_ballpark=True to accept an "
            "unquantified error."
        )

    logger.info(f"Transformation: {transformer.description}")
    if transformer.accuracy is not None and transformer.accuracy >= 0:
        logger.info(f"Stated accuracy: {transformer.accuracy:.3f} m")
    return transformer


def _enu_basis(lat_deg: np.ndarray, lon_deg: np.ndarray) -> tuple:
    """Unit East/North/Up vectors in ECEF, one per point."""
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    east = np.stack([-np.sin(lon), np.cos(lon), np.zeros_like(lon)], axis=-1)
    north = np.stack([-np.sin(lat) * np.cos(lon),
                      -np.sin(lat) * np.sin(lon),
                      np.cos(lat)], axis=-1)
    up = np.stack([np.cos(lat) * np.cos(lon),
                   np.cos(lat) * np.sin(lon),
                   np.sin(lat)], axis=-1)
    return east, north, up


def _target_jacobian(X, Y, Z, epoch, transformer: Transformer,
                     projected: bool, base_out: tuple) -> np.ndarray:
    """
    Local Jacobian of the transformation, in metres per metre.

    Uncertainty is carried as local ENU standard deviations, so what is needed
    is how a one-metre step East, North and Up at the source point appears in
    the target's own metric axes. Deriving that numerically - shift the point,
    transform it, measure the response - works for any target without needing
    per-projection formulas for scale factor and meridian convergence.

    Parameters
    ----------
    transformer, base_out, projected
        For a projected target, the native transformer and its ``(E, N, h)``
        output, whose axes are already metres. Otherwise the *geographic*
        transformer and its ``(lon, lat, h)`` output, converted to metres
        here. A geocentric target must use the geographic form too: its
        native output is ECEF, which shares no axes with local ENU.

    Returns
    -------
    np.ndarray
        ``(n, 3, 3)`` array; column j is the response to a unit step along
        source ENU axis j.
    """
    east, north, up = _enu_basis(*_source_latlon(X, Y, Z))
    src = np.stack([X, Y, Z], axis=-1)

    columns = []
    for direction in (east, north, up):
        shifted = src + _JACOBIAN_STEP_M * direction
        out = transformer.transform(shifted[:, 0], shifted[:, 1],
                                    shifted[:, 2], epoch)
        if projected:
            # Grid axes are already metres.
            d = np.stack([out[0] - base_out[0],
                          out[1] - base_out[1],
                          out[2] - base_out[2]], axis=-1)
        else:
            # Geographic degrees -> metres in the local tangent plane.
            e, n, u = pm.geodetic2enu(out[1], out[0], out[2],
                                      base_out[1], base_out[0], base_out[2])
            d = np.stack([e, n, u], axis=-1)
        columns.append(d / _JACOBIAN_STEP_M)

    return np.stack(columns, axis=-1)


def _source_latlon(X, Y, Z):
    """Geodetic latitude/longitude of source ECEF, for the ENU basis only."""
    lat, lon, _ = pm.ecef2geodetic(X, Y, Z)
    return np.asarray(lat), np.asarray(lon)


def _resolve_epoch(df: pd.DataFrame, source_epoch: float | None) -> float:
    """
    Decide the epoch to transform at, and refuse to proceed without one.

    Omitting the time coordinate does not fail loudly; PROJ applies the
    transformation as if the point were stationary. At the reference site that
    is a 0.29 m error, identical whether the mistake is made through pyproj or
    through NRCan's own web API.

    Two columns can supply it. ``epoch_decimal_year`` is the machine-readable
    one, but ``geotag()`` keeps only the raw ``epoch`` token in its compact
    output, and that is also the only one that survives a round trip through
    CSV, so the token is parsed when the decimal year is absent.
    """
    if source_epoch is not None:
        return float(source_epoch)

    values = []
    if "epoch_decimal_year" in df.columns:
        values = [float(v) for v in pd.unique(df["epoch_decimal_year"].dropna())]

    if not values and "epoch" in df.columns:
        # Both forms occur: "25:211:68415" from a .sum, "2010.0" from manual
        # entry. _epoch_to_decimal_year handles either.
        from dji_geotagger.ppk.base_position import _epoch_to_decimal_year
        tokens = pd.unique(df["epoch"].dropna())
        parsed = [_epoch_to_decimal_year(str(t)) for t in tokens]
        if any(p is None for p in parsed):
            unparsed = [str(t) for t, p in zip(tokens, parsed) if p is None]
            raise TransformError(
                f"[ERROR] The 'epoch' column could not be read: {unparsed}.\n"
                "        Expected either a CSRS-PPP token such as "
                "'25:211:68415' or a decimal year such as '2010.0'.\n"
                "        Pass source_epoch=<decimal year> to state it directly."
            )
        values = parsed

    if len(values) == 1:
        return values[0]
    if len(values) > 1:
        raise TransformError(
            f"[ERROR] The table mixes {len(values)} reference epochs "
            f"({', '.join(f'{v:.4f}' for v in sorted(values))}).\n"
            "        Those rows came from different base solutions and "
            "cannot share one transformation. Split them, or pass "
            "source_epoch= to force a single epoch."
        )

    raise TransformError(
        "[ERROR] No reference epoch. A datum transformation without one is "
        "applied as if the ground were stationary, which at the reference "
        "site is a 0.29 m error - and it is silent.\n"
        "        Supply source_epoch=<decimal year>, or use a table that "
        "still carries 'epoch' or 'epoch_decimal_year' from geotag()."
    )


def transform_coordinates(
    df: pd.DataFrame,
    target_crs,
    *,
    source_crs=None,
    source_epoch: float | None = None,
    coord_cols: tuple[str, str, str] = ("cam_X", "cam_Y", "cam_Z"),
    transform_sigma: bool = True,
    allow_ballpark: bool = False,
    allow_datum_ensemble: bool = False,
    progress=None,
) -> pd.DataFrame:
    """
    Transform a geotag table into another CRS.

    Handles the datum transformation and the projection. It does **not**
    propagate the epoch: the coordinates come out at the epoch they went in
    at, in the target frame. See Notes.

    Parameters
    ----------
    df : pd.DataFrame
        Output of :func:`~dji_geotagger.core.geotag.geotag`. Must carry the
        three columns named by `coord_cols`, and - unless `source_crs` and
        `source_epoch` are both given - the ``coord_sys`` and
        ``epoch_decimal_year`` columns.
    target_crs : pyproj.CRS or int or str
        Anything ``CRS.from_user_input`` accepts: ``22811``,
        ``"EPSG:22811"``, a WKT string, or a CRS built by
        :func:`make_utm_crs`. **Use versioned realizations** - see Notes.
    source_crs : pyproj.CRS or int or str, optional
        Overrides the frame read from ``coord_sys``.
    source_epoch : float, optional
        Decimal year, overriding ``epoch_decimal_year``.
    coord_cols : tuple of str, default ("cam_X", "cam_Y", "cam_Z")
        Source ECEF columns. Use ``("X", "Y", "Z")`` for antenna rather than
        camera positions.
    transform_sigma : bool, default True
        Rotate and scale the uncertainties into the target frame. Uses a
        numerical Jacobian, so it accounts for meridian convergence and the
        point scale factor. When False the sigma columns are passed through
        untouched and the result is marked accordingly.
    allow_ballpark : bool, default False
        Permit a transformation PROJ cannot do rigorously. The output then
        carries the target's label with roughly the source's values.
    allow_datum_ensemble : bool, default False
        Permit an ensemble target such as plain WGS 84, accepting metre-level
        ambiguity in exchange.
    progress : Progress, optional
        Progress sink; also provides cancellation.

    Returns
    -------
    pd.DataFrame
        A copy. ``cam_lat``/``cam_lon``/``cam_h`` and ``cam_X``/``cam_Y``/
        ``cam_Z`` are replaced with their values in the target frame - never
        left at their source values, which would be indistinguishable from
        correct output. A projected target adds ``cam_E``/``cam_N``.
        ``coord_sys`` becomes the target CRS name.

        ``df.attrs["transform"]`` records source, target, epoch, the PROJ
        operation, its stated accuracy, and the mean and maximum displacement,
        for the provenance sidecar.

    Raises
    ------
    TransformError
        On a missing epoch, an unrecognised source frame, a ballpark
        fallback, or a datum-ensemble CRS. Every one of these is a silent
        failure in PROJ itself, which is why they are raised here.

    Notes
    -----
    **Use versioned EPSG codes.** ``EPSG:2956`` and ``EPSG:22812`` are both
    "NAD83(CSRS) / UTM zone 12N", but the first names no realization, so PROJ
    cannot find a rigorous operation and silently discards the entire 1.63 m
    datum shift. The versioned codes for NAD83(CSRS) UTM run ``222xx`` (v2)
    through ``228xx`` (v8), where ``xx`` is the zone.

    **Epoch propagation is not done here.** Moving a coordinate between epochs
    requires the NAD83 v8.0 velocity grid, which PROJ does not distribute. To
    deliver at a fixed epoch, ask CSRS-PPP for it when the base station is
    processed::

        resolve_base_position(mode="online", base_obs=..., email=...,
                              ppp_kwargs={"sysref": "NAD83",
                                          "nad83_epoch": "NAD83_20100101"})

    That is not merely a workaround. CSRS-PPP returns the propagation
    uncertainty as a second sigma column, which at 15.6 years was 0.75-1.10 cm
    (1-sigma) - the same order as the PPP solution itself, and a term neither
    pyproj nor NRCan's TRX will give you. ``nad83_epoch="NAD83_CURR"`` does
    *not* propagate; it stays at the observation epoch.

    Examples
    --------
    >>> utm = transform_coordinates(geotag_df, 22811)      # NAD83(CSRS)v8 UTM 11N
    >>> utm.attrs["transform"]["shift_3d_mean_m"]
    1.6286...
    """
    progress = as_progress(progress)
    progress.update("transform", "Preparing coordinate transformation")

    missing = [c for c in coord_cols if c not in df.columns]
    if missing:
        raise TransformError(
            f"[ERROR] Missing coordinate columns {missing}. Expected "
            f"{list(coord_cols)}; pass coord_cols= if the table uses "
            "different names."
        )

    epoch = _resolve_epoch(df, source_epoch)

    if source_crs is None:
        if "coord_sys" not in df.columns:
            raise TransformError(
                "[ERROR] No 'coord_sys' column and no source_crs=. The source "
                "frame is not guessed."
            )
        frames = pd.unique(df["coord_sys"].dropna())
        if len(frames) != 1:
            raise TransformError(
                f"[ERROR] The table mixes {len(frames)} source frames "
                f"({', '.join(map(str, frames))}). Split them, or pass "
                "source_crs=."
            )
        source = resolve_source_crs(frames[0])
    else:
        source = CRS.from_user_input(source_crs)

    target = CRS.from_user_input(target_crs)
    logger.info(f"Transforming {source.name} @ {epoch:.4f} -> {target.name}")

    X = df[coord_cols[0]].to_numpy(dtype=float)
    Y = df[coord_cols[1]].to_numpy(dtype=float)
    Z = df[coord_cols[2]].to_numpy(dtype=float)
    valid = np.isfinite(X) & np.isfinite(Y) & np.isfinite(Z)
    if not valid.any():
        raise TransformError(
            "[ERROR] Every coordinate is NaN; there is nothing to transform. "
            "Out-of-coverage exposures are set to NaN by the camera position "
            "step - check the PPK time span first."
        )
    if not valid.all():
        logger.warning(f"{(~valid).sum()} of {len(df)} rows have no position "
                       "and will stay NaN.")

    probe = (X[valid][0], Y[valid][0], Z[valid][0], epoch)
    transformer = _check_pair(source, target,
                              allow_ballpark=allow_ballpark,
                              allow_datum_ensemble=allow_datum_ensemble,
                              probe=probe)
    progress.update("transform", f"Transforming {int(valid.sum())} positions")

    # PROJ requires the time coordinate to match the others in length; a
    # scalar is accepted only for a single point.
    epochs = np.full(len(df), epoch, dtype=float)

    # Native target coordinates, and geographic ones on the same datum. Both
    # are produced for every target so that no column is ever left holding a
    # value from the source frame.
    native = transformer.transform(X, Y, Z, epochs)
    geo_transformer = Transformer.from_crs(source, _geographic3d(target),
                                           always_xy=True)
    lon_t, lat_t, h_t = geo_transformer.transform(X, Y, Z, epochs)[:3]

    out = df.copy()
    out["cam_lat"] = np.where(valid, lat_t, np.nan)
    out["cam_lon"] = np.where(valid, lon_t, np.nan)
    out["cam_h"] = np.where(valid, h_t, np.nan)

    # ECEF on the target datum, from its own ellipsoid. Computing it here
    # rather than dropping the columns keeps every coordinate column valid and
    # mutually consistent, whatever the target type.
    ell = pm.Ellipsoid(semimajor_axis=target.ellipsoid.semi_major_metre,
                       semiminor_axis=target.ellipsoid.semi_minor_metre)
    Xt, Yt, Zt = pm.geodetic2ecef(lat_t, lon_t, h_t, ell=ell)
    out["cam_X"] = np.where(valid, Xt, np.nan)
    out["cam_Y"] = np.where(valid, Yt, np.nan)
    out["cam_Z"] = np.where(valid, Zt, np.nan)

    if target.is_projected:
        out["cam_E"] = np.where(valid, native[0], np.nan)
        out["cam_N"] = np.where(valid, native[1], np.nan)

    # Displacement, so the caller can see how large a change was just made. A
    # transformation that moves nothing is the signature of a ballpark
    # fallback, so this doubles as a sanity check and must read exactly zero
    # when it should - hence comparing ECEF against ECEF rather than going via
    # geodetic coordinates, where the source and target ellipsoids differ.
    dX, dY, dZ = Xt - X, Yt - Y, Zt - Z
    shift = np.sqrt(dX ** 2 + dY ** 2 + dZ ** 2)[valid]
    logger.info(f"Datum shift: mean {shift.mean():.3f} m, "
                f"max {shift.max():.3f} m  "
                f"(dX {np.mean(dX[valid]):+.3f}, dY {np.mean(dY[valid]):+.3f}, "
                f"dZ {np.mean(dZ[valid]):+.3f})")

    sigma_transformed = False
    if transform_sigma:
        # A geocentric target's native axes are ECEF, which share nothing with
        # local ENU, so only a projected target can use the native output.
        if target.is_projected:
            jac_tr = transformer
            jac_base = tuple(np.asarray(native[i]) for i in range(3))
        else:
            jac_tr = geo_transformer
            jac_base = (np.asarray(lon_t), np.asarray(lat_t), np.asarray(h_t))
        sigma_transformed = _apply_sigma(out, X, Y, Z, epochs, jac_tr,
                                         target.is_projected, jac_base, valid)
    if not sigma_transformed:
        logger.warning(
            "Uncertainties were NOT transformed; they still describe the "
            "source frame's axes. They also never include the transformation "
            "error itself.")

    out["coord_sys"] = target.name
    out.attrs = dict(df.attrs)
    out.attrs["transform"] = {
        "source_crs": source.name,
        "source_crs_srs": source.srs,
        "target_crs": target.name,
        "target_crs_srs": target.srs,
        "epoch_decimal_year": epoch,
        "epoch_propagated": False,
        "operation": transformer.description,
        "accuracy_m": transformer.accuracy,
        "shift_3d_mean_m": float(shift.mean()),
        "shift_3d_max_m": float(shift.max()),
        "shift_ecef_mean_m": [float(np.mean(dX[valid])),
                              float(np.mean(dY[valid])),
                              float(np.mean(dZ[valid]))],
        "sigma_transformed": sigma_transformed,
        "sigma_includes_transformation_error": False,
        "ballpark_allowed": allow_ballpark,
        "datum_ensemble_allowed": allow_datum_ensemble,
    }
    progress.update("transform", "Coordinate transformation complete")
    return out


def _apply_sigma(out, X, Y, Z, epoch, transformer, projected, base_out,
                 valid) -> bool:
    """
    Rotate the uncertainties into the target frame in place.

    Returns whether anything was done. Prefers the full ``cov_total_ECEF``
    matrix when the table carries it; falls back to a diagonal built from
    ``sigma_E``/``sigma_N``/``sigma_U``, which loses the cross-terms because
    ``format_output`` already discarded them - not because they are assumed
    to be zero.
    """
    has_cov = "cov_total_ECEF" in out.columns
    has_sigma = all(c in out.columns for c in ("sigma_E", "sigma_N", "sigma_U"))
    if not (has_cov or has_sigma):
        return False

    J = _target_jacobian(X, Y, Z, epoch, transformer, projected, base_out)
    lat_s, lon_s = _source_latlon(X, Y, Z)

    sig_e = np.full(len(out), np.nan)
    sig_n = np.full(len(out), np.nan)
    sig_u = np.full(len(out), np.nan)

    for i in np.flatnonzero(valid):
        cov_enu = None
        if has_cov:
            cov_ecef = out["cov_total_ECEF"].iloc[i]
            if isinstance(cov_ecef, np.ndarray) and cov_ecef.shape == (3, 3):
                from dji_geotagger.tools.tools import ECEF2ENU_vec
                cov_enu = ECEF2ENU_vec(cov_ecef=cov_ecef,
                                       lat_deg=float(lat_s[i]),
                                       lon_deg=float(lon_s[i]))
        if cov_enu is None and has_sigma:
            s = np.array([out["sigma_E"].iloc[i], out["sigma_N"].iloc[i],
                          out["sigma_U"].iloc[i]], dtype=float)
            if np.all(np.isfinite(s)):
                cov_enu = np.diag(s ** 2)
        if cov_enu is None:
            continue

        cov_t = J[i] @ cov_enu @ J[i].T
        sig_e[i], sig_n[i], sig_u[i] = np.sqrt(np.maximum(np.diag(cov_t), 0.0))

    if not np.isfinite(sig_e).any():
        return False

    out["sigma_E"] = sig_e
    out["sigma_N"] = sig_n
    out["sigma_U"] = sig_u
    return True
