from pathlib import Path
import datetime
from pyproj import CRS
from dji_geotagger import *

# Set input directory (replace with your own)
input_dir = Path("example_data/site1")

# Optional: Clean temp folders
# clean_temp_dirs()

# Convert raw GNSS logs to RINEX
raw_to_rinex_batch(
    keywords=["DRTK", ".dat"],
    input_dir=input_dir,
    type="base"
)

raw_to_rinex_batch(
    keywords=["PPKRAW", ".bin"],
    input_dir=input_dir,
    type="rover"
)

# Process PPK (will auto-download precise ephemeris if missing)
process_ppk(
    base_obs=Path("temp/rinex_base/base.obs"),
    base_nav=Path("temp/rinex_base/base.nav"),
    rover_dir=Path("temp/rinex_rover"),
    conf_override={"pos1-elmask": "10"},
    override_base_from_sum_file=Path("example_data/site1/base_data/base.sum"),
    output_dir=Path("temp/ppk_result")
)

# Interpolate camera positions using MRK and PPK data
final_df = load_and_compute_camera_positions(
    mrk_dir=input_dir,
    img_dir=input_dir,
    pos_dir=Path("temp/ppk_result"),
    base_sum_file=Path("example_data/site1/base_data/base.sum")
)

# Optional: Convert to target CRS (e.g., NAD83 / UTM zone 12N)
target_crs = 26912
crs_tgt = CRS.from_user_input(target_crs)
final_df = transform_coordinates(
    final_df,
    source_crs=get_crs_igb20(),
    target_crs=crs_tgt,
    x_col="x_ecef",
    y_col="y_ecef",
    z_col="z_ecef",
    out_x="E_NAD83",
    out_y="N_NAD83",
    out_z="H_NAD83",
    cov_ecef2enu=True
)

# Export results
output_csv = Path(f"temp/geotag_output/geotagged_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
final_df.to_csv(output_csv, index=False)
print(f"[INFO] Exported geotagged data to: {output_csv}")
