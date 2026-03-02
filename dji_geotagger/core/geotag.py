from pathlib import Path
import pandas as pd
from dji_geotagger.ppk.raw_converter import raw2rinex
from dji_geotagger.ppk.ppk_solver import process_ppk
from dji_geotagger.core.mrk_parser import mrk2df
from dji_geotagger.core.xml_parser import parse_img_dir
from dji_geotagger.core.camera_pos_solver import compute_camera_position


def geotag(
    flight_folders: list[str],
    base_obs: str,
    base_nav: str,
    csv_path : str = None,
    sum_file_path: str = None,
    full_output: bool = False
) -> pd.DataFrame:
    """
    High-level API: process a single DJI flight folder end-to-end.

    Pipeline:
        1. raw2rinex        : convert rover GNSS raw data to RINEX
        2. process_ppk      : run PPK solver to get rover trajectory
        3. parse_mrk        : parse DJI MRK timestamp file
        4. parse_img_dir    : extract image XMP metadata
        5. compute_camera_position : interpolate + gimbal correction

    Parameters
    ----------
    flight_dir : str or Path
        Path to DJI flight folder (must contain *_PPKRAW.bin and *.MRK).
    base_obs : str or Path
        Base station RINEX observation file.
    base_nav : str or Path
        Base station RINEX navigation file.
    full_output : bool
        If True, return all intermediate columns. Default False.
    save_pos : bool
        If True, save pos_df to CSV in flight_dir. Default False.

    Returns
    -------
    pd.DataFrame
        Final camera position DataFrame.
    """
    results = []

    for flight_dir in flight_folders:

        flight_dir = Path(flight_dir)

        # Step 1+2: Rover RINEX + PPK
        rover_raw = list(flight_dir.glob("*_PPKRAW.bin"))[0]
        rover_obs, _ = raw2rinex(rover_raw)
        pos_df = process_ppk(
                        base_obs, 
                        base_nav, 
                        rover_obs=rover_obs,
                        sum_file_path=sum_file_path)

        # Step 3: MRK
        mrk = list(flight_dir.glob("*.MRK"))[0]
        mrk_df = mrk2df(mrk)

        # Step 4: Image XML
        img_df = parse_img_dir(flight_dir)

        # Step 5: Camera position
        result = compute_camera_position(
            pos_df=pos_df,
            mrk_df=mrk_df,
            img_df=img_df,
            full_output=full_output,
        )

        # flight name
        result["flight"] = flight_dir.stem
        results.append(result)

    final_df = pd.concat(results, ignore_index=True)
    return final_df