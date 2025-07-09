# DJI Geotagger

**A precise PPK + MRK-based geotagging tool for DJI RTK drones**

This Python library enables centimeter-level camera geotagging by combining PPK `.pos` solutions, DJI `.MRK` gimbal offset corrections, and EXIF/XMP metadata from DJI RTK drone images. It is designed for photogrammetry and remote sensing workflows that require accurate EOPs without using ground control points (GCPs).

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
pip install -r requirements.txt
pip install dji-geotagger
```

## Dependencies

- Python ≥ 3.9
- `pandas`, `numpy`, `pyproj`, `tqdm`, `exifread`
- RTKLIB (`convbin.exe`, `rnx2rtkp.exe`)

## Workflow Overview

1. **Convert raw GNSS to RINEX**  
   Uses RTKLIB `convbin` for both base and rover logs.

2. **Download precise IGS ephemeris**  
   Automatically fetch `.sp3` and `.clk` based on RINEX timestamps.

3. **Run PPK**  
   Batch PPK processing using `rnx2rtkp` with optional override base coordinates from PPP `.sum` file.

4. **Parse image EXIF/XMP metadata**  
   Extracts capture time, attitude, and gimbal orientation.

5. **Parse MRK files**  
   Converts NED to ENU, then ENU → ECEF correction vectors.

6. **Interpolate camera center**  
   Matches MRK by time, interpolates PPK positions, applies gimbal offset.

7. **Export results**  
   Generates a DataFrame (or CSV) of corrected positions and attitude per image.

## Example Usage

```python
from pathlib import Path
from pyproj import CRS
from dji_geotagger import (
    raw_to_rinex_batch, try_download_igs_data, process_ppk,
    combine_all_img_info, combine_all_mrk, combine_all_pos,
    compute_camera_positions, transform_coordinates, get_crs_igb20
)

# === Step 1: Convert raw GNSS logs to RINEX ===
raw_to_rinex_batch(
    input_dir=Path("project/data"),
    keywords=[".dat"],  # for base
    type="base",
    antenna_height_in_meter=0.0
)

raw_to_rinex_batch(
    input_dir=Path("project/data"),
    keywords=[".bin"],  # for rover
    type="rover",
    antenna_height_in_meter=0.0
)

# === Step 2: Download precise ephemeris data (SP3/CLK) ===
ephemeris_files = try_download_igs_data(base_obs_path=Path("temp/rinex_base/base.obs"))

# === Step 3: Run PPK positioning ===
process_ppk(
    base_obs=Path("temp/rinex_base/base.obs"),
    base_nav=Path("temp/rinex_base/base.nav"),
    rover_dir=Path("temp/rinex_rover"),
    override_base_from_sum_file=Path("project/base/base.sum"),
    ephemeris_files=ephemeris_files,
    output_dir=Path("temp/ppk_result")
)

# === Step 4: Load and merge data ===
df_img = combine_all_img_info(photo_folder=Path("project/images"))
df_mrk = combine_all_mrk(mrk_folder=Path("project/mrk"))
df_pos = combine_all_pos(
    base_sum_file=Path("project/base/base.sum"),
    pos_folder=Path("temp/ppk_result")
)

# === Step 5: Compute geotagged camera positions ===
df_output = compute_camera_positions(df_img, df_mrk, df_pos)

# === Step 6: Transform coordinates (e.g., to UTM NAD83 / Zone 12N) ===
df_output = transform_coordinates(
    df_output,
    source_crs=get_crs_igb20(),
    target_crs=CRS.from_epsg(26912),
    x_col="x_ecef",
    y_col="y_ecef",
    z_col="z_ecef",
    out_x="E_NAD83",
    out_y="N_NAD83",
    out_z="H_NAD83",
    cov_ecef2enu=True
)

# === Step 7: Export to CSV ===
df_output.to_csv("geotag_output/geotagged_results.csv", index=False)
```

## Output Format

| file_name | gps_week | gps_time | x_ecef | y_ecef | z_ecef | sd_x_ecef | sd_y_ecef | sd_z_ecef | yaw | pitch | roll |
|-----------|----------|----------|--------|--------|--------|-----------|-----------|-----------|------|--------|------|

## License

This project is licensed under the BSD 2-Clause (see LICENSE for details).

## Acknowledgments

- Developed at the University of Calgary, ASPEN Lab
- Inspired by real-world field workflows using DJI P1 + Hemisphere + CSRS PPP