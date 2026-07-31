# DJI Geotagger [![Downloads](https://static.pepy.tech/badge/dji-geotagger)](https://pepy.tech/project/dji-geotagger)

**A precise PPK + MRK-based geotagging tool for DJI RTK drones**

This Python library enables centimetre-level camera geotagging by combining PPK `.pos` solutions, DJI `.MRK` gimbal offset corrections, and EXIF/XMP metadata from DJI RTK drone images. It is designed for photogrammetry and remote sensing workflows that require accurate EOPs.

## Features

- Convert raw GNSS logs (`.bin`, `.dat`) to RINEX format using RTKLIB `convbin`
- Download precise ephemeris data (SP3/CLK) automatically from IGS
- Run differential PPK (rover against base) with RTKLIB `rnx2rtkp`
- Resolve the base station position three ways: submit to CSRS-PPP automatically, read an existing `.sum`, or enter known coordinates
- Parse DJI `.MRK` gimbal offset files and apply the lever arm to the camera centre (ECEF)
- Match images by GPS time and propagate full 3×3 covariance from both the rover and the base
- Transform to any CRS with guards against PROJ's silent failure modes
- Batch processing of multiple flight folders, with per-flight error isolation
- Support for DJI P1, M300, and other RTK-enabled drones

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

- Python ≥ 3.9
- `pillow` - Image processing and EXIF reading
- `defusedxml` - Required by Pillow's `getxmp()`; without it XMP is silently unreadable and every image yields no metadata
- `pandas` - Data manipulation and CSV export
- `numpy` - Numerical computations
- `pyproj` - Coordinate reference systems and transformations
- `tqdm` - Progress bars
- `requests` - HTTP requests for ephemeris download and CSRS-PPP submission
- `georinex` - RINEX file parsing
- `astropy` - Time and coordinate utilities
- `pymap3d` - Geodetic coordinate conversions
- `scipy` - Scientific computing (interpolation, linear algebra)
- RTKLIB (`convbin`, `rnx2rtkp`) - Auto-downloaded on first use

## Workflow Overview

1. **Convert raw GNSS to RINEX** - Base station (`.dat`) and rover (`.bin`) logs to standard RINEX
2. **Resolve the base station position** - CSRS-PPP (online or from a `.sum`), or known coordinates
3. **Run PPK batch processing** - `rnx2rtkp` per flight folder, after checking base/rover time overlap
4. **Parse image metadata** - Capture time, gimbal attitude and camera orientation from EXIF/XMP
5. **Parse and interpolate MRK** - Gimbal offset vectors from NED → ECEF
6. **Compute geotagged positions** - Match images to PPK solutions, apply the lever arm, propagate covariance
7. **Optional: transform coordinates** - Datum transformation and projection for delivery
8. **Export CSV**

## Quick Start

```python
import dji_geotagger as dgt

# === 1. Convert GNSS raw data to RINEX ===
base_obs, base_nav = dgt.raw2rinex(
    input_path=r"DRTK3/DRTK3_20250730.dat",
    antenna_height_in_meter=2.0,
)

# === 2. Resolve the base station position ===
#     Submitted to CSRS-PPP and fetched back automatically. A free CSRS
#     account is needed; the email is the only credential involved.
base_position = dgt.resolve_base_position(
    mode="online",
    base_obs=base_obs,
    email="you@example.com",
    ppp_kwargs={"process_type": "Static", "sysref": "ITRF"},
)

# === 3. Define flight folders to process ===
flight_folders = [
    r"P1/DJI_202507301227_011_LOCATION",
    r"P1/DJI_202507301227_012_LOCATION",
    r"P1/DJI_202507301256_013_LOCATION",
]

# === 4. Process all flights ===
geotag_df = dgt.geotag(
    flight_folders=flight_folders,
    base_obs=base_obs,
    base_nav=base_nav,
    base_position=base_position,
)

# === 5. Save ===
geotag_df.to_csv("geotagged_results.csv", index=False)
print(f"Geotagged {len(geotag_df)} images")
```

Resolving the base position before calling `geotag()` — rather than letting
`geotag()` do it — means the coordinates can be checked before committing to a
run that takes minutes per flight, and a bad `.sum` fails immediately instead
of after the first PPK solve.

## Base Station Position

All three sources return the same structure, so the rest of the script is
identical whichever is used.

**Online CSRS-PPP submission** — submits the RINEX, polls, and parses the
returned `.sum`:

```python
base_position = dgt.resolve_base_position(
    mode="online",
    base_obs=base_obs,
    email="you@example.com",
    ppp_kwargs={"process_type": "Static", "sysref": "ITRF"},
)
```

**Existing `.sum` file** — for re-running without resubmitting. Omit
`sum_file_path` to auto-detect a `.sum` sitting next to the `.obs`:

