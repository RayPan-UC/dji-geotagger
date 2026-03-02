from pathlib import Path
from datetime import datetime
import pandas as pd
import datetime as dt
import requests
import gzip
import shutil
import time
import georinex as gr
from dji_geotagger.tools.tools import utc2gps


def parse_obs_time_range(obs_file: Path) -> tuple[datetime, datetime]:
    """
    Parse RINEX observation file to extract the first and last epoch as UTC datetime objects.

    Parameters
    ----------
    obs_file : Path
        RINEX observation file (.obs / .rnx).

    Returns
    -------
    tuple[datetime, datetime]
        (t_start, t_end) — timezone-aware UTC datetimes.

    Raises
    ------
    ValueError
        If fewer than 2 epochs are found in the file.
    """
    times = gr.gettime(obs_file)
    if len(times) < 2:
        raise ValueError("[ERROR] Not enough epochs in obs file.")
    t_start = pd.to_datetime(times[0], utc=True).to_pydatetime()
    t_end   = pd.to_datetime(times[-1], utc=True).to_pydatetime()
    return t_start, t_end




def download_file(url: str, dest: Path) -> bool:
    """
    Download a gzip-compressed file, decompress it, and remove the .gz archive.

    The function treats `dest` as the target path for the downloaded `.gz` file.
    After download, it extracts to `dest.with_suffix('')` and deletes the `.gz`.

    Parameters
    ----------
    url : str
        Remote URL of a `.gz` file.
    dest : Path
        Local destination path for the downloaded `.gz`.

    Returns
    -------
    bool
        True if the decompressed output exists (download performed or already present),
        False otherwise.

    Notes
    -----
    - If the decompressed file already exists, the download is skipped.
    - This implementation downloads the response into memory (non-streaming).
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


def ephemeris_downloader(
    utc_time: datetime,
    CODE_products: bool,
    product_type: str = "FINAL",  # or RAPID
    igs_dir: Path = Path("temp/ephemeris")
)-> list[Path]:
    """
    Download and extract precise orbit/clock products for a given UTC day.

    This function constructs product URLs for a GPS day (derived from `utc_time`)
    and downloads each `.gz` file into `igs_dir`. Each archive is decompressed and
    the `.gz` is removed. The returned paths point to the decompressed files.

    Parameters
    ----------
    utc_time : datetime
        Any UTC datetime within the target day (date portion is used).
    CODE_products : bool
        If True, also download CODE products in addition to IGS products.
    product_type : {"FINAL", "RAPID"}, default "FINAL"
        Latency tier:
        - FINAL: highest accuracy, longer latency
        - RAPID: lower latency
    igs_dir : Path, default Path("temp/ephemeris")
        Output directory for downloaded and extracted products.

    Returns
    -------
    list[Path]
        Paths to decompressed product files (e.g., *.SP3, *.CLK).
        Returns an empty list if nothing was downloaded successfully.
    """
    gps_time_dict = utc2gps(utc_time)
    yyyy = gps_time_dict.get("yyyy")
    wwww = gps_time_dict.get("gps_week")
    ddd = gps_time_dict.get("ddd")

    dest_files = []

    if product_type == "FINAL":
        urls = [
            f"http://garner.ucsd.edu/pub/products/{wwww}/IGS0OPSFIN_{yyyy}{ddd}0000_01D_15M_ORB.SP3.gz",
            f"http://garner.ucsd.edu/pub/products/{wwww}/IGS0OPSFIN_{yyyy}{ddd}0000_01D_30S_CLK.CLK.gz"
        ]
        if CODE_products:
            urls += [
                f"http://garner.ucsd.edu/pub/products/{wwww}/COD0OPSFIN_{yyyy}{ddd}0000_01D_05M_ORB.SP3.gz",
                f"http://garner.ucsd.edu/pub/products/{wwww}/COD0OPSFIN_{yyyy}{ddd}0000_01D_05S_CLK.CLK.gz"
            ]

    elif product_type == "RAPID":
        urls = [
            f"http://garner.ucsd.edu/pub/products/{wwww}/IGS0OPSRAP_{yyyy}{ddd}0000_01D_15M_ORB.SP3.gz",
            f"http://garner.ucsd.edu/pub/products/{wwww}/IGS0OPSRAP_{yyyy}{ddd}0000_01D_05M_CLK.CLK.gz"
        ]
        if CODE_products:
            urls += [
                f"http://garner.ucsd.edu/pub/products/{wwww}/COD0OPSRAP_{yyyy}{ddd}0000_01D_05M_ORB.SP3.gz",
                f"http://garner.ucsd.edu/pub/products/{wwww}/COD0OPSRAP_{yyyy}{ddd}0000_01D_30S_CLK.CLK.gz"
            ]

    for url in urls:
        filename = url.split("/")[-1]
        dest = igs_dir / filename
        if download_file(url, dest):
            dest_files.append(Path(dest))

    return dest_files


def download_igs_data(
    base_obs_path: str,
    igs_dir: Path = None,
    CODE_products: bool = True
) -> list[Path]:
    """
    Download precise ephemeris (SP3) and clock (CLK) files covering the observation time span.

    The observation date range is derived from the base station RINEX observation file
    via `parse_obs_time_range()`. For each UTC day in that span, this function attempts:

      1) FINAL products
      2) RAPID products

    If all required days succeed at a given tier, returns the list of decompressed ephemeris files.

    Parameters
    ----------
    base_obs_path : str | Path
        Base station RINEX observation file used to determine required dates.
    igs_dir : Path, optional
        Destination directory for downloaded products. Defaults to:
        <cwd>/DGT_output/PPK_result/Ephemeris
    CODE_products : bool, default True
        If True, also attempt CODE products.

    Returns
    -------
    list[Path]
        Paths to decompressed ephemeris/clock files (*.SP3, *.CLK) for all required days.

    Raises
    ------
    RuntimeError
        If both FINAL and RAPID downloads fail for any required day.
    """

    # input
    base_obs_path = Path(base_obs_path)
    igs_dir = igs_dir if igs_dir else Path.cwd() / "DGT_output" / "PPK_result" / "Ephemeris"

    # Parse start and end time from obs
    utc_start, utc_end = parse_obs_time_range(base_obs_path)

    # Generate all dates in that range (handle cross-day)
    day_list = []
    current = utc_start.date()
    while current <= utc_end.date():
        day_list.append(datetime.combine(current, dt.time.min))
        current += dt.timedelta(days=1)

    # Try FINAL → RAPID
    
    for level in ["FINAL", "RAPID"]:
        success = True
        eph_files = []
        for d in day_list:
            files = ephemeris_downloader(
                d, product_type=level, CODE_products=CODE_products, igs_dir=igs_dir
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
    raise RuntimeError(ephemeris_download_fail())

def ephemeris_download_fail():
    """Return a formatted error message for ephemeris download failure."""
    return("""
[WARNING] Failed to download ephemeris data (.sp3 / .clk).
            - You can manually download them from: https://igs.org/products/#orbits_clocks"
            - Look under FINAL or RAPID products for your observation date
            - For GPS week and date, you can check: https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/calendr.php
        """)