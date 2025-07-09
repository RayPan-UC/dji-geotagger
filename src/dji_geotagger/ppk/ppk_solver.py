from pathlib import Path
import subprocess
from tqdm import tqdm
import numpy as np
from dji_geotagger.ppk.ephemeris_downloader import try_download_igs_data
from dji_geotagger.core.PPP_sum_parser import sum_file_parser
from dji_geotagger.tools.install_utils import download_RTKLIB_instruction
from dji_geotagger.config.import_config import import_rtklib_config


def process_ppk(
    base_obs: Path,
    base_nav: Path,
    rover_dir: Path,
    ephemeris_files: list[Path] = None,
    output_dir: Path = Path(r"temp\ppk_result"),
    override_base_from_sum_file: Path = None,
    conf_override: dict = None,
    rnx2rtkp: Path = Path(r"tools\RTKLIB\bin\rnx2rtkp.exe")
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
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check rnx2rtkp exists
    if not rnx2rtkp.exists():
        download_RTKLIB_instruction()

    # Parse .sum if provided
    if override_base_from_sum_file:
        X, Y, Z, lat, lon, hgt, cor_sys, cov_ecef = sum_file_parser(override_base_from_sum_file)

        if cor_sys not in ["IGb20", "IGS20", "ITRF2020", "IGS14", "ITRF2014"]:
            raise ValueError(f"[ERROR] Unexpected base coordinate system '{cor_sys}'")

        print(f"[INFO] Parsed base ECEF: ({X:.3f}, {Y:.3f}, {Z:.3f}) | LLH: ({np.degrees(lat):.3f}°, {np.degrees(lon):.3f}°, {hgt:.3f} m)")
        print(f"[INFO] Base RMS error (1σ): {np.sqrt(np.diag(cov_ecef))}")

        base_conf = {
            "ant2-postype": "xyz",
            "ant2-pos1": f"{X:.4f}",
            "ant2-pos2": f"{Y:.4f}",
            "ant2-pos3": f"{Z:.4f}"
        }

        if conf_override:
            base_conf.update(conf_override)
        conf_file = import_rtklib_config(base_conf)
    else:
        # without sum file -> default + override
        conf_file = import_rtklib_config(conf_override)
    # update conf file
    print(f"[INFO] Updated ant2 position in: {conf_file.name}")

    # Download ephemeris data (.clk and .sp3)
    if not ephemeris_files:
        ephemeris_files = try_download_igs_data(base_obs_path=base_obs)
        if not ephemeris_files:
            print("[WARNING] Failed to download ephemeris data (.sp3 / .clk).")
            print("[INFO] You can manually download them from:")
            print("        https://igs.org/products/#orbits_clocks")
            print("        (Look under FINAL or RAPID products for your observation date)")
            return
    
    # Print rover file count
    obs_files = list(rover_dir.glob("*.obs"))
    print(f"\n======= {len(obs_files)} .obs files were found. Start PPK calculation now... =======")


    # start ppk
    for rover_obs in sorted(rover_dir.glob("*.obs")):
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
    print("[NOTE] Although RTKLIB .pos file output labels coordinates as 'WGS84', the actual reference frame corresponds to the IGS20 realization (i.e., aligned with ITRF), as determined by the SP3/CLK products used.")

    return output_dir
