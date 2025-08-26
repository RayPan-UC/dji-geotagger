from pathlib import Path
from pyproj import CRS
from dji_geotagger import *

# === User-defined project path ===
project_root = Path(r"/path/to/your/project/SynopticSite1")

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

# === Pause here to process base .sum file if available ===

ppp_sum_file = pause_for_PPP_sum_file()


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

# === Transform to target coordinate system (e.g., WGS84/UTM) ===
target_crs = 32612
final_df = transform_coordinates(
    final_df,
    target_crs=CRS.from_user_input(target_crs),
    out_x="Easting",
    out_y="Northing",
    out_z="Height_Ellp",
    cov_ecef2enu=True,
    drop_original=True
)

# === Save result as CSV ===
save_csv(final_df)