"""
Desktop front end - window, and the Python side of the JavaScript bridge.

Layout is a control panel rather than a wizard. A geotagging run is something
you repeat with small variations - a different flight folder, a different
delivery CRS, a resubmitted PPP - so every setting stays visible and editable
at all times. A wizard would charge six clicks to get back to the one field
that changed.

Two run buttons, not one. Resolving the base station is separated from the
main run for the reason given in ``example/Example_1.py``: PPP takes minutes,
and every flight downstream inherits whatever it returns. The coordinates get
shown and wait for a human to look at them before anything else starts.

File paths come from native dialogs. The browser File API hands JavaScript a
file *handle*, not a path, and Chromium removed ``File.path`` - so a dropped
folder cannot be turned into something ``open()`` accepts. That matters here
because a flight folder is gigabytes of imagery that Python must read
directly. Drag-and-drop is a later addition on top of the dialogs, never a
replacement for them.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import subprocess
import threading
import webbrowser
import traceback
from datetime import datetime
from importlib import metadata
from pathlib import Path

import pandas as pd
import webview
from PIL import Image
from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_crs_info
from pyproj.enums import PJType

# Header positions are ECEF on WGS 84; metre-level, so the ensemble's own
# ambiguity is irrelevant here. Only ever used to place a map pin.
_ECEF_TO_LLH = Transformer.from_crs(4978, 4326, always_xy=True)

from dji_geotagger.core.geotag import geotag
from dji_geotagger.core.mrk_parser import mrk2df
from dji_geotagger.core.transform import (
    FRAME_EPSG,
    _AMBIGUOUS_TOKENS,
    make_utm_crs,
    rebase_projected_crs,
    transform_coordinates,
)
from dji_geotagger.ppk.base_position import resolve_base_position
from dji_geotagger.ppk.PPP_sum_parser import sum_file_parser
from dji_geotagger.ppk.raw_converter import raw2rinex
from dji_geotagger.ppk.time_check import parse_obs_time_range
from dji_geotagger.tools.tools import utc2gps
from dji_geotagger.tools.logging_setup import configure_logging
from dji_geotagger.tools.progress import OperationCancelled, Progress

_WEB_DIR = Path(__file__).parent / "web"

# Extensions that identify an already-converted RINEX observation file. The
# numbered form is the classic RINEX 2 convention, where the digits are the
# two-digit year: DRTK3_0038.25o.
_RINEX_OBS_SUFFIXES = {".obs", ".rnx"}

_PHOTO_SUFFIXES = {".jpg", ".jpeg"}

# Points kept per flight track on the map. A lawnmower pattern is recognisable
# from a few hundred vertices, and drawing every exposure of a 160-flight
# survey would put a million of them into the page for no gain in meaning.
_TRACK_MAX_POINTS = 400

# Filenames listed in a coverage warning before it just says "and N more".
_MAX_LISTED_NAMES = 40

# Camera centres drawn on the map. Canvas keeps this many interactive without
# trouble; a whole survey can be several times more, and past this the map is
# a solid block of colour anyway.
_MAX_MAP_POINTS = 6000

# GPS week zero.
_GPS_EPOCH = datetime(1980, 1, 6)

# Shipped alongside the compiled binaries, as BSD-2-Clause asks.
_RTKLIB_LICENSE = (Path(__file__).resolve().parents[1]
                   / "tools" / "RTKLIB" / "bin" / "LICENSE.txt")

# Remembered preferences. Beside the user's own profile rather than in the
# package, so an upgrade or a reinstall does not discard them.
_SETTINGS_FILE = Path.home() / ".dji_geotagger" / "gui_settings.json"

# Left for the window, the ephemeris downloads and the operating system.
_RESERVED_CORES = 2

# What a .sum's frame token may read as, per the option that was requested.
_FRAME_ALIASES = {
    "ITRF": {"IGB20", "IGS20", "ITRF2020"},
    "NAD83": {"NAD83", "NAD83(CSRS)", "NAD83(CSRS)V8"},
}

# Offered by default. Not the ceiling: throughput is limited by the shared base
# observation file long before it is limited by cores.
_DEFAULT_WORKERS = 4


def _gps_seconds(moment) -> float:
    """
    Absolute GPS seconds since 1980-01-06, for a RINEX observation time.

    No leap-second conversion. RINEX observation epochs are already on the GPS
    time scale - `parse_obs_time_range` returns them verbatim, merely labelled
    UTC - and the MRK's week/time-of-week is GPS too, so both sides already
    agree.

    Running them through `utc2gps` added the 18 s offset twice over. On a
    five-hour base window that is invisible; against a fifteen-minute flight
    it moved the window far enough to report a dozen good exposures as
    falling outside it. Checked against a solved trajectory: the rover
    observations of DJI_202507221213_002 start at GPS 239617.000, which is
    exactly where RTKLIB's .pos begins, and 239635.000 by the converted route.
    """
    delta = moment.replace(tzinfo=None) - _GPS_EPOCH
    return delta.total_seconds()


def _rinex_obs_patterns() -> list[str]:
    """
    Every extension that can name a RINEX observation file.

    RINEX 2 names them by two-digit year - ``.25o`` - and pywebview's filter
    grammar has no ``?`` wildcard, so the year has to be expanded into one
    literal extension each. The range ends two years ahead of today rather
    than at a fixed year, so it keeps covering new data without an edit.
    """
    this_year = datetime.now().year
    return ["*.obs", "*.rnx"] + [
        f"*.{y % 100:02d}o" for y in range(2000, this_year + 3)
    ]


def _obs_file_filters() -> tuple[str, ...]:
    """
    Filters for the base observation dialog, widest first.

    The default entry accepts raw and RINEX together. Both are valid input and
    the extension decides which path is taken, so making the user first choose
    a category and only then a file would be asking a question the program can
    answer for itself.

    pywebview validates each string against ``^([\\w ]+)\\((\\*(?:\\.(?:\\w+|
    \\*))*(?:;\\*(?:\\.(?:\\w+|\\*))*)*)\\)$``, which permits no punctuation in
    the description - hence "CSRS PPP", not "CSRS-PPP", elsewhere in this file.
    """
    rinex = _rinex_obs_patterns()
    return (
        "GNSS observations (" + ";".join(["*.dat"] + rinex) + ")",
        "DJI raw log (*.dat)",
        "RINEX observation (" + ";".join(rinex) + ")",
        "All files (*.*)",
    )


def _classify_base_input(path: Path) -> str:
    """
    Decide whether a chosen base file is a raw logger dump or RINEX already.

    Returns ``"raw"``, ``"rinex"`` or ``"unknown"``. The distinction drives
    whether the antenna height field applies: it is consumed during raw
    conversion, and a RINEX file already has it baked into the header.
    """
    suffix = path.suffix.lower()
    if suffix == ".dat":
        return "raw"
    if suffix in _RINEX_OBS_SUFFIXES:
        return "rinex"
    # RINEX 2 observation files end in a two-digit year followed by "o".
    if len(suffix) == 4 and suffix[1:3].isdigit() and suffix[3] == "o":
        return "rinex"
    return "unknown"


# Depth guard for the flight search. Survey folders are nested by date, site
# and sensor, so several levels are normal - but an accidental pick of C:\ must
# not walk the whole disk.
_MAX_SEARCH_DEPTH = 8
_MAX_SEARCH_DIRS = 20000

# A DJI flight folder is named DJI_<timestamp>_<index>[_<mission name>].
_DJI_FLIGHT_DIR = re.compile(r"^DJI_\d{8,}_\d{3}(_.*)?$", re.IGNORECASE)


# The only columns that may be rescaled. Named explicitly rather than matched
# on "sigma", because a name match cannot tell a standard deviation from a
# variance and would leave a full_output file self-contradictory: sigma_E at
# 95% beside cov_total_ECEF still in 1-sigma m^2.
_SCALABLE_SIGMA_COLS = ("sigma_E", "sigma_N", "sigma_U")

# Present only with full_output; second moments, so k would have to be squared
# and the column is an array or a matrix. Left alone, and said so.
_UNSCALED_COV_COLS = ("cov_total_ECEF", "sigma_total_ECEF")


def _scale_sigma(df, k: float, label: str):
    """
    Express the uncertainty columns at the requested confidence level.

    Everything the pipeline stores is 1-sigma, which is what makes a single
    multiplier legitimate:

    * CSRS-PPP reports 95% and ``PPP_sum_parser`` divides by 1.96 on the way
      in (``sigma_solution = ... / 1.96``);
    * RTKLIB's ``.pos`` gives standard deviations, squared into variances by
      ``pos_cov_wrapper``.

    So the combined covariance is 1-sigma throughout and ``sigma_E/N/U`` are
    its square roots.

    The columns are **renamed** as well as scaled - ``sigma_E`` becomes
    ``sigma_E_95``. Writing a 95% figure into a column still called
    ``sigma_E`` would hand the next reader a number wrong by a factor of two
    with nothing on the file to say so.
    """
    logger = logging.getLogger("dji_geotagger")
    if k == 1.0:
        return df

    columns = [c for c in _SCALABLE_SIGMA_COLS if c in df.columns]
    if not columns:
        logger.warning("[WARN] None of %s present; nothing rescaled to %s.",
                       ", ".join(_SCALABLE_SIGMA_COLS), label or f"k={k:g}")
        return df

    suffix = (label.split()[0].rstrip("%") if label else f"k{k:g}")
    df = df.copy()
    for column in columns:
        df[column] = df[column] * k

    left = [c for c in _UNSCALED_COV_COLS if c in df.columns]
    if left:
        logger.warning("[WARN] %s left at 1-sigma: second moments would need "
                       "k^2, and the CSV cannot label them per column.",
                       ", ".join(left))

    logger.info("[INFO] Rescaled to %s (k=%.3f): %s",
                label or suffix, k, ", ".join(columns))
    return df.rename(columns={c: f"{c}_{suffix}" for c in columns})


# How far into a raw log to look for the station position. The DRTK-3 sends
# RTCM 1006 about once a second, and the first frame lands within the first
# few kilobytes; this is slack, not a requirement.
_RTCM_SCAN_BYTES = 2_000_000


def _rtcm_bits(payload: bytes, start: int, length: int, signed: bool = False) -> int:
    """Read a big-endian bit field out of an RTCM payload."""
    value = 0
    for i in range(start, start + length):
        value = (value << 1) | ((payload[i >> 3] >> (7 - (i & 7))) & 1)
    if signed and value >= (1 << (length - 1)):
        value -= 1 << length
    return value


def _rtcm_station_position(path: Path) -> tuple[float, float, float] | None:
    """
    Recover the station position a DJI raw log broadcasts.

    The DRTK-3 writes RTCM 3, and messages 1005/1006 carry the antenna
    reference point in ECEF at 0.1 mm resolution. That is where convbin gets
    the RINEX ``APPROX POSITION XYZ`` from - decoding it here gives the same
    answer without spending a conversion first. Verified against a real log:
    2703 frames, all identical, agreeing with the converted header to under a
    millimetre.

    Like the header value it is a broadcast single-point position, metres from
    the truth, and it excludes the antenna height the user has yet to enter.
    Good for a map pin, nothing else.

    Frames are accepted only once two of them agree, which stands in for the
    CRC that is not checked here: a stray 0xD3 in the data stream can start a
    plausible-looking frame, but not twice with the same coordinates.
    """
    try:
        data = path.read_bytes()[:_RTCM_SCAN_BYTES]
    except OSError:
        return None

    first = None
    index = 0
    limit = len(data) - 6
    while index < limit:
        if data[index] != 0xD3:
            index += 1
            continue

        length = ((data[index + 1] & 0x03) << 8) | data[index + 2]
        if length < 19 or index + 6 + length > len(data):
            index += 1
            continue

        payload = data[index + 3:index + 3 + length]
        message = (payload[0] << 4) | (payload[1] >> 4)

        if message in (1005, 1006):
            position = tuple(
                _rtcm_bits(payload, offset, 38, signed=True) * 0.0001
                for offset in (34, 74, 114)
            )
            if first is None:
                first = position
            elif all(abs(a - b) < 0.01 for a, b in zip(first, position)):
                return first

        index += 6 + length

    return None


def _find_nav(obs: Path) -> Path | None:
    """
    Locate the broadcast navigation file belonging to an observation file.

    Only relevant when the user supplies RINEX directly - conversion returns
    both halves itself. RINEX 3 uses ``.nav``; RINEX 2 splits by constellation
    and encodes the year, so ``DRTK3.25o`` pairs with ``DRTK3.25p`` (mixed),
    ``.25n`` (GPS) or ``.25g`` (GLONASS).
    """
    for candidate in sorted(obs.parent.glob(obs.stem + ".*")):
        suffix = candidate.suffix.lower()
        if suffix == ".nav":
            return candidate
        if len(suffix) == 4 and suffix[1:3].isdigit() and suffix[3] in "png":
            return candidate
    return None


def _summarise_flight(folder: Path, filenames: list[str], root: Path) -> dict:
    """
    Describe one candidate flight folder well enough to show a row for it.

    The display name is relative to the chosen survey folder rather than just
    the leaf: once the search recurses, two flights from different missions can
    easily share a leaf name, and a list of identical rows is unusable.
    """
    photos = 0
    mrk = None
    for name in filenames:
        suffix = Path(name).suffix.lower()
        if suffix in _PHOTO_SUFFIXES:
            photos += 1
        elif suffix == ".mrk":
            mrk = name

    try:
        label = str(folder.relative_to(root))
    except ValueError:
        label = folder.name

    return {"path": str(folder), "name": label or folder.name,
            "root": str(root), "photos": photos, "mrk": mrk, "error": None}


def _find_flights(root: Path) -> list[dict]:
    """
    Walk `root` for anything that looks like a flight.

    A survey is not reliably one level deep. Three missions may sit in one
    subfolder and six in another, and the sensor model often adds a level of
    its own - so the search recurses instead of assuming a shape, and the
    caller points at the top of the survey once.

    A directory qualifies when it holds an MRK file. Failing that, it still
    qualifies if it holds photos *and* carries a DJI flight folder name - which
    is how a mission with a missing MRK stays visible in the list instead of
    vanishing from it.

    "Any folder with photos" was the first rule and it does not survive
    recursion. On a real survey drive it returned Pix4D thumbnail caches, SDK
    sample datasets and report HTML - twenty-one entries, none of them flights.
    """
    root_depth = len(root.parts)
    flights: list[dict] = []
    visited = 0

    for dirpath, dirnames, filenames in os.walk(root):
        visited += 1
        if visited > _MAX_SEARCH_DIRS:
            break

        folder = Path(dirpath)

        # Prune before descending: hidden and system directories never hold
        # flights, and the depth cap stops a mis-click at a drive root.
        dirnames[:] = sorted(
            n for n in dirnames if not n.startswith((".", "$", "__"))
        )
        if len(folder.parts) - root_depth >= _MAX_SEARCH_DEPTH:
            dirnames.clear()

        summary = _summarise_flight(folder, filenames, root)
        if summary["mrk"] or (summary["photos"]
                              and _DJI_FLIGHT_DIR.match(folder.name)):
            flights.append(summary)

    return flights


# Set while a thread is inside a synchronous js_api call. The log bridge pushes
# records with evaluate_js, which needs the page to respond - and the page is
# blocked awaiting that very call, so the two wait on each other. Records from
# such a thread are dropped rather than deadlocking the window.
_no_bridge = threading.local()


class _Quiet:
    """Suppress log forwarding on this thread for the duration of a block."""

    def __enter__(self):
        _no_bridge.on = True
        return self

    def __exit__(self, *exc):
        _no_bridge.on = False
        return False


class _LogBridge(logging.Handler):
    """
    Forward the library's own log records into the window's Log tab.

    The pipeline already narrates itself in detail - which ephemeris tier was
    used, how many photos matched, why a flight was skipped. Reproducing that
    as GUI status messages would mean maintaining a second, worse account of
    the same events, so the existing one is piped through instead.
    """

    def __init__(self, emit) -> None:
        super().__init__()
        self._emit = emit
        # Message only. The level travels as its own field so the window can
        # render it consistently - the library writes "[INFO]" into some
        # messages and not others, which is why the log used to look
        # half-prefixed.
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(_no_bridge, "on", False):
            return
        try:
            self._emit("log", {"level": record.levelname,
                               "text": self.format(record)})
        except Exception:  # noqa: BLE001 - a broken display must not stop a run
            pass


def _base_summary(base: dict) -> dict:
    """
    Reduce a base position to what the result card shows.

    Sigma leaves as 1-sigma in metres. The confidence multiplier is applied at
    display time, so that one stored number cannot drift out of step with the
    label describing it.
    """
    sigma = base.get("PPP_sigma_ENU")
    return {
        "source": base.get("source"),
        "source_detail": base.get("source_detail"),
        "mode": base.get("mode"),
        "coord_sys": base.get("coord_sys"),
        "epoch": base.get("epoch"),
        "epoch_decimal_year": base.get("epoch_decimal_year"),
        "epoch_propagated": bool(base.get("epoch_propagated")),
        "velocity_model": base.get("velocity_model"),
        "lat_dd": base.get("lat_dd"),
        "lon_dd": base.get("lon_dd"),
        "hgt": base.get("hgt"),
        "uncertainty_available": bool(base.get("uncertainty_available")),
        "sigma_ENU": [float(v) for v in sigma] if sigma is not None else None,
    }


class Api:
    """
    Methods callable from JavaScript as ``window.pywebview.api.<name>()``.

    Everything here is intentionally thin: dialogs, filesystem inspection and
    state hand-off. The pipeline itself is not wired up yet.
    """

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()

        # Carried between steps: the converted RINEX pair and the resolved
        # base, so that a run does not repeat work the user already watched.
        self.base_obs: Path | None = None
        self.base_nav: Path | None = None
        self.base_position: dict | None = None
        self.sum_path: Path | None = None

        # The finished table, kept so the map and the statistics can be built
        # from it without reading the CSV back.
        self.result = None
        self.result_raw = None
        self.output_path: Path | None = None
        self.flight_dirs: dict[str, Path] = {}

    def _bind(self, window: webview.Window) -> None:
        self._window = window
        handler = _LogBridge(self._emit)
        handler.setLevel(logging.INFO)
        logging.getLogger("dji_geotagger").addHandler(handler)

    # -- event channel ----------------------------------------------------

    def _emit(self, kind: str, payload: dict) -> None:
        """
        Push one event to the page.

        Called from worker threads as well as the main one. `evaluate_js`
        marshals across for us; failures are swallowed because the window may
        already be closing while a thread is still winding down.
        """
        if self._window is None:
            return
        message = json.dumps({"kind": kind, **payload})
        try:
            self._window.evaluate_js(f"window.onPipelineEvent({message})")
        except Exception:  # noqa: BLE001
            pass

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def cancel(self) -> None:
        """Ask the running step to stop at its next checkpoint."""
        self._cancel.set()

    # -- file selection ---------------------------------------------------

    def pick_base_file(self) -> dict | None:
        """Choose the base station observation file, raw or RINEX."""
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=_obs_file_filters(),
        )
        if not result:
            return None
        path = Path(result[0])
        return {"path": str(path), "name": path.name,
                "kind": _classify_base_input(path)}

    def pick_sum_file(self) -> dict | None:
        """Choose an existing CSRS-PPP summary file."""
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            # No hyphen: pywebview rejects punctuation in the description.
            file_types=("CSRS PPP summary (*.sum)", "All files (*.*)"),
        )
        if not result:
            return None
        path = Path(result[0])
        return {"path": str(path), "name": path.name}

    def pick_survey_folder(self) -> dict | None:
        """
        Choose one folder and discover the flights inside it.

        The native dialog takes a single directory at a time, so adding a dozen
        missions one by one would be a dozen trips through the file browser to
        express "all of them". The top of the survey is chosen once instead and
        searched recursively - three flights may sit in one subfolder and six
        in another, and the sensor model often adds a level of its own, so no
        fixed depth is assumed. The chosen folder is included in the search, so
        pointing straight at a single flight also works.

        Several folders can be chosen at once where the platform's folder
        dialog allows it, and the caller accumulates across calls either way -
        so a survey split over two cards, or two dates processed against one
        base station, is a second click rather than a second run.
        """
        try:
            result = self._window.create_file_dialog(
                webview.FileDialog.FOLDER, allow_multiple=True)
        except (TypeError, ValueError):
            # Not every backend accepts multiple folders; one at a time still
            # works because the caller appends rather than replaces.
            result = self._window.create_file_dialog(webview.FileDialog.FOLDER)

        if not result:
            return None
        if isinstance(result, str):
            result = [result]

        roots, flights, errors = [], [], []
        for entry in result:
            root = Path(entry)
            roots.append(str(root))
            try:
                flights.extend(_find_flights(root))
            except OSError as exc:
                errors.append(f"{root}: {exc}")

        # Somewhere to put the result without asking. Beside the data it came
        # from, in its own directory so the output never mixes with imagery.
        # Created at write time, not now - choosing a folder should not leave
        # anything behind on disk.
        suggested = (Path(roots[0]) / "DGT_output" / "geotag.csv") if roots else None

        return {"roots": roots, "flights": flights,
                "suggested_output": str(suggested) if suggested else None,
                "error": "; ".join(errors) or None}

    def pick_output_file(self) -> str | None:
        """Choose where the result CSV is written."""
        result = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename="geotag.csv",
            file_types=("CSV (*.csv)", "All files (*.*)"),
        )
        if not result:
            return None
        # The save dialog returns a bare string on some backends and a tuple on
        # others; normalise before it reaches JavaScript.
        return str(result[0]) if isinstance(result, (list, tuple)) else str(result)

    # -- settings ---------------------------------------------------------

    def about(self) -> dict:
        """
        Version, licensing and the limits of what the numbers mean.

        The third-party list is not decoration: RTKLIB ships here as compiled
        binaries and Leaflet as source, both under BSD-2-Clause, which asks
        that the copyright notice travel with a binary distribution. The full
        texts sit beside the files themselves; this makes them findable.
        """
        try:
            version = metadata.version("dji-geotagger")
        except metadata.PackageNotFoundError:
            version = "development"

        rtklib = _RTKLIB_LICENSE
        return {
            "version": version,
            "licence": "BSD 2-Clause  ·  Copyright (c) 2025, Ray Pan",
            "third_party": [
                {"name": "RTKLIB 2.4.3",
                 "who": "Copyright (c) 2007-2020, T. Takasu",
                 "licence": "BSD 2-Clause",
                 "where": str(rtklib) if rtklib.exists() else None},
                {"name": "Leaflet 1.7.1",
                 "who": "Copyright (c) 2010-2019, Vladimir Agafonkin; "
                        "(c) 2010-2011, CloudMade",
                 "licence": "BSD 2-Clause",
                 "where": str(_WEB_DIR / "vendor" / "leaflet.js")},
            ],
        }

    def suggest_workers(self) -> dict:
        """
        How many flights this machine can sensibly solve at once.

        The ceiling scales with the hardware: `rnx2rtkp` saturates whatever
        core it gets, so a 22-core workstation should not be held to the same
        limit as a 4-core laptop. Two cores are reserved rather than one, since
        the window, the ephemeris downloads and the operating system all want
        time while a run is going.

        The default is lower than the ceiling on purpose. Every worker reads
        the same base observation file, so throughput stops tracking core count
        well before the cores run out - measured 2.35x from three workers. Ask
        for more only if the disk turns out to keep up.
        """
        cores = os.cpu_count() or 2
        ceiling = max(1, cores - _RESERVED_CORES)
        return {"cores": cores,
                "max": ceiling,
                "suggested": max(1, min(_DEFAULT_WORKERS, ceiling))}

    def load_settings(self) -> dict:
        """Read remembered preferences, or an empty dict if there are none."""
        try:
            with _SETTINGS_FILE.open(encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_settings(self, changes: dict) -> bool:
        """
        Merge `changes` into the settings file.

        Merging rather than replacing so that a window which only knows about
        one key cannot drop the others - a later version storing more will
        share this file with an older one.
        """
        settings = self.load_settings()
        settings.update(changes or {})
        try:
            _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _SETTINGS_FILE.open("w", encoding="utf-8") as handle:
                json.dump(settings, handle, indent=2)
            return True
        except OSError as exc:
            logging.getLogger("dji_geotagger").warning(
                "[WARN] Could not save settings: %s", exc)
            return False

    # -- provisional base position ----------------------------------------

    def approximate_base(self, path: str) -> dict | None:
        """
        Read the position a RINEX file already declares in its header.

        Every RINEX observation file carries ``APPROX POSITION XYZ`` - the
        receiver's own single-point fix, good to a few metres. That is useless
        for photogrammetry but perfectly good for putting a pin on a map, and
        it costs one header read instead of a CSRS-PPP round trip.

        A raw log is answered from its RTCM station messages, which is the
        same number by a shorter route - no conversion, and therefore no need
        for the antenna height the user may not have entered yet.
        """
        obs = Path(path)

        if obs.suffix.lower() == ".dat":
            ecef = _rtcm_station_position(obs)
            if ecef is None:
                return None
            lon, lat, hgt = _ECEF_TO_LLH.transform(*ecef)
            return {"lat": lat, "lon": lon, "hgt": hgt}

        try:
            with obs.open("r", errors="ignore") as handle:
                for line in handle:
                    if "APPROX POSITION XYZ" in line:
                        x, y, z = (float(v) for v in line.split()[:3])
                        lon, lat, hgt = _ECEF_TO_LLH.transform(x, y, z)
                        return {"lat": lat, "lon": lon, "hgt": hgt}
                    if "END OF HEADER" in line:
                        break
        except (OSError, ValueError):
            return None
        return None

    # -- resolved base details --------------------------------------------

    def base_details(self) -> dict | None:
        """Everything worth showing about the resolved base, plus the report."""
        if self.base_position is None:
            return None

        base = self.base_position
        report = None
        detail = str(base.get("source_detail") or "")
        candidate = self.sum_path or (Path(detail) if detail.endswith(".sum") else None)
        if candidate is not None:
            pdf = Path(candidate).with_suffix(".pdf")
            if pdf.exists():
                report = str(pdf)

        summary = _base_summary(base)
        summary.update({
            "X": base.get("X"), "Y": base.get("Y"), "Z": base.get("Z"),
            "report": report,
        })
        return summary

    def reveal(self, path: str) -> bool:
        """
        Open the folder holding `path`, with the file selected.

        Selecting it matters when the output sits in a directory that also
        holds the RINEX and the PPK solutions - "somewhere in here" is not
        much of an answer after a twenty-minute run.
        """
        target = Path(path)
        try:
            if target.exists():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(str(target.parent))  # noqa: S606
            return True
        except OSError as exc:
            logging.getLogger("dji_geotagger").warning(
                "[WARN] Could not open %s: %s", path, exc)
            return False

    def open_url(self, url: str) -> bool:
        """
        Send a link to the default browser.

        Only http and https: this is called from the page, and letting it hand
        arbitrary strings to the shell would turn a link into a way to run
        things.
        """
        if not str(url).startswith(("http://", "https://")):
            logging.getLogger("dji_geotagger").warning(
                "Refused to open a non-web link: %s", url)
            return False
        webbrowser.open(url)
        return True

    def open_path(self, path: str) -> bool:
        """Hand a file to whatever the desktop uses to open it."""
        try:
            os.startfile(path)  # noqa: S606 - Windows shell association
            return True
        except OSError as exc:
            logging.getLogger("dji_geotagger").warning(
                "[WARN] Could not open %s: %s", path, exc)
            return False

    # -- coordinate systems -----------------------------------------------

    def list_frames(self) -> list[dict]:
        """
        Frame tokens a manually entered position may declare.

        Taken from the transform module's own table rather than retyped here,
        so the offered list cannot drift from the accepted one. `NAD83` is
        marked: it names no realization, and the module resolves it to
        NAD83(CSRS)v8 only because that is what CSRS-PPP means by it.
        """
        return [
            {"token": token,
             "epsg": code,
             "name": CRS.from_epsg(code).name,
             "ambiguous": token in _AMBIGUOUS_TOKENS}
            for token, code in FRAME_EPSG.items()
        ]

    def list_crs(self, near_base: bool = True) -> dict:
        """
        Coordinate systems to offer, grouped the way a picker shows them.

        Restricted by default to systems whose area of use covers the resolved
        base station. The EPSG registry holds some seven thousand entries and
        all but a handful of them are, for any given survey, noise - the ones
        that matter are the few that cover the site.
        """
        area = None
        if near_base and self.base_position is not None:
            lat = self.base_position["lat_dd"]
            lon = self.base_position["lon_dd"]
            area = AreaOfInterest(west_lon_degree=lon - 0.5,
                                  south_lat_degree=lat - 0.5,
                                  east_lon_degree=lon + 0.5,
                                  north_lat_degree=lat + 0.5)

        groups = {
            "Projected": PJType.PROJECTED_CRS,
            "Geographic": PJType.GEOGRAPHIC_2D_CRS,
            "Geocentric": PJType.GEOCENTRIC_CRS,
        }

        out = {}
        for title, pj_type in groups.items():  # noqa: PLR1702
            entries = []
            for info in query_crs_info(auth_name="EPSG", pj_types=pj_type,
                                       area_of_interest=area,
                                       allow_deprecated=False):
                entries.append({"code": f"{info.auth_name}:{info.code}",
                                "name": info.name})
            entries.sort(key=lambda e: e["name"])
            out[title] = entries

        out["User-Defined"] = self.load_settings().get("user_crs", [])
        return {"groups": out, "filtered": area is not None}

    def validate_many(self, targets: list[str]) -> dict:
        """
        Which of these can actually be used, checked the same way as one.

        Most entries in a regional list are refused - unversioned datums and
        ensembles outnumber the usable ones - so leaving the user to discover
        that by clicking each in turn is the wrong way round. About 60 ms per
        entry, so a group of fifty is a few seconds in the background.
        """
        return {code: bool(self.validate_crs(code).get("ok"))
                for code in targets}

    def validate_crs(self, target: str) -> dict:
        """
        Decide whether a target is usable, by actually transforming into it.

        Rather than reimplementing the checks, this runs the real
        `transform_coordinates` on a single row holding the resolved base
        position. Whatever it would refuse during a run, it refuses here -
        there is no second opinion to drift out of step, and a pass reports
        the operation and accuracy PROJ actually chose.
        """
        if self.base_position is None:
            return {"ok": False,
                    "reason": "Resolve the base station first: whether a "
                              "transformation is rigorous depends on the "
                              "frame the data is in."}

        base = self.base_position
        row = pd.DataFrame({
            "cam_X": [base["X"]], "cam_Y": [base["Y"]], "cam_Z": [base["Z"]],
            "coord_sys": [base["coord_sys"]],
            "epoch": [base.get("epoch")],
            "epoch_decimal_year": [base.get("epoch_decimal_year")],
            "sigma_E": [0.01], "sigma_N": [0.01], "sigma_U": [0.02],
        })

        try:
            # Quiet for two reasons: the deadlock above, and because checking
            # a highlighted entry is not an event worth a log line - a user
            # scrolling the list would fill the Log with transformations they
            # never asked for.
            with _Quiet():
                crs = self._resolve_target(target)
                out = transform_coordinates(row, crs)
        except Exception as exc:  # noqa: BLE001 - the verdict is the product
            return {"ok": False, "reason": str(exc)}
        finally:
            _no_bridge.on = False

        # Everything crossing the bridge is coerced to a plain type. numpy
        # scalars arrive here from the transform metadata and the serialiser
        # does not know them - the call then never returns, which looks like a
        # dead control rather than an error.
        meta = out.attrs.get("transform", {})

        def plain(value):
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return str(value)

        return {
            "ok": True,
            "name": str(CRS.from_user_input(crs).name),
            "operation": str(meta.get("operation") or ""),
            "accuracy": plain(meta.get("accuracy_m")),
            "shift": plain(meta.get("shift_3d_mean_m")),
            "epoch": plain(meta.get("epoch_decimal_year")),
            "sigma_transformed": bool(meta.get("sigma_transformed")),
        }

    def _resolve_target(self, target):
        """A stored user definition, or anything pyproj accepts."""
        for entry in self.load_settings().get("user_crs", []):
            if entry.get("code") == target:
                return CRS.from_wkt(entry["wkt"])
        return target

    def define_crs(self, spec: dict) -> dict:
        """
        Build and store a CRS that has no EPSG code of its own.

        Two exist in practice: a UTM zone on ITRF2020, and a provincial grid
        whose only published definition names an unversioned datum. Both are
        constructed, so both have to be remembered rather than looked up.
        """
        try:
            if spec["kind"] == "utm":
                crs = make_utm_crs(int(spec["zone"]), spec["datum"],
                                   south=bool(spec.get("south")))
            elif spec["kind"] == "rebase":
                crs = rebase_projected_crs(spec["projected"], spec["datum"])
            else:
                return {"ok": False, "reason": f"Unknown kind {spec['kind']!r}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

        crs = CRS.from_user_input(crs)
        entry = {"code": "USER:" + spec["name"], "name": spec["name"],
                 "wkt": crs.to_wkt()}

        stored = [e for e in self.load_settings().get("user_crs", [])
                  if e["code"] != entry["code"]]
        stored.append(entry)
        self.save_settings({"user_crs": stored})
        return {"ok": True, "entry": entry}

    def forget_crs(self, code: str) -> bool:
        stored = [e for e in self.load_settings().get("user_crs", [])
                  if e["code"] != code]
        return self.save_settings({"user_crs": stored})

    # -- results ----------------------------------------------------------

    def _sigma_columns(self) -> list[str]:
        """The E/N/U uncertainty columns, whatever suffix export gave them."""
        df = self.result
        return [next((c for c in df.columns if c.startswith(f"sigma_{axis}")), None)
                for axis in ("E", "N", "U")]

    def camera_points(self) -> dict:
        """
        Every camera centre, with the horizontal uncertainty that qualifies it.

        Positions alone say the flight was processed; positions coloured by
        their own uncertainty say whether the result is usable, and where it
        stopped being usable. That second question is the one a survey is
        actually asking.

        Thinned only when the count would make the map unusable, and the log
        says so rather than quietly dropping half a survey.
        """
        if self.result is None:
            return {"points": [], "total": 0, "shown": 0}

        df = self.result
        east, north, _ = self._sigma_columns()
        have_sigma = east is not None and north is not None

        keep = df.dropna(subset=["cam_lat", "cam_lon"])
        total = int(len(keep))

        step = max(1, total // _MAX_MAP_POINTS)
        if step > 1:
            logging.getLogger("dji_geotagger").info(
                "[INFO] Map shows every %d%s camera centre of %d.",
                step, "th" if step > 3 else "nd", total)
            keep = keep.iloc[::step]

        horizontal = (
            (keep[east].astype(float) ** 2 + keep[north].astype(float) ** 2) ** 0.5
            if have_sigma else None
        )

        def cell(row, column, digits=4):
            if column is None or column not in row or pd.isna(row[column]):
                return None
            return round(float(row[column]), digits)

        points = []
        for i, (_, row) in enumerate(keep.iterrows()):
            points.append({
                "lat": round(float(row["cam_lat"]), 7),
                "lon": round(float(row["cam_lon"]), 7),
                "h": (None if horizontal is None
                      or pd.isna(horizontal.iloc[i])
                      else round(float(horizontal.iloc[i]), 4)),
                "sE": cell(row, east), "sN": cell(row, north),
                "sU": cell(row, self._sigma_columns()[2]),
                "hgt": cell(row, "cam_h", 3),
                "name": str(row.get("FileName", "")),
                "path": self._image_path(row),
                "flight": str(row.get("flight", "")),
                "time": str(row.get("UTCAtExposure", "")),
                "status": str(row.get("rtk_status", "")),
            })

        return {"points": points, "total": total, "shown": len(points),
                "sigma_label": (east or "").replace("sigma_E", "") or ""}

    def _image_path(self, row) -> str | None:
        """Where the photo behind a result row lives, if it can be traced."""
        folder = self.flight_dirs.get(str(row.get("flight", "")))
        name = str(row.get("FileName", ""))
        if folder is None or not name:
            return None
        return str(folder / name)

    def thumbnail(self, path: str, size: int = 260) -> str | None:
        """
        A small JPEG of one photo, as a data URI.

        Made on demand rather than up front: a survey is thousands of 20 MB
        images and only the one being looked at is wanted. Pillow's `draft`
        does the downscale during decoding, so a preview costs a fraction of
        reading the full frame.
        """
        try:
            with Image.open(path) as image:
                image.draft("RGB", (size * 2, size * 2))
                image = image.convert("RGB")
                image.thumbnail((size, size))
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=72)
        except Exception as exc:  # noqa: BLE001 - a preview, never fatal
            logging.getLogger("dji_geotagger").debug(
                "No thumbnail for %s: %s", path, exc)
            return None

        return ("data:image/jpeg;base64,"
                + base64.b64encode(buffer.getvalue()).decode("ascii"))

    def run_statistics(self) -> dict | None:
        """
        What the run actually produced, in the terms it will be judged on.

        Percentiles rather than a mean: a survey is accepted or rejected on
        its worst usable fraction, and a mean hides a bad segment inside a
        good average.
        """
        if self.result is None:
            return None

        df = self.result
        east, north, up = self._sigma_columns()

        def spread(column):
            if column is None or column not in df:
                return None
            values = df[column].astype(float).dropna()
            if values.empty:
                return None
            return {"median": round(float(values.median()), 4),
                    "p95": round(float(values.quantile(0.95)), 4),
                    "max": round(float(values.max()), 4)}

        status = df["rtk_status"].value_counts().to_dict() if "rtk_status" in df else {}
        missing = int(df["cam_lat"].isna().sum()) if "cam_lat" in df else 0

        flights = {}
        if "flight" in df:
            flights = {str(k): int(v) for k, v in
                       df["flight"].value_counts().sort_index().items()}

        return {
            "rows": int(len(df)),
            "missing": missing,
            "flights": flights,
            "status": {str(k): int(v) for k, v in status.items()},
            "sigma": {"E": spread(east), "N": spread(north), "U": spread(up)},
            "sigma_label": east or "sigma_E",
            "output": str(self.output_path) if self.output_path else None,
        }

    # -- map data ---------------------------------------------------------

    def flight_tracks(self, paths: list[str]) -> list[dict]:
        """
        Return the recorded exposure positions for each flight, for the map.

        These come straight from the MRK - the drone's own RTK fixes - so they
        are available the moment a folder is added, with no processing at all.
        That is the point: the earliest possible check that the right flights
        were picked happens before anything expensive starts.

        Tracks are thinned to `_TRACK_MAX_POINTS`. A survey can be tens of
        thousands of exposures and the shape of the flight lines survives
        decimation intact; the endpoints are always kept.
        """
        tracks = []
        for path in paths:
            folder = Path(path)
            mrk = next((p for p in folder.glob("*.MRK")), None) \
                or next((p for p in folder.glob("*.mrk")), None)
            if mrk is None:
                continue
            try:
                df = mrk2df(str(mrk))
            except Exception as exc:  # noqa: BLE001 - one bad file, not a stop
                logging.getLogger("dji_geotagger").warning(
                    "[WARN] Could not read %s: %s", mrk.name, exc)
                continue

            step = max(1, len(df) // _TRACK_MAX_POINTS)
            thinned = df.iloc[::step]
            if len(df) and thinned.index[-1] != df.index[-1]:
                thinned = pd.concat([thinned, df.iloc[[-1]]])

            fixed = int((df["rtk_status"] == "Fixed").sum())
            tracks.append({
                "path": str(folder),
                "points": [[round(float(a), 7), round(float(o), 7)]
                           for a, o in zip(thinned["lat"], thinned["lon"])],
                "exposures": int(len(df)),
                "fixed": fixed,
            })
        return tracks

    def check_coverage(self, paths: list[str]) -> dict:
        """
        Report which exposures fall outside the base station's observations.

        PPK can only correct an exposure the base was observing at the time.
        The pipeline already refuses or NaNs those cases, but it does so
        during the solve - after the ephemerides are downloaded and minutes of
        RTKLIB have run. Everything needed to know beforehand is on disk, so
        the check is done here instead, when the folders are added.

        Both windows are checked, not just the base. An exposure needs the
        base observing *and* the aircraft observing: the first makes the
        differential correction possible, the second makes there be a
        trajectory to interpolate onto. They fail separately - a shutter fired
        eighteen seconds before the rover began logging passes the base check
        and still comes out NaN, which is exactly what the pipeline's "outside
        PPK trajectory range" warning was reporting after the fact.

        Comparison happens in GPS time rather than UTC: the MRK carries GPS
        week and time-of-week natively, so converting the windows with the
        existing `utc2gps` keeps leap seconds in one tested place instead of
        introducing a second, reverse conversion.
        """
        if self.base_obs is None:
            return {"available": False,
                    "reason": "The base observations have not been converted "
                              "yet - resolve the base station first."}

        try:
            start, end = parse_obs_time_range(Path(self.base_obs))
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

        window = [_gps_seconds(start), _gps_seconds(end)]

        flights = []
        for path in paths:
            folder = Path(path)
            mrk = next((p for p in folder.glob("*.MRK")), None) \
                or next((p for p in folder.glob("*.mrk")), None)
            if mrk is None:
                continue
            try:
                df = mrk2df(str(mrk))
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("dji_geotagger").warning(
                    "[WARN] Could not read %s: %s", mrk.name, exc)
                continue

            stamps = df["GPS_week"].astype(float) * 604800.0 \
                + df["GPS_time"].astype(float)
            outside_base = (stamps < window[0]) | (stamps > window[1])

            # The aircraft's own observations, straight from the flight folder
            # - no processing needed, so this is knowable now rather than
            # after RTKLIB has run.
            rover = next(iter(folder.glob("*_PPKOBS.obs")), None)
            outside_rover = outside_base & False
            rover_window = None
            if rover is not None:
                try:
                    r0, r1 = parse_obs_time_range(rover)
                    rover_window = [_gps_seconds(r0), _gps_seconds(r1)]
                    outside_rover = ((stamps < rover_window[0])
                                     | (stamps > rover_window[1]))
                except Exception as exc:  # noqa: BLE001
                    logging.getLogger("dji_geotagger").warning(
                        "Could not read %s: %s", rover.name, exc)

            outside = outside_base | outside_rover

            # The MRK's own sequence numbers index the exposures; only trust
            # them against filenames when the counts agree exactly.
            names = None
            photos = sorted(p.name for p in folder.iterdir()
                            if p.suffix.lower() in _PHOTO_SUFFIXES)
            if len(photos) == len(df):
                names = [photos[i] for i in
                         df.index[outside].map(df.index.get_loc)]

            flights.append({
                "path": str(folder),
                "exposures": int(len(df)),
                "outside": int(outside.sum()),
                "outside_base": int(outside_base.sum()),
                "outside_rover": int(outside_rover.sum()),
                "has_rover": rover_window is not None,
                "names": names[:_MAX_LISTED_NAMES] if names else None,
                "truncated": bool(names and len(names) > _MAX_LISTED_NAMES),
            })

        return {
            "available": True,
            "base_start": start.isoformat(timespec="seconds"),
            "base_end": end.isoformat(timespec="seconds"),
            "flights": flights,
        }

    # -- base position ----------------------------------------------------

    def resolve_base(self, cfg: dict) -> bool:
        """
        Start resolving the base station and return immediately.

        The work runs on a thread because it is minutes long - a CSRS-PPP job
        queues at NRCan - and doing it inline would freeze the window for the
        duration. Results arrive back as ``base`` / ``failed`` events.
        """
        if self._busy():
            return False
        self._cancel.clear()
        self._worker = threading.Thread(
            target=self._resolve_worker, args=(cfg,), daemon=True)
        self._worker.start()
        return True

    def _prepare_rinex(self, cfg: dict) -> Path:
        """
        Return an observation file in RINEX, converting the raw log if needed.

        The antenna height is applied here and only here. A RINEX header
        already carries it, which is why the field is hidden for that input -
        applying it twice would sink the base by a whole antenna.
        """
        chosen = Path(cfg["baseFile"]["path"])
        if cfg["baseFile"]["kind"] != "raw":
            self.base_obs = chosen
            self.base_nav = _find_nav(chosen)
            if self.base_nav is None:
                logging.getLogger("dji_geotagger").warning(
                    "[WARN] No navigation file found next to %s. PPP can "
                    "proceed; PPK will need one.", chosen.name)
            return chosen

        # No progress report: conversion is seconds, and a bar that appears
        # and vanishes says less than the log line convbin already writes.
        obs, nav = raw2rinex(
            str(chosen),
            antenna_height_in_meter=float(cfg.get("antennaHeight") or 0.0),
        )
        self.base_obs, self.base_nav = Path(obs), Path(nav)
        return self.base_obs

    def _cached_sum(self, obs: Path, ppp_kwargs: dict) -> tuple[Path | None, str]:
        """
        Find a previous CSRS-PPP result that answers this exact request.

        A submission costs minutes of somebody else's queue, so repeating one
        that has already been answered is worth avoiding. But reuse is only
        safe if the stored answer is the answer to *this* question: the .sum
        records the frame it solved in, whether it was static, and whether an
        epoch was propagated, and all three are checked. A NAD83 result cannot
        stand in for an ITRF request just because the file is there.

        Returns the path and an empty string, or None and the reason.
        """
        candidates = [obs.parent / "PPP" / (obs.stem + ".sum"),
                      obs.with_suffix(".sum")]

        for path in candidates:
            if not path.exists():
                continue
            try:
                with _Quiet():
                    parsed = sum_file_parser(sum_file_path=str(path))
            except Exception as exc:  # noqa: BLE001
                return None, f"{path.name} could not be read ({exc})"

            wanted_frame = str(ppp_kwargs.get("sysref", "ITRF")).upper()
            found_frame = str(parsed.get("coord_sys", "")).upper()
            if found_frame not in _FRAME_ALIASES.get(wanted_frame, {wanted_frame}):
                return None, (f"the stored result is {parsed.get('coord_sys')}, "
                              f"not {wanted_frame}")

            wanted_mode = str(ppp_kwargs.get("process_type", "Static")).upper()
            found_mode = str(parsed.get("mode", "")).upper()
            if found_mode != wanted_mode:
                return None, (f"the stored result is {parsed.get('mode')}, "
                              f"not {wanted_mode}")

            epoch = ppp_kwargs.get("nad83_epoch")
            if (epoch and epoch != "NAD83_CURR"
                    and not parsed.get("epoch_propagated")):
                return None, "the stored result was not propagated to a fixed epoch"

            return path, ""

        return None, "no previous result for this file"

    def _resolve_worker(self, cfg: dict) -> None:
        logger = logging.getLogger("dji_geotagger")
        mode = cfg.get("mode", "online")
        # Echoed back on the result so a superseded run can be recognised: a
        # CSRS-PPP submission takes minutes, and the base it was for may have
        # been replaced by the time it answers.
        token = cfg.get("token")
        try:
            if mode == "online":
                obs = self._prepare_rinex(cfg)
                ppp_kwargs = {
                    "process_type": cfg.get("processType", "Static"),
                    "sysref": cfg.get("frame", "ITRF"),
                }
                # Only NAD83 accepts a delivery epoch, and only a value other
                # than NAD83_CURR actually propagates one.
                epoch = cfg.get("epoch")
                if ppp_kwargs["sysref"] == "NAD83" and epoch:
                    ppp_kwargs["nad83_epoch"] = epoch

                cached, why = ((None, "a re-submission was requested")
                               if cfg.get("force")
                               else self._cached_sum(obs, ppp_kwargs))

                if cached is not None:
                    logger.info("[INFO] Reusing the CSRS-PPP result already "
                                "on disk: %s", cached.name)
                    self.sum_path = cached
                    base = resolve_base_position(
                        mode="sum", sum_file_path=str(cached))
                else:
                    logger.info("[INFO] Submitting to CSRS-PPP - %s.", why)
                    base = resolve_base_position(
                        mode="online",
                        base_obs=str(obs),
                        email=cfg.get("email", "").strip(),
                        ppp_kwargs=ppp_kwargs,
                    )
                    self.sum_path = self._cached_sum(obs, ppp_kwargs)[0]

            elif mode == "sum":
                self.sum_path = Path(cfg["sumFile"])
                base = resolve_base_position(
                    mode="sum", sum_file_path=cfg["sumFile"])

            else:
                man = cfg["manual"]
                # The form collects sigma at the selected confidence level;
                # the library wants 1-sigma.
                k = float(cfg.get("k") or 1.0)
                horizontal = float(man["sh"]) / k
                vertical = float(man["sv"]) / k
                base = resolve_base_position(
                    mode="manual",
                    manual_kwargs=dict(
                        lat_dd=float(man["lat"]),
                        lon_dd=float(man["lon"]),
                        hgt=float(man["hgt"]),
                        coord_sys=man["frame"],
                        epoch=man["epoch"],
                        sigma_ENU=(horizontal, horizontal, vertical),
                    ),
                )

            self.base_position = base
            self._emit("base", {"base": _base_summary(base), "token": token})

        except Exception as exc:  # noqa: BLE001 - reported, not raised
            logging.getLogger("dji_geotagger").debug(traceback.format_exc())
            self._emit("failed", {
                "stage": "Base position",
                "message": f"{type(exc).__name__}: {exc}",
                "token": token,
            })

    # -- the run ----------------------------------------------------------

    def run(self, cfg: dict) -> bool:
        """Start the geotagging run and return immediately."""
        if self._busy():
            return False
        self._cancel.clear()
        self._worker = threading.Thread(
            target=self._run_worker, args=(cfg,), daemon=True)
        self._worker.start()
        return True

    def reexport(self, cfg: dict) -> bool:
        """
        Write the finished run out again under different delivery settings.

        A different target CRS, confidence level or filename changes only the
        last step. Repeating the solve for it would be twenty minutes of
        RTKLIB to answer a question already answered - the camera positions
        are the same coordinates either way, described differently.
        """
        if self.result_raw is None or self._busy():
            return False
        self._worker = threading.Thread(
            target=self._reexport_worker, args=(cfg,), daemon=True)
        self._worker.start()
        return True

    def _reexport_worker(self, cfg: dict) -> None:
        logger = logging.getLogger("dji_geotagger")
        try:
            df = self.result_raw.copy()

            target = cfg.get("targetCrs")
            if target:
                df = transform_coordinates(df, self._resolve_target(target))

            df = _scale_sigma(df, float(cfg.get("k") or 1.0),
                              cfg.get("confidenceLabel", ""))

            out = Path(cfg["outFile"])
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out, index=True)
            logger.info("Re-exported %d rows to %s", len(df), out)

            self.result = df
            self.output_path = out
            self._emit("done", {"rows": int(len(df)), "path": str(out)})

        except Exception as exc:  # noqa: BLE001 - reported, not raised
            logger.debug(traceback.format_exc())
            self._emit("failed", {"stage": "Re-export",
                                  "message": f"{type(exc).__name__}: {exc}"})

    def _progress(self) -> Progress:
        """A Progress that reports to the window and honours Cancel."""
        def report(event):
            self._emit("progress", {
                "stage": event.stage,
                "message": event.message,
                "current": event.current,
                "total": event.total,
                "key": event.key,
            })

        return Progress(on_progress=report, should_cancel=self._cancel.is_set)

    def _run_worker(self, cfg: dict) -> None:
        logger = logging.getLogger("dji_geotagger")
        try:
            if self.base_position is None:
                raise RuntimeError(
                    "Resolve the base station first - every camera position "
                    "is computed relative to it.")

            # Only needed when the base came from a .sum or was typed in: the
            # online path has already converted whatever it submitted.
            if self.base_obs is None:
                self._prepare_rinex(cfg)
            if self.base_nav is None:
                raise RuntimeError(
                    "No navigation file for the base station. PPK needs "
                    "broadcast ephemerides alongside the observations.")

            # geotag() labels each row with the flight folder's stem; keeping
            # the folders lets a row be traced back to the actual image.
            self.flight_dirs = {Path(f).stem: Path(f) for f in cfg["flights"]}

            progress = self._progress()
            df = geotag(
                cfg["flights"],
                str(self.base_obs),
                str(self.base_nav),
                base_position=self.base_position,
                progress=progress,
                max_workers=int(cfg.get("workers") or 1),
            )

            # Kept before projection: changing the delivery CRS afterwards is
            # a second of work from here, against twenty minutes of RTKLIB if
            # the whole run had to be repeated.
            self.result_raw = df.copy()

            target = cfg.get("targetCrs")
            if target:
                # A user definition has no EPSG code, so the stored WKT has to
                # be recovered here - passing "USER:..." straight to pyproj
                # would fail after the whole solve had already run.
                progress.update("transform", f"Projecting to {target}")
                df = transform_coordinates(df, self._resolve_target(target),
                                           progress=progress)

            df = _scale_sigma(df, float(cfg.get("k") or 1.0),
                              cfg.get("confidenceLabel", ""))

            out = Path(cfg["outFile"])
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out, index=True)
            logger.info("[INFO] Wrote %d rows to %s", len(df), out)
            self.result = df
            self.output_path = out

            self._emit("done", {"rows": int(len(df)), "path": str(out)})

        except OperationCancelled:
            self._emit("cancelled", {"message": "Stopped at the last checkpoint."})
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            logger.debug(traceback.format_exc())
            self._emit("failed", {"stage": "Run",
                                  "message": f"{type(exc).__name__}: {exc}"})


def launch(debug: bool = False) -> None:
    """
    Open the main window and block until it closes.

    Parameters
    ----------
    debug : bool
        Enable the web inspector and disable caching of the bundled assets.
    """
    # The library configures a console handler on import so that scripts keep
    # printing. Here it is worse than useless: the Log tab already shows every
    # record, and a windowed process on Windows gets a cp1252 stream that
    # raises UnicodeEncodeError on the sigma in the base position report.
    configure_logging(console=False)

    api = Api()
    window = webview.create_window(
        "dji-geotagger",
        str(_WEB_DIR / "index.html"),
        js_api=api,
        width=1280,
        height=820,
        min_size=(1040, 680),
        # pywebview disables selection by default, which also makes the log
        # impossible to copy out of. Enabled here; the stylesheet turns it
        # back off for the chrome, so only content stays selectable.
        text_select=True,
    )
    api._bind(window)
    webview.start(debug=debug)
