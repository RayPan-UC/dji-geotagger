# DJI Geotagger [![Downloads](https://static.pepy.tech/badge/dji-geotagger)](https://pepy.tech/project/dji-geotagger)

**A precise PPK + MRK-based geotagging tool for DJI RTK drones**

This Python library enables centimetre-level camera geotagging by combining PPK `.pos` solutions, DJI `.MRK` gimbal offset corrections, and EXIF/XMP metadata from DJI RTK drone images. It is designed for photogrammetry and remote sensing workflows that require accurate EOPs.

## Features

- Convert raw GNSS logs (`.bin`, `.dat`) to RINEX using RTKLIB `convbin`
- Download precise ephemeris (SP3/CLK) automatically from IGS
- Run differential PPK (rover against base) with RTKLIB `rnx2rtkp`
- Resolve the base station position from CSRS-PPP (submitted automatically or from a `.sum`) or known coordinates
- Apply the DJI `.MRK` lever arm to the camera centre, propagating full 3×3 covariance
- Transform to any CRS, with guards against PROJ's silent failure modes
- Batch multiple flight folders with per-flight error isolation

## Installation

```bash
pip install dji-geotagger
```

Or from source:
```bash
git clone https://github.com/geo-raypan/dji-geotagger.git
cd dji-geotagger
pip install -e .
```

## Dependencies

Python ≥ 3.11, plus `pillow`, `defusedxml`, `pandas`, `numpy`, `pyproj`, `tqdm`,
`requests`, `georinex`, `astropy`, `pymap3d`.

RTKLIB (`convbin`, `rnx2rtkp`) is downloaded automatically on first use.

## Quick Start

```python
import dji_geotagger as dgt

# 1. Convert GNSS raw data to RINEX
base_obs, base_nav = dgt.raw2rinex(
    input_path=r"DRTK3/DRTK3_20250730.dat",
    antenna_height_in_meter=2.0,
)

# 2. Resolve the base station position (submitted to CSRS-PPP, fetched back)
base_position = dgt.resolve_base_position(
    mode="online",
    base_obs=base_obs,
    email="you@example.com",
    ppp_kwargs={"process_type": "Static", "sysref": "ITRF"},
)

# 3. Process all flights
geotag_df = dgt.geotag(
    flight_folders=[
        r"P1/DJI_202507301227_011_LOCATION",
        r"P1/DJI_202507301227_012_LOCATION",
    ],
    base_obs=base_obs,
    base_nav=base_nav,
    base_position=base_position,
)

geotag_df.to_csv("geotagged_results.csv", index=False)
```

## Base Station Position

All three modes return the same structure, so the rest of the script is
unchanged whichever is used.

```python
# Submit to CSRS-PPP and fetch the .sum back (no account needed)
dgt.resolve_base_position(mode="online", base_obs=base_obs,
                          email="you@example.com")

# An existing .sum - omit sum_file_path to auto-detect one next to the .obs
dgt.resolve_base_position(mode="sum", sum_file_path=r"DRTK3/PPP/base.sum")

# Known coordinates. hgt must be ELLIPSOIDAL; orthometric heights are refused.
dgt.resolve_base_position(mode="manual", manual_kwargs=dict(
    lat_dd=51.0, lon_dd=-114.0, hgt=1000.0,
    coord_sys="NAD83(CSRS)", epoch="2010.0",
    sigma_ENU=(0.010, 0.010, 0.020),   # metres, 1-sigma; None to disable
))
```

`sigma_ENU` must be given explicitly. Pass `None` to report rover-only
precision — it is never assumed to be zero.

**To deliver at a fixed epoch**, ask CSRS-PPP for it here:

```python
ppp_kwargs={"sysref": "NAD83", "nad83_epoch": "NAD83_20100101"}
```

This is the only step that can propagate an epoch, and it returns the
propagation uncertainty too. `"NAD83_CURR"` does *not* propagate.

## Coordinate Transformation

`geotag()` leaves coordinates in the frame CSRS-PPP solved in, tagged with the
reference epoch. That pair is lossless, so keep it — any other CRS can be
derived from it later.

```python
utm_df = dgt.transform_coordinates(geotag_df, 22811)   # NAD83(CSRS)v8 / UTM 11N
print(utm_df.attrs["transform"])                        # provenance
```

Uncertainties are rotated into the target frame, accounting for meridian
convergence and the point scale factor.

**Use versioned EPSG codes.** `EPSG:2956` and `EPSG:22812` are both named
"NAD83(CSRS) / UTM zone 12N", but the first names no realization, so PROJ
falls back to a ballpark shift and discards the datum shift entirely. The
versioned NAD83(CSRS) UTM codes run `222xx` (v2) to `228xx` (v8).

Where EPSG has no versioned code, build the CRS:

```python
itrf_utm    = dgt.make_utm_crs(11, 9988)             # no ITRF2020 UTM exists
alberta_3tm = dgt.rebase_projected_crs(3780, 10412)  # 3TM grid, v8 datum
```

PROJ degrades silently rather than raising, so three cases are refused by
default:

| Refused | Override |
|---|---|
| Ballpark fallback (no rigorous transformation exists) | `allow_ballpark=True` |
| Datum ensemble target, e.g. plain WGS 84 | `allow_datum_ensemble=True` |
| Missing epoch | supply `source_epoch=` |

Note that `cam_lat`/`cam_lon` are already WGS 84 for practical purposes, so
asking for it is rarely necessary.

## Output Format

`geotag()` returns a compact table by default; `full_output=True` adds all
intermediate columns.