```python
base_position = dgt.resolve_base_position(
    mode="sum",
    sum_file_path=r"DRTK3/PPP/DRTK3_20250730.sum",
)
```

**Known coordinates** — a published control point or CORS station. The height
must be **ellipsoidal**, not orthometric; orthometric heights are refused
rather than silently converted:

```python
base_position = dgt.resolve_base_position(
    mode="manual",
    manual_kwargs=dict(
        lat_dd=51.0000000, lon_dd=-114.0000000, hgt=1000.0000,
        coord_sys="NAD83(CSRS)", epoch="2010.0",
        sigma_ENU=(0.010, 0.010, 0.020),   # metres, 1-sigma
    ),
)
```

`sigma_ENU` is required, and cannot simply be left out. To report rover-only
precision, pass it explicitly as `None`:

```python
manual_kwargs=dict(..., sigma_ENU=None)   # disables base error propagation
```

It is never treated as zero — a base station with no stated uncertainty is not
the same as a perfect one, and the difference has to be stated deliberately
rather than by omission.

### Delivering at a fixed epoch

A coordinate is meaningless without its epoch: in a plate-fixed frame such as
NAD83(CSRS) the North American plate moves 1–2 cm/yr. If the deliverable must
be at a fixed epoch (2010.0, say), ask CSRS-PPP for it at this step:

```python
ppp_kwargs={"sysref": "NAD83", "nad83_epoch": "NAD83_20100101"}
```

This is the **only** step in the pipeline that can propagate an epoch — doing
it later needs the NAD83 v8.0 velocity grid, which PROJ does not distribute.
CSRS-PPP also returns the propagation uncertainty, which for a 15.6-year
propagation was 0.75–1.10 cm (1-sigma), the same order as the PPP solution
itself. That term is folded into the reported covariance automatically.

Note that `"NAD83_CURR"` does **not** propagate; it stays at the observation
epoch.

## Coordinate Transformation

`geotag()` leaves its output in whatever frame CSRS-PPP solved in, tagged with
the reference epoch. Keep that file: the frame + epoch pair is lossless, so any
other CRS can still be derived from it later.

```python
utm_df = dgt.transform_coordinates(geotag_df, 22811)   # NAD83(CSRS)v8 / UTM 11N
utm_df.to_csv("results_utm11n.csv", index=False)

print(utm_df.attrs["transform"])   # provenance: operation, accuracy, shift
```

Uncertainties are rotated into the target frame using a numerical Jacobian, so
meridian convergence and the point scale factor are accounted for without
per-projection formulas.

### Use versioned EPSG codes

`EPSG:2956` and `EPSG:22812` are both named "NAD83(CSRS) / UTM zone 12N", but
the first names no realization. PROJ then cannot find a rigorous operation and
falls back to a *ballpark* shift, which returns the coordinates essentially
unchanged — discarding the entire 1.63 m datum shift with no error and no
symptom. The versioned NAD83(CSRS) UTM codes run `222xx` (v2) through `228xx`
(v8), where `xx` is the zone.

Where EPSG has no versioned code, build the CRS instead:

```python
itrf_utm    = dgt.make_utm_crs(11, 9988)                # no ITRF2020 UTM exists
alberta_3tm = dgt.rebase_projected_crs(3780, 10412)     # 3TM grid, v8 datum
```

### What it refuses, and why

PROJ degrades rather than raising. Three silent failures are refused by
default, each with its own opt-out:

| Refused | Measured cost if allowed | Override |
|---|---|---|
| Ballpark fallback (no rigorous transformation) | full datum shift lost | `allow_ballpark=True` |
| Datum ensemble target, e.g. plain WGS 84 | 1.60 m | `allow_datum_ensemble=True` |
| Missing epoch | 0.29 m | supply `source_epoch=` |

The datum-ensemble case is the least obvious. "WGS 84" is an ensemble with
~2 m internal accuracy, so PROJ has several candidate operations whose stated
accuracies all cluster near 2 m and picks the numerically smallest — which at
the reference site routed through NAD83(2011) and shifted the point 1.60 m,
flagged as neither ballpark nor low-accuracy.

There is also rarely a reason to ask for it: WGS 84 (G2296) is aligned to
ITRF2020 at the centimetre level, so the `cam_lat`/`cam_lon` already produced
by `geotag()` *are* WGS 84 — more precisely than WGS 84's own definition.

### Verification

Checked against CSRS-PPP's own answers for the same base station, by
submitting the same RINEX with `sysref="ITRF"` and `sysref="NAD83"`:

| Step | Agreement |
|---|---|
| Datum transformation (EPSG:9988 → 10412) vs a native NAD83 solve | 0.055 mm |
| Projection vs the `PRJ` line in the `.sum`, both frames | exact to the printed mm |
| Jacobian scale factor vs the `.sum`'s `SCALE_COMBINED` | 1e-7 |

