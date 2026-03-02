from pathlib import Path
import subprocess
import pandas as pd
from dji_geotagger.tools.install_RTKLIB import get_rtklib_executable
from dji_geotagger.ppk.ephemeris_downloader import download_igs_data
from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser
from dji_geotagger.config.import_config import override_rtklib_config
from dji_geotagger.core.pos_parser import pos2df

# for PPK process
# We need:
# 1) Base: OBS and PPP result position
# 2) Rover: OBS
# 3) Ephemeris: CLK/SP3
# 4) RTKLIB ppk config

def process_ppk(
    base_obs: str,
    base_nav: str,
    rover_obs: str,
    sum_file_path: str = None,
    user_conf: dict = {},
    ephemeris_files: list[str] = None,
    base_error_propogation_on: bool = True,
    output_dir: Path = None,
    RTKLIB: Path = None
    ) -> pd.DataFrame:
    """
    Batch process RTKLIB PPK solution for a directory of rover OBS files.

    Performs post-processed kinematic (PPK) GNSS positioning using RTKLIB's
    rnx2rtkp. Resolves base station coordinates from a PPP .sum file or
    user-supplied config, builds a temporary RTKLIB .conf, and processes
    each rover .obs file to produce a .pos output.

    Base coordinate priority:
        1. sum_file_path  : explicit path to CSRS-PPP .sum file
        2. base_obs stem  : auto-locate .sum under DGT_output/RINEX/base/PPP/
        3. user_conf      : manual ant2-pos1/2/3 entries

    Ephemeris priority:
        1. ephemeris_files : user-supplied .sp3/.clk files
        2. auto-download   : IGS FINAL products via download_igs_data()

    Already-existing .pos files are skipped but still included in the
    returned list, so downstream pos_df_merger() receives all results.

    Parameters:
    base_obs : str or Path
        Path to base station RINEX observation file.
    base_nav : str or Path
        Path to base station RINEX navigation file.
    rover_dir : str or Path
        Directory containing rover RINEX observation files (*.obs).
    sum_file_path : str or Path, optional
        Path to CSRS-PPP .sum file for base station ECEF coordinates.
        If None, attempts to auto-locate from base_obs stem.
    user_conf : dict, optional
        RTKLIB config key-value overrides (e.g. {"pos1-posmode": "kinematic"}).
        Base coordinates from .sum file always take priority over
        ant2-pos1/2/3 entries here.
    ephemeris_files : list of str or Path, optional
        Precise ephemeris (.sp3) and clock (.clk) files.
        If None, IGS FINAL products are downloaded automatically.
    output_dir : Path, optional
        Directory for .pos output files.
        Defaults to <cwd>/DGT_output/PPK_result.
    RTKLIB : Path, optional
        Path to RTKLIB installation directory containing rnx2rtkp.
        If None, searches system PATH.

    Returns:
    list of Path
        Paths to all .pos output files (both newly solved and pre-existing).
        Failed epochs are excluded.
    """
    

    # Check input
    base_obs = Path(base_obs)
    base_nav = Path(base_nav)
    rover_obs = Path(rover_obs)
    ephemeris_files = [Path(p) for p in ephemeris_files] if ephemeris_files else None
    base_error_propogation_on = base_error_propogation_on
    output_dir = Path(output_dir) if output_dir else Path.cwd() / "DGT_output" / "PPK_result"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check files exist
    for file in [base_obs, base_nav, rover_obs]:
        if file and not file.exists():
            raise FileNotFoundError(f"[ERROR] File not found: {file}")

    if ephemeris_files:
        for file in ephemeris_files:
            if not file.exists():
                raise FileNotFoundError(f"[ERROR] Ephemeris file not found: {file}")
    

    # Check rnx2rtkp
    rnx2rtkp = get_rtklib_executable("rnx2rtkp", RTKLIB)

    # Handle base station configuration
    base_conf = resolve_base_position(base_obs, sum_file_path, user_conf)
    user_conf.update(base_conf)
    conf_file = override_rtklib_config(user_conf)

    # Download ephemeris data (.clk and .sp3)
    if not ephemeris_files:
        ephemeris_files = download_igs_data(base_obs_path=base_obs)   
    
    # start ppk
    output_pos = output_dir / f"{rover_obs.stem}.pos"
    if output_pos.exists():
        answer = input(f"[WARNING] {output_pos.name} already exists. Overwrite? (y/n): ").strip().lower()
        if answer != "y":
            print(f"[INFO] Skipping: {output_pos.name}")
            return
        
    cmd = [
        str(rnx2rtkp),
        "-k", str(conf_file),
        "-o", str(output_pos),
        str(rover_obs),
        str(base_obs),
        str(base_nav),
        *[str(f) for f in ephemeris_files],
    ]

    try:
        print(f"[INFO] Solving: {rover_obs.name} ...")
        subprocess.run(cmd, check=True)
        print(f"[INFO] Finished: {output_pos.name}")
    
    except subprocess.CalledProcessError:
        print(f"[ERROR] Failed to process: {rover_obs.name}")

    # .pos -> df
    df = pos2df(pos_file=output_pos, base_error_propogation_on=base_error_propogation_on)

    return df