| Column | Description |
|--------|-------------|
| `FileName` | Image filename |
| `UTCAtExposure` | UTC datetime of exposure |
| `coord_sys`, `epoch` | Reference frame and epoch |
| `cam_lat`, `cam_lon`, `cam_h` | Camera centre, **ellipsoidal** height (metres) |
| `cam_X`, `cam_Y`, `cam_Z` | Camera centre ECEF (metres) |
| `sigma_E`, `sigma_N`, `sigma_U` | 1-sigma uncertainty, local ENU (metres) |
| `DGT_YawDegree`, `DGT_PitchDegree`, `DGT_RollDegree` | Camera attitude (degrees) |
| `rtk_status` | `Fixed`, `Float`, `Single` or `Unknown` |

With `full_output=True`: `seq`, `GPS_time`, `GPS_week`, antenna position
(`X/Y/Z`, `lat_dd/lon_dd/hgt`), `cov_total_ECEF`, `sigma_total_ECEF`,
`epoch_decimal_year`, `base_source`, `cov_repaired`, the lever arm
(`gimbal_dN/dE/dD`, `gimbal_dX/dY/dZ`), aircraft and gimbal attitude,
EXIF/XMP fields, and `flight`.

After `transform_coordinates()` every coordinate column holds its value in the
target frame, and a projected target adds `cam_E`/`cam_N`.

Skipped flights are listed in `geotag_df.attrs["failed_flights"]`.

## Covariance / Uncertainty Model

The reported per-image uncertainty combines **two independent error sources as
full 3×3 covariance matrices in ECEF**, then rotates the result into local ENU:

$$\Sigma_{\text{total}} = \Sigma_{\text{PPK}} + \Sigma_{\text{PPP}}, \qquad \Sigma_{\text{ENU}} = R\,\Sigma_{\text{total}}\,R^{\top}$$

- $\Sigma_{\text{PPK}}$ — per-epoch rover precision from the RTKLIB `.pos` solution
- $\Sigma_{\text{PPP}}$ — base station precision from the CSRS-PPP `.sum`, including the epoch-propagation term when one applies
- $R$ — the ECEF → ENU rotation at the epoch's latitude/longitude

Working at the matrix level preserves inter-axis correlations. Set
`base_error_propagation_on=False` for rover-only precision.

### ⚠️ The reported sigma is slightly optimistic

Treat it as a lower bound. Not propagated: linear interpolation between GNSS
epochs, camera/GNSS clock offset, lever-arm error, and the coordinate
transformation's own error.

A small fraction of RTKLIB epochs report an indefinite covariance matrix,
which cannot be used as a bundle-adjustment weight. `pos2df()` detects these
and substitutes the nearest valid epoch's matrix (`fix_bad_covariance=True`,
the default); positions are never altered and repaired epochs are flagged in
`cov_repaired`.

## Key Functions

**Core** — `geotag()`, `compute_camera_position()`

**Raw data & PPK** — `raw2rinex()`, `process_ppk()`, `pos2df()`, `mrk2df()`,
`parse_img_dir()`

**Base station** — `resolve_base_position()`, `build_base_position()`,
`run_online_ppp()`, `sum_file_parser()`

**Coordinate transformation** — `transform_coordinates()`, `make_utm_crs()`,
`rebase_projected_crs()`, `resolve_source_crs()`

**Infrastructure** — `ensure_rtklib()`, `configure_logging()`, `Progress`,
`OperationCancelled`

Two helpers are not re-exported:

```python
from dji_geotagger.ppk.ephemeris_downloader import download_igs_data
from dji_geotagger.config.import_config import override_rtklib_config
```

## Configuration

RTKLIB defaults are in `dji_geotagger/config/default_ppk_dict.py`. Override
with `user_conf`:

```python
dgt.process_ppk(base_obs, base_nav, rover_obs,
                user_conf={'pos1-posmode': 'kinematic'})
```

`geotag()`, `process_ppk()` and `raw2rinex()` accept `progress`, which also
carries cancellation:

```python
progress = dgt.Progress(on_progress=lambda e: print(e.message),
                        should_cancel=lambda: stop_requested)
try:
    dgt.geotag(..., progress=progress)
except dgt.OperationCancelled:
    ...
```

Console logging is configured on import; `dgt.configure_logging(console=False)`
silences it.

## Troubleshooting

**RTKLIB not found** — run `dgt.ensure_rtklib()` up front.

**No CSRS-PPP account** — use `mode="manual"` with a surveyed position.

**Base and rover do not overlap in time** — PPK is differential, so epochs
outside the base station's span cannot be solved. Checked before RTKLIB runs;
disable with `check_overlap=False`.

**One flight fails in a batch** — skipped by default and listed in
`attrs["failed_flights"]`. Use `on_flight_error="raise"` to stop instead.

**Image-time mismatch** — check the camera clock is within ±1 s of GPS, that
EXIF/XMP timestamps are UTC, and that MRK files cover the same period.

**Every image reports no metadata** — `defusedxml` is missing; Pillow's
`getxmp()` needs it and fails silently without it.

## Performance Tips

- Use IGS Rapid orbits (available ~17–18 hours after end-of-day UTC)
- Process multiple flights in one `geotag()` call
- Filter low-confidence solutions using covariance thresholds

## References

- **RTKLIB**: https://www.rtklib.com/
- **CSRS-PPP**: https://webapp.geod.nrcan.gc.ca/geod/tools-outils/ppp.php
- **IGS Data**: https://www.igs.org/products/
- **DJI Documentation**: https://enterprise.dji.com/

## License

This project is licensed under the BSD 2-Clause (see LICENSE for details).

## Acknowledgments

- Developed at the University of Calgary, Applied Geospatial Research Group ([appliedgrg.ca](https://www.appliedgrg.ca))
- Inspired by real-world field workflows involving DJI Matrice 350 RTK + Zenmuse P1, Hemisphere base stations, and CSRS-PPP post-processing
- RTKLIB by Tomoji Takasu
