"""
Automated CSRS-PPP submission.

Replaces the manual "upload the .obs on the website, wait for the email,
download the archive, drop the .sum in a folder" round trip with three HTTP
calls. The service is operated by the Canadian Geodetic Survey, Natural
Resources Canada.

Protocol
--------
NRCan publishes no API contract; the following was recovered from the
``<form id='pppform'>`` element of the logged-in ppp.php page and verified
end-to-end on 2026-07-30 against CSRS-PPP v5.15.4.

1. ``POST /CSRS-PPP/service/submit`` (multipart/form-data)
   -> ``200 text/plain``, body is a 64-character processing key
2. ``GET /CSRS-PPP/service/results?id=<key>``
   -> HTML page; once processing finishes it contains download links
3. ``GET /CSRS-PPP/service/results/file?id=<dl_id>&...&type=full``
   -> ZIP archive containing ``.sum`` / ``.pos`` / ``.tro`` / ``.clk`` /
   ``.csv`` / ``.pdf``

No session cookie and no CSRF token are involved: ``user_name`` (the account
email) is the only identity, and every hidden form field is a static value.
This is what makes the flow automatable rather than merely replayable.

Stability
---------
This is an undocumented interface and NRCan may change it without notice. Two
specific risks:

* The submitted ``ppp_access`` field is set to ``real_browser`` by the web
  page. The name implies the backend distinguishes access channels, so this is
  the most likely lever if NRCan decides to restrict automated use.
* Job completion has no explicit signal. The download link is present from the
  moment of submission, and HEAD does not report ``Content-Length`` for a
  completed archive of realistic size. :func:`wait_for_results` therefore
  polls by fetching the archive and testing whether it is a valid ZIP. Should
  NRCan start serving a placeholder archive while queued, that test would
  report completion too early.

Every failure path here raises rather than degrading, so the caller can fall
back to a manually supplied ``.sum`` with the accuracy class unchanged. See
:mod:`dji_geotagger.ppk.base_position` for the source-selection policy.
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path

import requests

from dji_geotagger.tools.logging_setup import get_logger
from dji_geotagger.tools.progress import as_progress

logger = get_logger(__name__)

BASE_URL = "https://webapp.csrs-scrs.nrcan-rncan.gc.ca"
SUBMIT_URL = f"{BASE_URL}/CSRS-PPP/service/submit"
RESULTS_URL = f"{BASE_URL}/CSRS-PPP/service/results"

# Flat rejection body returned for a well-formed multipart POST the service
# cannot interpret. Carries no diagnostic detail.
_REJECT_BODY = "ERROR [SEVERE]"
_INVALID_KEY = "The processing key is not valid."

# Submission keys are a fixed-width URL-safe token.
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{40,120}$")
_LINK_PATTERN = re.compile(r'href="([^"]*results/file\?[^"]*)"')

VALID_PROCESS_TYPES = ("Static", "Kinematic")
VALID_SYSREFS = ("ITRF", "NAD83")
VALID_NAD83_EPOCHS = ("NAD83_CURR", "NAD83_19970101", "NAD83_20020101",
                      "NAD83_20100101", "NAD83_CUSTOM_SELECTED")


class PPPServiceError(RuntimeError):
    """Raised when CSRS-PPP rejects a submission or cannot be reached."""


def submit_rinex(
    rinex_path: str | Path,
    email: str,
    *,
    process_type: str = "Static",
    sysref: str = "ITRF",
    nad83_epoch: str = "NAD83_CURR",
    timeout: int = 900,
) -> str:
    """
    Upload a RINEX observation file to CSRS-PPP.

    Parameters
    ----------
    rinex_path : str | Path
        RINEX observation file, typically the base-station ``.obs``.
    email : str
        CSRS account email. Doubles as the identity and the notification
        address; no password is involved.
    process_type : {"Static", "Kinematic"}, optional
        Use ``"Static"`` for a base station on a fixed mark.
    sysref : {"ITRF", "NAD83"}, optional
        Output reference frame. ``"NAD83"`` yields NAD83(CSRS), which is what
        Canadian deliverables normally require; ``"ITRF"`` is the global frame.
    nad83_epoch : str, optional
        Epoch for NAD83 output; ignored when `sysref` is ``"ITRF"``, matching
        the web form's own behaviour. One of :data:`VALID_NAD83_EPOCHS`.
    timeout : int, optional
        Upload timeout in seconds. Base-station files run to tens of MB.

    Returns
    -------
    str
        The processing key, used to poll for and download results.

    Raises
    ------
    FileNotFoundError
        If `rinex_path` does not exist.
    ValueError
        If an argument is outside the set the web form allows.
    PPPServiceError
        If the service rejects the submission or returns an unrecognised body.
    """
    rinex_path = Path(rinex_path)
    if not rinex_path.exists():
        raise FileNotFoundError(f"[ERROR] RINEX file not found: {rinex_path}")
    if process_type not in VALID_PROCESS_TYPES:
        raise ValueError(f"[ERROR] process_type must be one of "
                         f"{VALID_PROCESS_TYPES}, got {process_type!r}")
    if sysref not in VALID_SYSREFS:
        raise ValueError(f"[ERROR] sysref must be one of {VALID_SYSREFS}, "
                         f"got {sysref!r}")
    if sysref == "NAD83" and nad83_epoch not in VALID_NAD83_EPOCHS:
        raise ValueError(f"[ERROR] nad83_epoch must be one of "
                         f"{VALID_NAD83_EPOCHS}, got {nad83_epoch!r}")

    fields = {
        "return_email": email,
        "user_name": email,
        "process_type": process_type,
        "sysref": sysref,
        # The web form blanks the epoch for ITRF; mirror that exactly.
        "nad83_epoch": "" if sysref == "ITRF" else nad83_epoch,
        "nad83_custom": "1997-01-01",
        "v_datum": "cgvd28",
        "v_datuml": "cgvd28",
        "cmd_process_type": "std",
        "dataName": rinex_path.name,
        "otlName": "",
        "ppp_access": "real_browser",
        "language": "en",
        "force_float_choice": "no",
        # 'lite' keeps the PDF small; the .sum is unaffected either way.
        "output_pdf": "lite",
        "official_marker": "",
    }

    size_mb = rinex_path.stat().st_size / 1e6
    logger.info(f"Submitting {rinex_path.name} ({size_mb:.1f} MB) to "
                f"CSRS-PPP [{process_type}, {sysref}]")

    try:
        with rinex_path.open("rb") as handle:
            response = requests.post(
                SUBMIT_URL,
                data=fields,
                files={"rfile_upload": (rinex_path.name, handle,
                                        "application/octet-stream")},
                headers={"Referer": f"{BASE_URL}/geod/tools-outils/ppp.php"},
                timeout=timeout,
            )
    except requests.RequestException as exc:
        raise PPPServiceError(
            f"[ERROR] Could not reach CSRS-PPP: {exc}") from exc

    body = (response.text or "").strip()

    if response.status_code != 200:
        raise PPPServiceError(
            f"[ERROR] CSRS-PPP returned HTTP {response.status_code}. "
            f"The service may be down or the interface may have changed.")
    if body == _REJECT_BODY:
        raise PPPServiceError(
            "[ERROR] CSRS-PPP rejected the submission (ERROR [SEVERE]). "
            "This usually means the RINEX file is unreadable, or NRCan has "
            "changed the submission form fields.")
    if not _KEY_PATTERN.match(body):
        raise PPPServiceError(
            f"[ERROR] Unexpected response from CSRS-PPP; expected a "
            f"processing key, got: {body[:200]!r}")

    # Print the key in full: it is the only handle on a submitted job. If the
    # poll loop is interrupted, this is what lets the user recover the results
    # instead of resubmitting.
    logger.info(f"Accepted. Processing key:\n       {body}")
    return body


def wait_for_results(
    key: str,
    *,
    poll_interval: int = 30,
    timeout: int = 3600,
    progress=None,
) -> bytes:
    """
    Poll until CSRS-PPP has finished processing, then return the archive.

    Parameters
    ----------
    key : str
        Processing key from :func:`submit_rinex`.
    poll_interval : int, optional
        Seconds between polls. Kept generous: this is a shared public service.
    timeout : int, optional
        Give up after this many seconds.
    progress : Progress, optional
        Progress reporting and cancellation. This loop can run for the best
        part of an hour, so the wait between polls is interruptible rather
        than a plain sleep.

    Returns
    -------
    bytes
        Content of the full result archive.

    Raises
    ------
    PPPServiceError
        If the key is rejected, or processing does not finish within `timeout`.

    Notes
    -----
    Neither of the two obvious completion signals works:

    * **Download links are present from the moment of submission**, while the
      archive behind them does not yet exist. Fetching one then returns an
      empty body.
    * **HEAD is not usable either.** A completed 296 kB archive returns no
      ``Content-Length`` at all - only trivially small archives (a ~700 byte
      error bundle) carry the header.

    Completion is therefore established by fetching the archive and checking
    that it is a valid, non-empty ZIP. The bytes are returned so the download
    is not repeated.
    """
    progress = as_progress(progress)
    waited = 0
    while True:
        progress.update("ppp", f"Waiting for CSRS-PPP ({waited}s)",
                        current=waited, total=timeout)
        try:
            response = requests.get(RESULTS_URL, params={"id": key},
                                    timeout=60)
        except requests.RequestException as exc:
            raise PPPServiceError(
                f"[ERROR] Could not reach CSRS-PPP results: {exc}") from exc

        body = response.text or ""
        if _INVALID_KEY in body:
            raise PPPServiceError(
                "[ERROR] CSRS-PPP rejected the processing key. It may have "
                "expired, or the submission failed.")

        full = [link for link in _LINK_PATTERN.findall(body)
                if "type=full" in link]
        if full:
            archive = _try_fetch_archive(full[0])
            if archive is not None:
                logger.info(f"Processing complete ({waited}s, "
                            f"{len(archive) / 1e3:.0f} kB).")
                return archive

        if waited >= timeout:
            raise PPPServiceError(
                f"[ERROR] CSRS-PPP did not return results within {timeout}s. "
                f"The job may still be queued. Retrieve it later with this "
                f"key:\n        {key}")

        logger.info(f"Still processing... ({waited}s elapsed)")
        # Cancellable: a bare sleep would make a cancel request wait out the
        # full poll interval before being noticed.
        progress.sleep(poll_interval)
        waited += poll_interval


def _try_fetch_archive(url: str, timeout: int = 600) -> bytes | None:
    """
    Fetch the result archive if it exists yet.

    Returns
    -------
    bytes | None
        Archive content, or ``None`` while the job is still processing (the
        URL is live from submission onward but serves an empty body until the
        archive has been written).
    """
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException:
        # Transient network trouble mid-poll: treat as not-ready and retry on
        # the next cycle rather than failing the whole run.
        return None
    if response.status_code != 200 or not response.content:
        return None
    if not zipfile.is_zipfile(io.BytesIO(response.content)):
        return None
    return response.content


def save_results(archive_bytes: bytes, out_dir: str | Path) -> Path:
    """
    Write the result archive to disk and extract the ``.sum`` file.

    Parameters
    ----------
    archive_bytes : bytes
        Archive content from :func:`wait_for_results`.
    out_dir : str | Path
        Directory to write the archive and extracted files into.

    Returns
    -------
    Path
        Path to the extracted ``.sum`` file.

    Raises
    ------
    PPPServiceError
        If the archive contains no ``.sum``, which is how a failed job
        presents: a well-formed but result-free bundle.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    archive_path = out_dir / "CSRS-PPP_full_output.zip"
    archive_path.write_bytes(archive_bytes)

    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        # Extract everything: the .pos, .csv and .pdf are what the user would
        # have received manually, and are worth keeping alongside the .sum.
        archive.extractall(out_dir)
        sums = [n for n in names if n.lower().endswith(".sum")]
        diagnostics = _read_diagnostics(archive, names)

    if not sums:
        # A failed job still returns a well-formed archive - it simply holds
        # an errors bundle instead of results. Roughly 700 bytes rather than
        # the ~300 kB of a successful run.
        raise PPPServiceError(
            "[ERROR] CSRS-PPP processing failed; the archive contains no "
            "solution.\n"
            f"        Archive contents: {names}\n"
            f"        {diagnostics}\n"
            "        Common causes: the observation session is too short to "
            "converge, the RINEX file is malformed, or a moving receiver was "
            "submitted in Static mode."
        )

    sum_path = out_dir / sums[0]
    logger.info(f"Extracted PPP summary: {sum_path.name}")
    return sum_path