## Output Format

By default `geotag()` returns a compact table. Pass `full_output=True` for all
intermediate columns (MRK fields, EXIF/XMP metadata, aircraft and gimbal
attitude, the full covariance matrices).

### Default columns

| Column | Description |
|--------|-------------|
| `FileName` | Image filename |
| `UTCAtExposure` | UTC datetime of exposure |
| `coord_sys` | Reference frame (e.g. `IGb20`) |
| `epoch` | Reference epoch of the coordinates |
| `cam_lat`, `cam_lon` | Camera centre latitude/longitude (decimal degrees) |
| `cam_h` | Camera centre **ellipsoidal** height (metres) |
| `cam_X`, `cam_Y`, `cam_Z` | Camera centre ECEF (metres) |
| `sigma_E`, `sigma_N`, `sigma_U` | 1-sigma uncertainty, local ENU (metres) |
| `DGT_YawDegree`, `DGT_PitchDegree`, `DGT_RollDegree` | Camera attitude (degrees) |
| `rtk_status` | RTK solution status (`Fixed`, `Float`, `Single`, `Unknown`) |

After `transform_coordinates()` every coordinate column is replaced with its
value in the target frame — never left holding a source-frame value — and a
projected target adds `cam_E` and `cam_N`.

### Additional columns with `full_output=True`

| Group | Columns |
|---|---|
| Sequence & time | `seq`, `GPS_time`, `GPS_week` |
| Antenna position | `X`, `Y`, `Z`, `lat_dd`, `lon_dd`, `hgt` |
| Covariance | `cov_total_ECEF` (3×3, m²), `sigma_total_ECEF` |
| Provenance | `epoch_decimal_year`, `base_source`, `cov_repaired` |
| Lever arm | `gimbal_dN/dE/dD` (NED), `gimbal_dX/dY/dZ` (ECEF) |
| Aircraft attitude | `FlightYawDegree`, `FlightPitchDegree`, `FlightRollDegree` |
| Gimbal attitude | `GimbalYawDegree`, `GimbalPitchDegree`, `GimbalRollDegree` |
| EXIF/XMP | `GpsLatitude`, `GpsLongitude`, `AbsoluteAltitude` |
| Grouping | `flight` (flight folder name) |

Flights that failed are recorded in `geotag_df.attrs["failed_flights"]`, so an
incomplete result does not have to be discovered by scraping the log.

## Covariance / Uncertainty Model

The reported per-image uncertainty (`sigma_E/N/U`, `cov_total_ECEF`) combines **two independent
error sources as full 3×3 covariance matrices in ECEF**, then rotates the result into the local
ENU frame:

$$\Sigma_{\text{total}} = \Sigma_{\text{PPK}} + \Sigma_{\text{PPP}}, \qquad \Sigma_{\text{ENU}} = R\,\Sigma_{\text{total}}\,R^{\top}$$

- **PPK (rover relative)** — $\Sigma_{\text{PPK}}$, per-epoch positioning precision from the RTKLIB `.pos` solution.
- **PPP (base absolute)** — $\Sigma_{\text{PPP}}$, base station precision from the CSRS-PPP `.sum` file, including the epoch-propagation term when one applies.
- $R$ — the ECEF → ENU rotation (Jacobian) at the epoch's latitude/longitude.

Working at the covariance-matrix level (rather than adding scalar variances) preserves the
inter-axis correlations. Disable the base term with `base_error_propagation_on=False` to report
rover-only precision.

### ⚠️ The reported sigma is slightly optimistic

It accounts for **PPK + PPP only**, so treat it as a lower bound on the true uncertainty. A few
real error sources are **not** propagated into the reported values:

- **Linear interpolation** — the PPK trajectory is interpolated linearly to each exposure time, and
  covariance is copied from the nearest epoch. Any acceleration between GNSS epochs adds a small
  error (near zero on straight lines, up to a few cm during turns/speed changes, growing with
  flight speed).
- **Exposure time-sync** — a small camera/GNSS clock offset shifts the position by roughly
  speed × offset.
- **Lever-arm / gimbal** — the MRK offset vector is applied as if error-free.
- **Coordinate transformation** — `transform_coordinates()` rotates the uncertainty into the
  target frame but does not add the transformation's own error.

### RTKLIB covariance repair

A small fraction of RTKLIB `.pos` epochs carry correlation values outside
[−1, 1], making the implied covariance matrix indefinite — which cannot be
inverted, so it cannot be used as a bundle-adjustment weight. `pos2df()`
detects these by eigenvalue test and, with `fix_bad_covariance=True` (the
default), replaces the matrix with the nearest valid epoch's. Positions are
never altered, and repaired epochs are flagged in `cov_repaired`.

