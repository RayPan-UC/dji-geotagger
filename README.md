# DJI Geotagger [![Downloads](https://static.pepy.tech/badge/dji-geotagger)](https://pepy.tech/project/dji-geotagger)

**A precise PPK + MRK-based geotagging tool for DJI RTK drones**

This Python library enables centimetre-level camera geotagging by combining PPK `.pos` solutions, DJI `.MRK` gimbal offset corrections, and EXIF/XMP metadata from DJI RTK drone images. It is designed for photogrammetry and remote sensing workflows that require accurate EOPs.

## Features

- Convert raw GNSS logs (`.bin`, `.dat`) to RINEX format using RTKLIB `convbin`
- Download precise ephemeris data (SP3/CLK) automatically from IGS
- Perform precise point positioning (PPK) with optional base station refinement from CSRS-PPP `.sum` files
- Parse DJI `.MRK` gimbal offset files and interpolate corrections to camera center (ECEF)
- Match images by GPS time, apply PPK + MRK corrections with full covariance propagation
- Export geotagged results in ECEF/ENU/UTM with estimated 3D precision (1-sigma)
- Support for DJI P1, M300, and other RTK-enabled drones
- Batch processing of multiple flight folders

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
- `pandas` - Data manipulation and CSV export
- `numpy` - Numerical computations
- `tqdm` - Progress bars
- `requests` - HTTP requests for ephemeris download
- `georinex` - RINEX file parsing
- `astropy` - Time and coordinate utilities
- `pymap3d` - Geodetic coordinate conversions
- `scipy` - Scientific computing (interpolation, linear algebra)
- RTKLIB (`convbin`, `rnx2rtkp`) - Auto-downloaded on first use

## Workflow Overview

1. **Convert raw GNSS to RINEX** - Convert base station (`.dat`) and rover (`.bin`) logs to standard RINEX format
2. **Optional: Download precise ephemeris** - Automatic IGS SP3/CLK download based on observation dates
3. **Optional: Resolve base station position** - Use CSRS-PPP `.sum` file or manual ECEF coordinates
4. **Run PPK batch processing** - Execute `rnx2rtkp` for each flight folder
5. **Parse image metadata** - Extract capture time, gimbal attitude, and camera orientation from EXIF/XMP
6. **Parse and interpolate MRK** - Convert gimbal offset vectors from NED → ENU → ECEF
7. **Compute geotagged positions** - Match images to PPK solutions, apply MRK offset, propagate covariance
8. **Export CSV** - Generate timestamped CSV with positions, attitudes, and uncertainties

## Example Usage

### Basic Workflow

```python
import dji_geotagger as dgt

# === 1. Convert GNSS raw data to RINEX ===
base_obs, base_nav = dgt.raw2rinex(
    input_path=r"path/to/base/DRTK3_20250730.dat",
    antenna_height_in_meter=2.0
)

# === 2. (Optional) Use PPP base position from CSRS-PPP ===
ppp_sum_file = r"path/to/DRTK3_20250730.sum"  # or leave as None for manual base position

# === 3. Define flight folders to process ===
flight_folders = [
    r"P1/DJI_202507301227_011_LOCATION",
    r"P1/DJI_202507301227_012_LOCATION",
    r"P1/DJI_202507301256_013_LOCATION"
]

# === 4. Process all flights at once ===
geotag_df = dgt.geotag(
    flight_folders=flight_folders,
    base_obs=base_obs,
    base_nav=base_nav,
    sum_file_path=ppp_sum_file,  # Optional CSRS-PPP base position
    output_dir="output/geotags"
)

# === 5. Save to CSV ===
geotag_df.to_csv("geotagged_results.csv", index=False)
print(f"Geotagged {len(geotag_df)} images")
```

### Advanced: Manual Base Station Position

If you don't have a CSRS-PPP `.sum` file, provide base station coordinates manually:

```python
import dji_geotagger as dgt

user_config = {
    'ant2-postype': 'xyz',
    'ant2-pos1': -2418456.789,  # X (metres), ECEF
    'ant2-pos2':  5385936.123,  # Y (metres), ECEF
    'ant2-pos3':  2405716.456,  # Z (metres), ECEF
}

geotag_df = dgt.geotag(
    flight_folders=flight_folders,
    base_obs=base_obs,
    base_nav=base_nav,
    user_conf=user_config  # Use manual base position instead of sum_file_path
)
```

