import dji_geotagger as dgt

## 1. Convert GNSS data (Base is Global)
base_obs, base_nav = dgt.raw2rinex(r"DRTK3\DRTK3_0038_20250730102537_XXXXX.dat", antenna_height_in_meter=2.0)

## 2. Resolve the base station position
#  Pick ONE of the three sources below. All return the same structure, so the
#  rest of the script is identical whichever you choose.
#
#  Resolving it here, rather than letting geotag() do it, means you can check
#  the coordinates before committing to a run that takes minutes per flight.

# (a) Existing CSRS-PPP .sum file - what you downloaded from the website.
#     Omit sum_file_path to auto-detect a .sum sitting next to your .obs.
base_position = dgt.resolve_base_position(
    mode="sum",
    sum_file_path=r"DRTK3\PPP\DRTK3_0038_20250730102537_XXXXX.sum",
)

# (b) Submit to CSRS-PPP automatically and fetch the .sum back.
#     Needs a free CSRS account; the email is the only credential involved.
#     Use sysref="NAD83" for Canadian deliverables, "ITRF" for the global frame.
# base_position = dgt.resolve_base_position(
#     mode="online",
#     base_obs=base_obs,
#     email="you@example.com",
#     ppp_kwargs={"process_type": "Static", "sysref": "ITRF"},
# )

# (c) Coordinates you already know - a published control point or CORS station.
#     Note: hgt must be ELLIPSOIDAL, not orthometric (CGVD28/CGVD2013), and
#     sigma_ENU is mandatory because it feeds the final uncertainty estimate.
# base_position = dgt.resolve_base_position(
#     mode="manual",
#     manual_kwargs=dict(
#         lat_dd=51.0000000, lon_dd=-114.0000000, hgt=1000.0000,
#         coord_sys="NAD83(CSRS)", epoch="2010.0",
#         sigma_ENU=(0.010, 0.010, 0.020),   # metres, 1-sigma
#     ),
# )

## 3. Define flight folders
flight_folders = [
    r"P1\DJI_202507301227_011_LOCATION-p1v2",
    r"P1\DJI_202507301227_012_LOCATION-p1v2",
    r"P1\DJI_202507301256_013_LOCATION-p1v2"
]

## 4. flight as unit to process
geotag_df = dgt.geotag(flight_folders, base_obs, base_nav, base_position=base_position)
geotag_df.to_csv("LOCATION.csv", index=True)