## Key Functions

### Core
- **`geotag(flight_folders, base_obs, base_nav, ...)`** - Batch process multiple flight folders (recommended)
- **`compute_camera_position(pos_df, mrk_df, img_df, ...)`** - Camera centres for one flight

### Raw data & PPK
- **`raw2rinex(input_path, ...)`** - Convert `.bin`/`.dat` to RINEX `.obs` and `.nav`
- **`process_ppk(base_obs, base_nav, rover_obs, ...)`** - Differential PPK with RTKLIB
- **`pos2df(pos_file, ...)`** - Parse an RTKLIB `.pos` solution, with covariance validation
- **`mrk2df(mrk_file)`** - Parse a DJI `.MRK` file
- **`parse_img_dir(flight_dir)`** - Read EXIF/XMP from a flight folder

### Base station
- **`resolve_base_position(mode, ...)`** - `"online"`, `"sum"` or `"manual"`
- **`build_base_position(...)`** - Build the structure from known coordinates
- **`run_online_ppp(rinex_path, email, out_dir, ...)`** - Submit, wait, download
- **`sum_file_parser(...)`** - Parse a CSRS-PPP `.sum`

### Coordinate transformation
- **`transform_coordinates(df, target_crs, ...)`** - Datum transformation and projection
- **`make_utm_crs(zone, datum_crs)`** - UTM on a datum EPSG has no code for
- **`rebase_projected_crs(projected_crs, datum_crs)`** - Any projection, on a chosen datum
- **`resolve_source_crs(coord_sys)`** - Frame token (`"IGb20"`, `"NAD83"`) → CRS

### Infrastructure
- **`ensure_rtklib(auto_install=True)`** - Download RTKLIB binaries
- **`configure_logging(...)`** - Route output to a file, a GUI, or silence it
- **`Progress`**, **`OperationCancelled`** - Progress reporting and cooperative cancellation

Two helpers are not re-exported at the top level:

```python
from dji_geotagger.ppk.ephemeris_downloader import download_igs_data
from dji_geotagger.config.import_config import override_rtklib_config
```

## Configuration

### RTKLIB settings

Defaults are in `dji_geotagger/config/default_ppk_dict.py`. Override with:

```python
user_config = {
    'pos1-posmode': 'kinematic',
    'pos1-frequency': '1',
    'pos1-soltype': 'forward',
}

dgt.process_ppk(base_obs, base_nav, rover_obs, user_conf=user_config)
```

### Progress and cancellation

`geotag()`, `process_ppk()` and `raw2rinex()` accept a `progress` argument. It
also carries cancellation, which interrupts long waits and terminates RTKLIB
subprocesses rather than waiting for them:

```python
cancelled = False

progress = dgt.Progress(
    on_progress=lambda e: print(f"{e.stage}: {e.message}"
                                + (f" ({e.fraction:.0%})" if e.fraction else "")),
    should_cancel=lambda: cancelled,
)

try:
    geotag_df = dgt.geotag(..., progress=progress)
except dgt.OperationCancelled:
    print("Cancelled")
```

An exception raised inside `on_progress` is suppressed — a faulty display must
not abort a long computation.

### Logging

Console logging is configured on import so existing scripts print what they
always printed. To silence it or route it elsewhere, attach your own handler
to the `dji_geotagger` logger:

```python
import logging

dgt.configure_logging(console=False)          # silence the console handler
logging.getLogger("dji_geotagger").addHandler(logging.FileHandler("run.log"))
```

## Troubleshooting

### RTKLIB not found
Binaries are downloaded on first use. To do it up front:
```python
dgt.ensure_rtklib()
```

### No CSRS-PPP account
Use `mode="manual"` with a surveyed position. `sigma_ENU` is a required
argument; pass `None` to disable base error propagation. It is never assumed
to be zero.

### Base and rover do not overlap in time
PPK is differential, so epochs outside the base station's span cannot be
solved. The overlap is checked before RTKLIB runs; partial coverage warns and
continues, no overlap raises. Disable with `check_overlap=False`.

### One flight fails in a batch
`geotag()` skips it and continues by default, listing failures in
`attrs["failed_flights"]`. Pass `on_flight_error="raise"` to stop on the first
failure instead.

### Image-time mismatch
Ensure:
- Camera clock is synchronized within ±1 second of GPS
- EXIF/XMP timestamps are in UTC (not local time)
- MRK files cover the same time period as images

### Every image reports no metadata
`defusedxml` is missing. Pillow's `getxmp()` needs it and fails silently
without it.

## Performance Tips

- Use **IGS Rapid orbits** (available ~17–18 hours after end-of-day UTC) for faster processing
- Process multiple flights with `geotag([flight1, flight2, ...])` for efficiency
- For large datasets, filter low-confidence solutions using covariance thresholds

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
