from pathlib import Path
import subprocess
from tqdm import tqdm
import numpy as np
from dji_geotagger.ppk.ephemeris_downloader import try_download_igs_data
from dji_geotagger.core.PPP_sum_parser import sum_file_parser

def update_ant2_position(
    conf_file: Path,
    postype: str = "xyz",  # "llh" or "xyz"
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
):
    """
    Update RTKLIB .conf file for ant2 (rover) position mode and coordinates.

    Parameters:
        conf_path (Path): Path to RTKLIB config (.conf) file.
        postype (str): 'llh' or 'xyz'.
        pos (tuple): Coordinates (ECEF in meters if xyz, or lat/lon/height in degrees/meters if llh).
    """

    # Mapping for ant2-postype: 0=llh, 1=xyz
    type_map = {"llh": "0", "xyz": "1"}
    if postype not in type_map:
        raise ValueError(f"Invalid postype: {postype}, must be 'llh' or 'xyz'")

    lines = conf_file.read_text(encoding="utf-8").splitlines()
    new_lines = []
    for line in lines:
        if line.strip().startswith("ant2-postype"):
            new_lines.append(f"ant2-postype       ={postype}        # (0:llh,1:xyz,...)")
        elif line.strip().startswith("ant2-pos1"):
            new_lines.append(f"ant2-pos1          ={pos[0]:.4f}")
        elif line.strip().startswith("ant2-pos2"):
            new_lines.append(f"ant2-pos2          ={pos[1]:.4f}")
        elif line.strip().startswith("ant2-pos3"):
            new_lines.append(f"ant2-pos3          ={pos[2]:.4f}")
        else:
            new_lines.append(line)

    conf_file.write_text("\n".join(new_lines), encoding="utf-8")
    print(f"[INFO] Updated ant2 position in: {conf_file.name}")


def process_ppk(
    base_obs: Path,
    base_nav: Path,
    rover_dir: Path,
    override_base_from_sum_file: Path = None,
    ephemeris_files: list[Path] = None,
    base_position: tuple[float, float, float] = (0, 0, 0),
    base_position_type: str = "xyz",  # or "llh"
    output_dir: Path = Path(r"temp\ppk_result"),
    conf_file: Path = Path(r"tools\RTKLIB\default_ppk.conf"),
    rnx2rtkp: Path = Path(r"tools\RTKLIB\bin\rnx2rtkp.exe")
) -> Path:
    """
    Batch process RTKLIB PPK solution for a directory of rover OBS files.

    Parameters:
        base_obs (Path): Path to base station .obs file.
        base_nav (Path): Path to base station .nav file.
        rover_dir (Path): Directory containing rover .obs files.
        sp3_files (list[Path]): List of precise ephemeris files (.sp3).
        clk_files (list[Path]): List of precise clock files (.clk).
        base_position (tuple): Base station position in ECEF (m) or LLH (deg/m).
        base_position_mode (str): "xyz" for ECEF, "llh" for geodetic.
        output_dir (Path): Where to store .pos results.
        conf_file (Path): Path to RTKLIB config file (.conf).
        rnx2rtkp (Path): Path to rnx2rtkp executable.

    Return:
        output_dir (Path): Path to where to store .pos results.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Check rnx2rtkp exists
    if rnx2rtkp_not_exists(rnx2rtkp):
        raise FileNotFoundError("rnx2rtkp.exe not found.")

    # If input sum file
    if override_base_from_sum_file:
        X, Y, Z, lat, lon, hgt, cor_sys, cov_ecef = sum_file_parser(override_base_from_sum_file)
        base_position = (X, Y, Z)
        print(f"[INFO] Parsed base position: (ECEF, system: {cor_sys}) from .sum: ({X:.3f}, {Y:.3f}, {Z:.3f})")
        print(f"                             (LLH,  system: {cor_sys}) from .sum: ({np.degrees(lat):.3f}°, {np.degrees(lon):.3f}°, {hgt:.3f} m)")
        print(f"                             Base RMS error (1σ, meters): {np.sqrt(np.diag(cov_ecef))}")

        if cor_sys not in ["IGb20", "IGS20", "ITRF2020", "IGS14", "ITRF2014"]:
            raise ValueError(f"[ERROR] Unexpected base coordinate system '{cor_sys}'. Please convert to IGS-compatible frame (e.g., IGS20) before use.")

    # update conf file
    update_ant2_position(conf_file = conf_file, postype = base_position_type, pos = base_position)

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

def rnx2rtkp_not_exists(rnx2rtkp_path: Path):
    """
    Check if rnx2rtkp.exe exists at the specified path.
    If not found, print an instructional message for the user.
    """
    if not rnx2rtkp_path.exists():
        print("[ERROR] rnx2rtkp.exe not found.")
        print(f"Expected location: {rnx2rtkp_path.resolve()}")
        print("\nTo use this tool, please download RTKLIB from the official site:")
        print("  🔗 https://www.rtklib.com/")
        print("\nAfter downloading, place 'rnx2rtkp.exe' in the following folder:")
        print(f" 📁 {rnx2rtkp_path.parent.resolve()}")
        print("\n⚠️ If your antivirus software blocks the executable,")
        print("please add an exception or trust rule for 'rnx2rtkp.exe' manually.")
        print("RTKLIB is an open-source, well-known GNSS processing toolkit.")
        return True
    return False