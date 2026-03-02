import dji_geotagger as dgt

## 1. Convert GNSS data and Process PPP (Base is Global)
base_obs, base_nav = dgt.raw2rinex(r"DRTK3\DRTK3_0038_20250730102537_XXXXX.dat", antenna_height_in_meter=2.0)
ppp_sum_file = r"DRTK3\PPP\DRTK3_0038_20250730102537_XXXXX.sum" # or put .sum file with your .obs file, auto get

## 2. Define flight folders
flight_folders = [
    r"P1\DJI_202507301227_011_LOCATION-p1v2",
    r"P1\DJI_202507301227_012_LOCATION-p1v2",
    r"P1\DJI_202507301256_013_LOCATION-p1v2"
]


## 3. flight as unit to process
geotag_df = dgt.geotag(flight_folders, base_obs, base_nav, sum_file_path=ppp_sum_file)
geotag_df.to_csv("LOCATION.csv", index=True)

