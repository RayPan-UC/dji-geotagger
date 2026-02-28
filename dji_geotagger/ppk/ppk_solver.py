from pathlib import Path
import subprocess
from dji_geotagger.tools.install_RTKLIB import get_rtklib_executable
from dji_geotagger.ppk.ephemeris_downloader import download_igs_data
from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser
from dji_geotagger.config.import_config import override_rtklib_config

# for PPK process
# We need:
# 1) Base: OBS and PPP result position
# 2) Rover: OBS
# 3) Ephemeris: CLK/SP3
# 4) RTKLIB ppk config

def process_ppk(
    base_obs: str,
    base_nav: str,
    rover_dir: str,
    base_PPP_sum: str = None,
    user_conf: dict = {},
    ephemeris_files: list[str] = None,

    output_dir: Path = None,


    
    RTKLIB: Path = None
) -> Path:
    """
    Batch process RTKLIB PPK solution for a directory of rover OBS files.

    This function performs post-processed kinematic (PPK) GNSS positioning using RTKLIB's `rnx2rtkp.exe`.
    It automatically applies base station coordinates from a `.sum` file (if provided), generates a
    temporary RTKLIB `.conf` file with optional user overrides, and processes each rover `.obs` file
    to produce `.pos` outputs.

    Parameters:
        base_obs (Path): Path to base station .obs file.
        base_nav (Path): Path to base station .nav file.
        rover_dir (Path): Directory containing rover .obs files.
        override_base_from_sum_file (Path, optional): Path to `.sum` file from CSRS-PPP or equivalent,
            used to extract base station ECEF coordinates (X, Y, Z).
        ephemeris_files (list[Path], optional): List of precise ephemeris and clock files (.sp3/.clk).
            If None, the function will attempt to download FINAL IGS products automatically.
        output_dir (Path, optional): Directory to store output .pos files. Defaults to 'temp/ppk_result'.
        conf_override (dict, optional): Dictionary of RTKLIB configuration options to override the default.
            Common keys include "pos1-posmode", "ant2-pos1", etc.
        rnx2rtkp (Path, optional): Path to RTKLIB rnx2rtkp executable. Default assumes standard install.

    Returns:
        Path: Path to the output directory containing all generated .pos files.
    """
    

    # Check input
    base_obs = Path(base_obs)
    base_nav = Path(base_nav)
    rover_dir = Path(rover_dir)
    base_PPP_sum = Path(base_PPP_sum) if base_PPP_sum else Path.cwd() / "DGT_output" / "RINEX" / "base" / "PPP" / f"{base_obs.stem}.sum"
    ephemeris_files = [Path(p) for p in ephemeris_files] if ephemeris_files else None
    output_dir = Path(output_dir) if output_dir else Path.cwd() / "DGT_output" / "PPK_result"
    output_dir.mkdir(parents=True, exist_ok=True)


    # Check files exist
    for file in [base_obs, base_nav, base_PPP_sum]:
        if file and not file.exists():
            raise FileNotFoundError(f"[ERROR] File not found: {file}")

    if ephemeris_files:
        for file in ephemeris_files:
            if not file.exists():
                raise FileNotFoundError(f"[ERROR] Ephemeris file not found: {file}")

    # Check rnx2rtkp
    rnx2rtkp = get_rtklib_executable("rnx2rtkp", RTKLIB)
            

    # Handle base station configuration
    # 1. If PPP .sum file provided
    if base_PPP_sum:
        # Priority: Use PPP .sum file for base coordinates
        PPP_result = sum_file_parser(base_PPP_sum)
        base_conf = {
            "ant2-postype": "xyz",
            "ant2-pos1": PPP_result.get("X"),
            "ant2-pos2": PPP_result.get("Y"),
            "ant2-pos3": PPP_result.get("Z")
        }
    
        # If user config provided
        user_conf.update(base_conf)
        conf_file = override_rtklib_config(user_conf)

    

    else:
        if not user_conf:
            raise ValueError(no_base_pos_warn())
        print("[INFO] Using manual base coordinates from conf_override.")
        conf_file = override_rtklib_config(user_conf)


    

    # Download ephemeris data (.clk and .sp3)
    if not ephemeris_files:
        ephemeris_files = download_igs_data(base_obs_path=base_obs)
    
    # Print rover file count
    obs_files = sorted(rover_dir.glob("*.obs"))
    print(f"\n======= {len(obs_files)} .obs files were found. Start PPK calculation now... =======")


    # start ppk
    for rover_obs in obs_files:
        output_pos = output_dir / f"{rover_obs.stem}.pos"

        if output_pos.exists():
            print(f"[WARNING] Output exists, skipping: {output_pos.name}")
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

        print(f"[INFO] Solving: {rover_obs.name} ...")
        
        
        try:
            subprocess.run(cmd, check=True)
            print(f"[INFO] Finished: {output_pos.name}")
        
        except subprocess.CalledProcessError:
            print(f"[ERROR] Failed to process: {rover_obs.name}")

    # Note for PPK
    print("[NOTE] Although RTKLIB labels output coordinates as 'WGS84', the actual reference frame "
        "is determined by the SP3/CLK products used. "
        "(e.g., IGS FINAL products are referenced to IGS20, "
        "which is consistent with ITRF2020 at the mm level.)")

    return output_dir

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