### Step-by-Step: Manual Control Over Each Flight

For detailed control and debugging of the geotagging pipeline for each flight:

```python
import dji_geotagger as dgt
from pathlib import Path

# === Setup ===
base_obs, base_nav = dgt.raw2rinex(
    r"DRTK3/DRTK3_0038_20250730102537.dat", 
    antenna_height_in_meter=2.0
)
ppp_sum_file = r"DRTK3/PPP/DRTK3_0038_20250730102537.sum"

flight_folders = [
    r"P1/DJI_202507301227_011_LOCATION",
    r"P1/DJI_202507301227_012_LOCATION",
]

# === Process each flight manually ===
all_results = []

for flight_dir in flight_folders:
    flight_dir = Path(flight_dir)
    print(f"\n=== Processing {flight_dir.stem} ===")
    
    # Step 1: Find and convert rover GNSS raw → RINEX
    rover_raws = list(flight_dir.glob("*_PPKRAW.bin"))
    if not rover_raws:
        print(f"No *_PPKRAW.bin found in {flight_dir}, skipping...")
        continue
    
    rover_raw = rover_raws[0]
    print(f"Converting {rover_raw.name}...")
    rover_obs, rover_nav = dgt.raw2rinex(rover_raw)
    
    # Step 2: Run PPK solver
    print(f"Running PPK for {flight_dir.stem}...")
    pos_df = dgt.process_ppk(
        base_obs=base_obs,
        base_nav=base_nav,
        rover_obs=rover_obs,
        sum_file_path=ppp_sum_file
    )
    print(f"✓ PPK solution: {len(pos_df)} poses")
    
    # Step 3: Parse MRK gimbal offsets
    mrks = list(flight_dir.glob("*.MRK"))
    if not mrks:
        print(f"No *.MRK found in {flight_dir}, skipping...")
        continue
    
    mrk = mrks[0]
    print(f"Parsing {mrk.name}...")
    mrk_df = dgt.mrk2df(mrk)
    print(f"✓ MRK data: {len(mrk_df)} exposure records")
    
    # Step 4: Parse image metadata (XMP/EXIF)
    print(f"Parsing image metadata from {flight_dir.name}...")
    img_df = dgt.parse_img_dir(flight_dir)
    print(f"✓ Images found: {len(img_df)}")
    
    # Step 5: Compute camera center positions
    print(f"Computing geotagged camera positions...")
    result = dgt.compute_camera_position(
        pos_df=pos_df,
        mrk_df=mrk_df,
        img_df=img_df,
        full_output=False  # Set to True for all intermediate columns
    )
    result["flight"] = flight_dir.stem
    print(f"✓ Geotagged {len(result)} images")
    
    all_results.append(result)

# === Combine all flights ===
if all_results:
    geotag_df = dgt.pd.concat(all_results, ignore_index=True)
    print(f"\n{'='*50}")
    print(f"Total geotagged images: {len(geotag_df)}")
    print(f"Flights processed: {geotag_df['flight'].nunique()}")
    
    # === Transform to UTM ===
    print(f"\nTransforming to UTM Zone 12N...")
    geotag_df = dgt.transform_coordinates(
        geotag_df,
        target_crs=32612,
        cov_ecef2enu=True,
        drop_original=False
    )
    
    # === Export ===
    geotag_df.to_csv("LOCATION_manual.csv", index=False)
    print(f"✓ Results saved to LOCATION_manual.csv")
else:
    print("No flights were successfully processed.")
```

### Debugging: Inspect Intermediate Results

```python
import dji_geotagger as dgt
import pandas as pd

# After running PPK
print("PPK Solution Statistics:")
print(f"  X range: {pos_df['x'].min():.2f} to {pos_df['x'].max():.2f} m")
print(f"  Y range: {pos_df['y'].min():.2f} to {pos_df['y'].max():.2f} m")
print(f"  Z range: {pos_df['z'].min():.2f} to {pos_df['z'].max():.2f} m")
print(f"  Std Dev X: {pos_df['sx'].mean():.4f} m")
print(f"  Std Dev Y: {pos_df['sy'].mean():.4f} m")
print(f"  Std Dev Z: {pos_df['sz'].mean():.4f} m")

# Inspect MRK data
print("\nMRK Gimbal Offsets (first 5 records):")
print(mrk_df[['gps_week', 'gps_tow', 'offset_x', 'offset_y', 'offset_z']].head())

# Inspect image metadata
print("\nImage Metadata (first 5 images):")
print(img_df[['file_name', 'gps_tow', 'roll', 'pitch', 'yaw']].head())

# Inspect final geotagged results
print("\nGeotagged Results (first 5 images):")
print(result[['file_name', 'x_ecef', 'y_ecef', 'z_ecef', 'sd_x_ecef', 'sd_y_ecef', 'sd_z_ecef']].head())
```

