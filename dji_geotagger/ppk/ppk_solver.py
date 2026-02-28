from pathlib import Path
import subprocess
from tqdm import tqdm
import numpy as np
from dji_geotagger.ppk.ephemeris_downloader import try_download_igs_data
from dji_geotagger.core.PPP_sum_parser import sum_file_parser
from dji_geotagger.tools.install_RTKLIB import get_rtklib_executable
from dji_geotagger.config.import_config import import_rtklib_config


def process_ppk(
    base_obs: Path,
    base_nav: Path,
    rover_dir: Path,
    ephemeris_files: list[Path] = None,
    output_dir: Path = Path(r"temp\ppk_result"),
    override_base_from_sum_file: Path = None,
    conf_override: dict = None,
    rnx2rtkp: Path = None
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

    # Check rnx2rtkp
    if rnx2rtkp is None:
        rnx2rtkp = get_rtklib_executable("rnx2rtkp")

    if not rnx2rtkp.exists():
        success = download_RTKLIB_instruction(rnx2rtkp)
        if success:
            # After download, recheck
            rnx2rtkp = get_rtklib_executable("rnx2rtkp")
            if not rnx2rtkp.exists():
                raise FileNotFoundError(f"[FATAL] RTKLIB tool still not found: {rnx2rtkp}")
            
    # Handle base station configuration
    if override_base_from_sum_file:
        # Priority: Use PPP .sum file for base coordinates
        X, Y, Z, lat, lon, hgt, cor_sys, cov_ecef = sum_file_parser(override_base_from_sum_file)

        if cor_sys not in ["IGb20", "IGS20", "ITRF2020", "IGS14", "ITRF2014"]:
            raise ValueError(f"[ERROR] Unexpected coordinate system '{cor_sys}' in sum file.")

        print(f"[INFO] Base ECEF (from .sum): ({X:.3f}, {Y:.3f}, {Z:.3f})")
        print(f"[INFO] Base LLH: ({np.degrees(lat):.5f}°, {np.degrees(lon):.5f}°, {hgt:.3f} m)")
        print(f"[INFO] Base RMS error (1σ): {np.sqrt(np.diag(cov_ecef))}")

        # Use coordinates from sum file unless overridden
        base_conf = {
            "ant2-postype": "xyz",
            "ant2-pos1": f"{X:.4f}",
            "ant2-pos2": f"{Y:.4f}",
            "ant2-pos3": f"{Z:.4f}"
        }

        # Merge additional user config (ignore ant2-posX overrides)
        if conf_override:
            for key, val in conf_override.items():
                if not key.startswith("ant2-pos"):
                    base_conf[key] = val

        conf_file = import_rtklib_config(base_conf)

    else:
        # No sum file: use provided overrides only
        if conf_override is None:
            raise ValueError("[ERROR] No base position provided. Either set `override_base_from_sum_file` or `conf_override` with ant2-pos1/2/3.")

        print("[INFO] Using manual base coordinates from conf_override.")
        conf_file = import_rtklib_config(conf_override)
    

    # Download ephemeris data (.clk and .sp3)
    if not ephemeris_files:
        ephemeris_files = try_download_igs_data(base_obs_path=base_obs)
        if not ephemeris_files:
            print("[WARNING] Failed to download ephemeris data (.sp3 / .clk).")
            print("[INFO] You can manually download them from:")
            print("        https://igs.org/products/#orbits_clocks")
            print("        (Look under FINAL or RAPID products for your observation date)")
            print("        (For GPS week and date, you can check: https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/calendr.php)")
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