def resolve_base_position(
    base_obs: Path = None,
    sum_file_path: str = None,
    user_conf: dict = {}
    ) -> dict:
    """
    Resolve base station ECEF coordinates from one of three sources:
    1. User-specified .sum file path
    2. Auto-detected .sum file from base_obs filename
    3. Manual coordinates in user_conf (ant2-pos1/2/3)

    Returns:
    base_conf : dict
        RTKLIB config entries for base position
    """

    required = {"ant2-pos1", "ant2-pos2", "ant2-pos3"} # for Priority 3
    # Priority 1 & 2: .sum file
    if sum_file_path or base_obs:
        PPP_result = sum_file_parser(base_obs=base_obs, sum_file_path=sum_file_path)
        X, Y, Z = PPP_result["X"], PPP_result["Y"], PPP_result["Z"]
    # Priority 3: manual user_conf
    elif required.issubset(user_conf):
        print("[INFO] Using manual base coordinates from user_conf.")
        X, Y, Z = user_conf["ant2-pos1"], user_conf["ant2-pos2"], user_conf["ant2-pos3"]    
    # No input
    else:
        raise ValueError("[ERROR] Base position not provided. Supply sum_file_path, base_obs, or ant2-pos1/2/3 in user_conf.")
    
    return  {
                "ant2-postype": "xyz",
                "ant2-pos1": X,
                "ant2-pos2": Y,
                "ant2-pos3": Z,
            }



def no_base_pos_warn():
    return("""
[ERROR] No base position provided.
        Please provide base coordinates via one of the following methods:
        
          Option 1 - Provide a CSRS-PPP .sum file (recommended):
              process_ppk(..., base_PPP_sum='path/to/result.sum')
        
          Option 2 - Manually specify ECEF coordinates via user_conf:
              user_conf = {
                  'ant2-postype': 'xyz',
                  'ant2-pos1': -2418456.789,  # X (metres)
                  'ant2-pos2':  5385936.123,  # Y (metres)
                  'ant2-pos3':  2405716.456,  # Z (metres)
              }
              process_ppk(..., user_conf=user_conf)
    """)


















#############################
"""
batch process_ppk


    # start ppk
    print(f"\n======= {len(rover_obs_files)} .obs files were found. Start PPK calculation now... =======")
    ppk_results = []
    for rover_obs in rover_obs_files:
        output_pos = output_dir / f"{rover_obs.stem}.pos"
        if output_pos.exists():
            print(f"[WARNING] Output exists, skipping: {output_pos.name}")
            ppk_results.append(output_pos) # skip and add result to list
            continue
        cmd = [
            str(rnx2rtkp),
            "-k", str(conf_file),
            "-o", str(output_pos),
            str(rover_obs),
            str(base_obs),
            str(base_nav),
            *[str(f) for f in ephemeris_files],
        ]

        try:
            print(f"[INFO] Solving: {rover_obs.name} ...")
            subprocess.run(cmd, check=True)
            ppk_results.append(output_pos)
            print(f"[INFO] Finished: {output_pos.name}")
        
        except subprocess.CalledProcessError:
            print(f"[ERROR] Failed to process: {rover_obs.name}")

    # Note for PPK
    print("[NOTE] Although RTKLIB labels output coordinates as 'WGS84', the actual reference frame "
        "is determined by the SP3/CLK products used. "
        "(e.g., IGS FINAL products are referenced to IGS20, "
        "which is consistent with ITRF2020 at the mm level.)")

        """