## Output Format

The output CSV contains the following columns (aligned with your actual flight data):

### Sequence & Time Information
| Column | Description |
|--------|-------------|
| `seq` | Sequence number of the image |
| `GPS_time` | GPS time-of-week (seconds) |
| `GPS_week` | GPS week number |
| `UTCAtExposure` | UTC datetime of exposure |
| `FileName` | Image filename |

### ECEF Coordinates (IGb20)
| Column | Description |
|--------|-------------|
| `cam_X` | Camera center ECEF X (metres) |
| `cam_Y` | Camera center ECEF Y (metres) |
| `cam_Z` | Camera center ECEF Z (metres) |
| `cov_total_ECEF` | Full 3×3 ECEF covariance matrix (m²) |
| `sigma_total_ECEF` | Diagonal covariances [σX, σY, σZ] (metres) |
| `coord_sys` | Reference frame (IGb20) |

### Geodetic Coordinates (Lat/Lon/Height)
| Column | Description |
|--------|-------------|
| `cam_lat` | Camera latitude (decimal degrees) |
| `cam_lon` | Camera longitude (decimal degrees) |
| `cam_h` | Camera altitude WGS84 ellipsoid (metres) |

### Positional Uncertainties (1-sigma)
| Column | Description |
|--------|-------------|
| `sigma_E` | Uncertainty in East direction (metres) |
| `sigma_N` | Uncertainty in North direction (metres) |
| `sigma_U` | Uncertainty in Up direction (metres) |

### RTK Solution Quality
| Column | Description |
|--------|-------------|
| `rtk_status` | RTK solution status ("Fixed", "Float", etc.) |
| `stddev` | Position standard deviation (metres) |

### Gimbal/DJI Offsets (NED frame)
| Column | Description |
|--------|-------------|
| `gimbal_dN` | Gimbal offset North (metres) |
| `gimbal_dE` | Gimbal offset East (metres) |
| `gimbal_dD` | Gimbal offset Down (metres) |
| `gimbal_dX` | Gimbal offset ECEF X (metres) |
| `gimbal_dY` | Gimbal offset ECEF Y (metres) |
| `gimbal_dZ` | Gimbal offset ECEF Z (metres) |

### Aircraft Attitude (Body Frame)
| Column | Description |
|--------|-------------|
| `FlightYawDegree` | Aircraft body yaw (degrees from North) |
| `FlightPitchDegree` | Aircraft body pitch (degrees) |
| `FlightRollDegree` | Aircraft body roll (degrees) |

### Gimbal Attitude (Stabilized)
| Column | Description |
|--------|-------------|
| `GimbalYawDegree` | Gimbal-reported yaw (degrees) |
| `GimbalPitchDegree` | Gimbal-reported pitch (degrees, -90=nadir) |
| `GimbalRollDegree` | Gimbal-reported roll (degrees) |

### Camera Attitude (Photogrammetry Angles)
| Column | Description |
|--------|-------------|
| `DGT_YawDegree` | Camera yaw (degrees) |
| `DGT_PitchDegree` | Camera pitch (degrees) |
| `DGT_RollDegree` | Camera roll (degrees) |

### Image Metadata (from EXIF/XMP)
| Column | Description |
|--------|-------------|
| `GpsLatitude` | Image EXIF GPS latitude (decimal degrees) |
| `GpsLongitude` | Image EXIF GPS longitude (decimal degrees) |
| `AbsoluteAltitude` | Image EXIF absolute altitude (metres) |

### Full ECEF Covariance (Alternative Storage)
| Column | Description |
|--------|-------------|
| `X`, `Y`, `Z` | Alternative ECEF coordinates (metres) |
| `cov_ecef_flat` | Flattened 3×3 covariance as nested array |

### Flight Grouping
| Column | Description |
|--------|-------------|
| `flight` | Flight folder name (Path.stem) for filtering |

