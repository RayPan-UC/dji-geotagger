from pathlib import Path
import pandas as pd
from dji_geotagger.ppk.raw_converter import raw2rinex
from dji_geotagger.ppk.ppk_solver import process_ppk
from dji_geotagger.core.mrk_parser import mrk2df
from dji_geotagger.core.xml_parser import parse_img_dir
from dji_geotagger.core.camera_pos_solver import compute_camera_position
from dji_geotagger.ppk.base_position import resolve_base_position


def geotag(
    flight_folders: list[str],
    base_obs: str,
    base_nav: str,
    sum_file_path: str = None,
    full_output: bool = False,
    base_position: dict = None
) -> pd.DataFrame:
    """
    High-level API: run an end-to-end DJI geotagging pipeline for one or more flight folders.

    For each flight folder, this function:
      1) Detects the rover raw GNSS log (*_PPKRAW.bin) and converts it to RINEX via `raw2rinex`.
      2) Runs RTKLIB PPK (`process_ppk`) to generate a rover trajectory in ECEF (and covariance).
      3) Parses the DJI MRK file (*.MRK) via `mrk2df` to obtain exposure epochs and lever-arm offsets.
      4) Parses image XMP metadata from the flight folder via `parse_img_dir`.
      5) Computes camera center positions by interpolating the PPK trajectory to exposure times and
         applying lever-arm correction via `compute_camera_position`.

    Parameters
    ----------
    flight_folders : list[str] | list[Path]
        A list of DJI flight directories. Each folder must contain:
        - one rover GNSS raw file matching "*_PPKRAW.bin"
        - one MRK file matching "*.MRK"
        - image files readable by `parse_img_dir` (e.g., *.jpg / *.tif with DJI XMP)
    base_obs : str | Path
        Base station RINEX observation file (.obs).
    base_nav : str | Path
        Base station RINEX navigation file (.nav / .rnx / .*n).
    sum_file_path : str | Path, optional
        Path to CSRS-PPP summary file (.sum) for base station coordinates/covariance.
        If provided, PPK covariance can include base uncertainty propagation (depends on `process_ppk`).
        Ignored when `base_position` is given.
    full_output : bool, default False
        Passed to `compute_camera_position`.
        If True, return all intermediate columns; if False, return a compact subset.
    base_position : dict, optional
        Base station position from
        :func:`~dji_geotagger.ppk.base_position.resolve_base_position`. This is
        how the non-.sum sources are reached: automated CSRS-PPP submission, or
        directly entered coordinates. When omitted, the base position is
        resolved from `sum_file_path` / `base_obs` as before.

        Resolving it yourself first is recommended for anything long-running,
        because it lets you inspect the base coordinates before committing to
        the full pipeline::

            bp = dgt.resolve_base_position(
                mode="online", base_obs=base_obs, email="you@example.com")
            # ... check the printed position ...
            df = dgt.geotag(flights, base_obs, base_nav, base_position=bp)

    Returns
    -------
    pd.DataFrame
        Concatenated camera center table for all flights, with an extra column:
        - flight : the folder name (Path.stem) for grouping / filtering

    Raises
    ------
    IndexError
        If a flight folder does not contain required "*_PPKRAW.bin" or "*.MRK" files
        (due to direct list indexing in the current implementation).
    FileNotFoundError / RuntimeError
        Propagated from lower-level functions (e.g., RTKLIB execution, missing base files, parsing errors).
    """
    # Resolve the base position once, before any flight is processed. Two
    # reasons: the .sum was previously re-parsed for every flight, and a bad
    # base position should surface before RTKLIB spends minutes on the first
    # flight rather than after.
    if base_position is None:
        base_position = resolve_base_position(
            mode="sum",
            base_obs=base_obs,
            sum_file_path=sum_file_path,
            print_report=True,
        )

    results = []

    for flight_dir in flight_folders:

        flight_dir = Path(flight_dir)

        # Step 1+2: Rover RINEX + PPK
        rover_raws = list(flight_dir.glob("*_PPKRAW.bin"))
        if not rover_raws:
            raise FileNotFoundError(f"No *_PPKRAW.bin found in {flight_dir}")
        rover_raw = rover_raws[0]
        rover_obs, _ = raw2rinex(rover_raw)
        pos_df = process_ppk(
                        base_obs,
                        base_nav,
                        rover_obs=rover_obs,
                        sum_file_path=sum_file_path,
                        base_position=base_position)

        # Step 3: MRK
        mrks = list(flight_dir.glob("*.MRK"))
        if not mrks:
            raise FileNotFoundError(f"No *.MRK found in {flight_dir}")
        mrk = mrks[0]
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

    if not results:
        raise RuntimeError("No flights were successfully processed.")

    final_df = pd.concat(results, ignore_index=True)
    return final_df