def _read_diagnostics(archive: zipfile.ZipFile, names: list[str]) -> str:
    """
    Pull whatever explanation the service bundled into a failed archive.

    Successful runs carry a plain ``errors.txt``; failed ones carry a nested
    ``errors.zip`` instead.
    """
    for name in names:
        if name.lower().endswith("errors.txt"):
            text = archive.read(name).decode("utf-8", "replace").strip()
            return f"Service diagnostics: {text[:500]}" if text else ""
    for name in names:
        if name.lower().endswith("errors.zip"):
            try:
                import io
                with zipfile.ZipFile(io.BytesIO(archive.read(name))) as inner:
                    parts = [
                        inner.read(n).decode("utf-8", "replace").strip()
                        for n in inner.namelist()
                    ]
                joined = " | ".join(p for p in parts if p)
                return f"Service diagnostics: {joined[:500]}"
            except zipfile.BadZipFile:
                return "Service diagnostics: errors.zip present but unreadable"
    return ""


def run_online_ppp(
    rinex_path: str | Path,
    email: str,
    out_dir: str | Path,
    *,
    process_type: str = "Static",
    sysref: str = "ITRF",
    nad83_epoch: str = "NAD83_CURR",
    poll_interval: int = 30,
    timeout: int = 3600,
    progress=None,
) -> Path:
    """
    Submit, wait and download in one call.

    Parameters
    ----------
    rinex_path : str | Path
        Base-station RINEX observation file.
    email : str
        CSRS account email.
    out_dir : str | Path
        Where to place the archive and extracted results.
    process_type, sysref, nad83_epoch
        Passed to :func:`submit_rinex`.
    poll_interval, timeout
        Passed to :func:`wait_for_results`.

    Returns
    -------
    Path
        Path to the extracted ``.sum`` file, ready for
        :func:`~dji_geotagger.ppk.PPP_sum_parser.sum_file_parser`.
    """
    key = submit_rinex(rinex_path, email, process_type=process_type,
                       sysref=sysref, nad83_epoch=nad83_epoch)
    archive = wait_for_results(key, poll_interval=poll_interval,
                               timeout=timeout, progress=progress)
    return save_results(archive, out_dir)