---

### Example Interpretation

For a single row in the output CSV:
```
seq=1, GPS_time=325923.89, GPS_week=2377, FileName=DJI_20250730123142_0001.JPG
cam_X=-1516923.93, cam_Y=-3308803.73, cam_Z=5220764.04 (ECEF coords)
cam_lat=55.2959°, cam_lon=-114.6291°, cam_h=678.89 m (geodetic)
sigma_E=0.0076 m, sigma_N=0.0112 m, sigma_U=0.0290 m (uncertainties)
DGT_YawDegree=91.2°, DGT_PitchDegree=0.1°, DGT_RollDegree=0.0° (camera angles)
```

The **camera center position** (`cam_X/Y/Z`, `cam_lat/lon/h`) is the final geotagged location after:
1. Interpolating PPK solution to exposure time
2. Applying gimbal offset correction (NED → ECEF)
3. Propagating full covariance through transformations

## Covariance / Uncertainty Model

The reported per-image uncertainty (`sigma_E/N/U`, `cov_total_ECEF`) combines **two independent
error sources as full 3×3 covariance matrices in ECEF**, then rotates the result into the local
ENU frame:

$$\Sigma_{\text{total}} = \Sigma_{\text{PPK}} + \Sigma_{\text{PPP}}, \qquad \Sigma_{\text{ENU}} = R\,\Sigma_{\text{total}}\,R^{\top}$$

- **PPK (rover relative)** — $\Sigma_{\text{PPK}}$, per-epoch positioning precision from the RTKLIB `.pos` solution.
- **PPP (base absolute)** — $\Sigma_{\text{PPP}}$, base station precision from the CSRS-PPP `.sum` file.
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

## Key Functions

### Core Geotagging
- **`geotag(flight_folders, base_obs, base_nav, ...)`** - Batch process multiple flight folders (recommended)
- **`load_and_compute_camera_positions(...)`** - Compute geotagged positions for a single batch of images

### Raw Data Processing
- **`raw2rinex(input_path, ...)`** - Convert `.bin`/`.dat` to RINEX observation (.obs) and navigation (.nav) files
- **`process_ppk(base_obs, base_nav, rover_obs, ...)`** - Run precise point positioning with RTKLIB

### Ephemeris & Configuration
- **`download_igs_data(...)`** - Download IGS precise orbits and clocks (SP3/CLK)
- **`pause_for_PPP_sum_file()`** - Interactive prompt for CSRS-PPP `.sum` file
- **`override_rtklib_config(user_conf, ...)`** - Merge and export RTKLIB configuration

### Coordinate Transformation
- **`transform_coordinates(df, target_crs, ...)`** - Convert ECEF → projected CRS (e.g., UTM)
- **`save_csv(df)`** - Export results to timestamped CSV file

## Configuration

### RTKLIB Settings
Default PPK configuration is in `dji_geotagger/config/default_ppk_dict.py`. Override using:

```python
user_config = {
    'pos1-posmode': 'kinematic',
    'pos1-frequency': '1',
    'pos1-soltype': 'forward',
    # ... any other RTKLIB parameters
}

dgt.geotag(flight_folders, base_obs, base_nav, user_conf=user_config)
```

### Base Station Position Options

**Option 1: CSRS-PPP (Recommended)**
```python
geotag_df = dgt.geotag(..., sum_file_path="path/to/base.sum")
```

**Option 2: Manual ECEF Coordinates**
```python
user_config = {
    'ant2-postype': 'xyz',
    'ant2-pos1': X_meters,
    'ant2-pos2': Y_meters,
    'ant2-pos3': Z_meters,
}
geotag_df = dgt.geotag(..., user_conf=user_config)
```

## Troubleshooting

### RTKLIB Not Found
The library will automatically prompt to download RTKLIB binaries on first use. To skip the prompt:
```python
dgt.download_rtklib_bins()
```

### No PPP Sum File Available
If you don't have access to CSRS-PPP:
1. Ensure base station coordinates are accurate (use surveyed position or local PPP)
2. Provide coordinates via `user_conf` dictionary
3. Accept slightly higher positional uncertainty (typically ±0.5–1.0 m without PPP refinement)

### Image-Time Mismatch
Ensure:
- Camera clock is synchronized within ±1 second of GPS
- EXIF/XMP timestamps are in UTC (not local time)
- MRK files cover the same time period as images

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
