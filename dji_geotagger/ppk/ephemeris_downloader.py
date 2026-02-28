from pathlib import Path
from datetime import datetime
import datetime as dt
import requests
import gzip
import shutil
import time
import georinex as gr
from dji_geotagger.tools.tools import utc_to_gps


##########################################################################################
def parse_obs_time_range(obs_file: Path) -> tuple[datetime, datetime]:
    """
    Parse RINEX observation file to extract TIME OF FIRST/LAST OBS as datetime objects.
    """
    times = gr.gettime(obs_file)
    if len(times) < 2:
        raise ValueError("[ERROR] Not enough epochs in obs file.")
    t_start = times[0].astype("datetime64[ms]").astype(datetime)
    t_end   = times[-1].astype("datetime64[ms]").astype(datetime)
    return t_start, t_end




def download_file(url: str, dest: Path) -> bool:
    """
    Download a .gz file from the given URL, extract its contents, and remove the .gz file.

    Parameters:
        url (str): The URL of the file to download.
        dest (Path): The destination path to save the .gz file.

    Returns:
        bool: True if the file was successfully downloaded and extracted, False otherwise.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)

        # If decompressed file already exists, skip download
        decompressed_path = dest.with_suffix('')
        if decompressed_path.exists():
            print(f"[INFO] Already exists: {decompressed_path.name}, skipping download.")
            return True

        # If .gz file already exists, remove it first
        if dest.exists():
            try:
                dest.unlink()
            except Exception as e:
                print(f"[WARNING] Could not remove existing file: {dest} ({e})")
                return False

        # Download the .gz file
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            with open(dest, 'wb') as f:
                f.write(response.content)
            print(f"[INFO] Downloaded: {dest.name}")

            # Decompress the .gz file
            with gzip.open(dest, 'rb') as f_in:
                with open(decompressed_path, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            time.sleep(2)

            # Remove the original .gz file
            dest.unlink()
            return True
        else:
            print(f"[WARNING] Failed ({response.status_code}): {url}")
            return False

    except Exception as e:
        print(f"[ERROR] Download failed: {url}\n{e}")
        return False


def download_ephemeris(
    utc_time: datetime,
    CODE_products: bool,
    type: str = "FINAL",  # or RAPID
    igs_dir: Path = Path("temp/ephemeris")
) -> bool:

    yyyy, ddd, wwww, gps_day, gps_tow = utc_to_gps(utc_time)
    dest_files = []

    if type == "FINAL":
        urls = [
            f"http://garner.ucsd.edu/pub/products/{wwww}/IGS0OPSFIN_{yyyy}{ddd:03d}0000_01D_15M_ORB.SP3.gz",
            f"http://garner.ucsd.edu/pub/products/{wwww}/IGS0OPSFIN_{yyyy}{ddd:03d}0000_01D_30S_CLK.CLK.gz"
        ]
        if CODE_products:
            urls += [
                f"http://garner.ucsd.edu/pub/products/{wwww}/COD0OPSFIN_{yyyy}{ddd:03d}0000_01D_05M_ORB.SP3.gz",
                f"http://garner.ucsd.edu/pub/products/{wwww}/COD0OPSFIN_{yyyy}{ddd:03d}0000_01D_05S_CLK.CLK.gz"
            ]

    elif type == "RAPID":
        urls = [
            f"http://garner.ucsd.edu/pub/products/{wwww}/IGS0OPSRAP_{yyyy}{ddd:03d}0000_01D_15M_ORB.SP3.gz",
            f"http://garner.ucsd.edu/pub/products/{wwww}/IGS0OPSRAP_{yyyy}{ddd:03d}0000_01D_05M_CLK.CLK.gz"
        ]
        if CODE_products:
            urls += [
                f"http://garner.ucsd.edu/pub/products/{wwww}/COD0OPSRAP_{yyyy}{ddd:03d}0000_01D_05M_ORB.SP3.gz",
                f"http://garner.ucsd.edu/pub/products/{wwww}/COD0OPSRAP_{yyyy}{ddd:03d}0000_01D_30S_CLK.CLK.gz"
            ]

    for url in urls:
        filename = url.split("/")[-1]
        dest = igs_dir / filename
        if download_file(url, dest):
            dest_files.append(Path(dest))

    return dest_files


def download_igs_data(
    base_obs_path: Path,
    igs_dir: Path,
    CODE_products: bool = True
) -> str:
    """
    Try to download highest quality IGS data available for the given days in obs file.
    Return level of data obtained: "Final", "Rapid", or "Broadcast".
    """

    # input
    igs_dir = Path.cwd() / "DGT_output" / "PPK_result"

    # Parse start and end time from obs
    utc_start, utc_end = parse_obs_time_range(base_obs_path)

    # Generate all dates in that range (handle cross-day)
    day_list = []
    current = utc_start.date()
    while current <= utc_end.date():
        day_list.append(datetime.combine(current, dt.time.min))
        current += dt.timedelta(days=1)

    # Try FINAL → RAPID
    eph_files = []
    for level in ["FINAL", "RAPID"]:
        success = True
        for d in day_list:
            files = download_ephemeris(
                d, type=level, CODE_products=CODE_products, igs_dir=igs_dir
            )
            if not files:
                success = False
                break
            for f in files:
                f_unzipped = f.with_suffix('')  # remove .gz
                if f_unzipped.suffix.lower() == ".sp3":
                    eph_files.append(Path(f_unzipped))
                elif f_unzipped.suffix.lower() == ".clk":
                    eph_files.append(Path(f_unzipped))

        if success:
            print(f"[INFO] All precise IGS data downloaded successfully! (Product type: {level})")
            return eph_files

    print(f"[WARNING] No precise IGS data found for days {day_list}, fallback to Broadcast")
    return None
