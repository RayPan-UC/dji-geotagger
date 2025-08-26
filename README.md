# DJI Geotagger

**A precise PPK + MRK-based geotagging tool for DJI RTK drones**

This Python library enables centimetre-level camera geotagging by combining PPK `.pos` solutions, DJI `.MRK` gimbal offset corrections, and EXIF/XMP metadata from DJI RTK drone images. It is designed for photogrammetry and remote sensing workflows that require accurate EOPs.

## Features

- Batch process `.obs` raw GNSS logs into RINEX and perform PPK with RTKLIB
- Download precise ephemeris data (SP3/CLK) automatically
- Parse DJI `.MRK` and interpolate correction vectors to camera center (ECEF)
- Match images by GPS time, apply PPK + MRK correction with covariance propagation
- Export geotagged results in ECEF/ENU/UTM with estimated 3D precision
- Support for DJI P1, M300, and other RTK-enabled drones

## Installation

```bash
git clone https://github.com/RayPan-UC/dji-geotagger.git
cd dji-geotagger
pip install dji-geotagger
```

## Dependencies

- Python ≥ 3.9
- `pillow`, `defusedxml`, `pandas`, `numpy`, `pyproj`, `tqdm`
- RTKLIB (`convbin.exe`, `rnx2rtkp.exe`)

## Workflow Overview

1. **Convert raw GNSS to RINEX**Uses RTKLIB `convbin` for both base and rover logs.
2. **Download precise IGS ephemeris**Automatically fetch `.sp3` and `.clk` based on RINEX timestamps.
3. **Run PPK**Batch PPK processing using `rnx2rtkp` with optional override base coordinates from PPP `.sum` file.
4. **Parse image EXIF/XMP metadata**Extracts capture time, attitude, and gimbal orientation.
5. **Parse MRK files**Converts NED to ENU, then ENU → ECEF correction vectors.
6. **Interpolate camera center**Matches MRK by time, interpolates PPK positions, applies gimbal offset.
7. **Export results**
   Generates a DataFrame (or CSV) of corrected positions and attitude per image.

## Example Usage

```python
from pathlib import Path
import datetime
from pyproj import CRS
from dji_geotagger import *

# === User-defined project path ===
project_root = Path(r"/path/to/your/project/SynopticSite1")
ppp_sum_file = project_root / "base_data" / "DRTK" / "PPP_result" / "DRTK3_0006_20250513073737_8PHDMCM00A1369.sum"

# === Clean temporary directories ===
clean_temp_dirs()

# === Convert base and rover raw logs to RINEX ===
base_obs, base_nav = raw_to_rinex_batch(
    keywords=['20250513', '0006', 'DRTK', '.dat'],
    input_dir=project_root,
    type="base",
)

rover_dir = raw_to_rinex_batch(
    keywords=['20250513', 'PPKRAW', '.bin'],
    input_dir=project_root,
    type="rover"
)

# === Post-process PPK with base .sum file ===
process_ppk(
    base_obs=base_obs,
    base_nav=base_nav,
    rover_dir=rover_dir,
    override_base_from_sum_file=ppp_sum_file,
    output_dir=Path("temp/ppk_result"),
)

# === Compute corrected camera positions ===
final_df = load_and_compute_camera_positions(
    mrk_dir=project_root,
    img_dir=project_root,
    pos_dir=Path("temp/ppk_result"),
    base_sum_file=ppp_sum_file
)

# === Transform to target coordinate system (e.g., NAD83 / UTM zone 12N) ===
target_crs = 26912
final_df = transform_coordinates(
    final_df,
    target_crs=CRS.from_user_input(target_crs),
    out_x="E_NAD83",
    out_y="N_NAD83",
    out_z="H_NAD83",
    cov_ecef2enu=True,
    drop_original=True
)

# === Save result as CSV ===
output_csv = Path(f"geotag_output/geotagged_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
output_csv.parent.mkdir(parents=True, exist_ok=True)
final_df.to_csv(output_csv, index=False)
print(f"[INFO] Exported geotagged data to: {output_csv}")
```

## Output Format

The output CSV contains the following columns:

| Column Name             | Description                                                                            |
| ----------------------- | -------------------------------------------------------------------------------------- |
| `file_name`           | Image file name                                                                        |
| `gps_week`            | GPS week number                                                                        |
| `gps_time`            | Seconds into the GPS week                                                              |
| `sd_x_ecef`           | Standard deviation in ECEF X (metres)                                                  |
| `sd_y_ecef`           | Standard deviation in ECEF Y (metres)                                                  |
| `sd_z_ecef`           | Standard deviation in ECEF Z (metres)                                                  |
| `cov_ecef_flat`       | Flattened 3×3 ECEF covariance matrix (row-major, space-separated)                     |
| `flight_roll`         | Aircraft body roll (degrees)                                                           |
| `flight_pitch`        | Aircraft body pitch (degrees)                                                          |
| `flight_yaw`          | Aircraft body yaw (degrees)                                                            |
| `gimbal_roll`         | Gimbal-reported roll (degrees)                                                         |
| `gimbal_pitch`        | Gimbal-reported pitch (degrees)                                                        |
| `gimbal_yaw`          | Gimbal-reported yaw (degrees)                                                          |
| `dji_geotagger_roll`  | Corrected camera roll for photogrammetry (degrees), with gimbal lock handling          |
| `dji_geotagger_pitch` | Corrected camera pitch for photogrammetry (degrees), computed as `gimbal_pitch + 90` |
| `dji_geotagger_yaw`   | Camera yaw for photogrammetry (degrees), taken directly from `flight_yaw`            |
| `Easting`             | Easting in WGS84 / UTM Zone 12N (metres)                                               |
| `Northing`            | Northing in WGS84 / UTM Zone 12N (metres)                                              |
| `Height_Ellp`         | Height in WGS84 ellipsoidal coordinates (metres)                                       |
| `sd_E`                | Standard deviation in Easting (ENU, metres)                                            |
| `sd_N`                | Standard deviation in Northing (ENU, metres)                                           |
| `sd_U`                | Standard deviation in Up (ENU, metres)                                                 |
| `cov_enu_flat`        | Flattened 3×3 ENU covariance matrix (row-major, space-separated)                      |

Note: The projected coordinates (`Easting`, `Northing`, `Height`) are output in WGS84 / UTM Zone 12N by default. Users can customize the coordinate reference system (CRS) and output column formats according to their project requirements.

## License

This project is licensed under the BSD 2-Clause (see LICENSE for details).

## Acknowledgments

- Developed at the University of Calgary, Applied Geospatial Research Group ([appliedgrg.ca](https://www.appliedgrg.ca))
- Inspired by real-world field workflows involving DJI Matrice 350 RTK + Zenmuse P1, Hemisphere base stations, and CSRS-PPP post-processing
