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
    source_crs=get_crs_igb20(),
    target_crs=CRS.from_user_input(target_crs),
    x_col="x_ecef",
    y_col="y_ecef",
    z_col="z_ecef",
    out_x="E_NAD83",
    out_y="N_NAD83",
    out_z="H_NAD83",
    cov_ecef2enu=True
)

# === Save result as CSV ===
output_csv = Path(f"geotag_output/geotagged_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
output_csv.parent.mkdir(parents=True, exist_ok=True)
final_df.to_csv(output_csv, index=False)
print(f"[INFO] Exported geotagged data to: {output_csv}")
