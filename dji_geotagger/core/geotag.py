from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import threading

import pandas as pd
from dji_geotagger.ppk.raw_converter import raw2rinex
from dji_geotagger.ppk.ppk_solver import process_ppk, prepare_ppk_inputs
from dji_geotagger.core.mrk_parser import mrk2df
from dji_geotagger.core.xml_parser import parse_img_dir
from dji_geotagger.core.camera_pos_solver import compute_camera_position
from dji_geotagger.ppk.base_position import resolve_base_position
from dji_geotagger.tools.progress import as_progress, OperationCancelled
from dji_geotagger.tools.logging_setup import get_logger

logger = get_logger(__name__)


def geotag(
    flight_folders: list[str],
    base_obs: str,
    base_nav: str,
    sum_file_path: str = None,
    full_output: bool = False,
    base_position: dict = None,
    progress=None,
    on_flight_error: str = "skip",
    max_workers: int = 1
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

    progress : Progress, optional
        Progress reporting and cancellation, from
        :mod:`dji_geotagger.tools.progress`. A run takes minutes to hours, so
        a caller that is not a terminal needs both to display where it is and
        to be able to stop it::

            from dji_geotagger.tools.progress import Progress, OperationCancelled

            p = Progress(on_progress=lambda ev: gui.update(ev.fraction, ev.message),
                         should_cancel=lambda: gui.cancel_pressed)
            try:
                df = dgt.geotag(flights, base_obs, base_nav, progress=p)
            except OperationCancelled:
                gui.show("Cancelled.")

        Cancellation is cooperative and is honoured inside the long external
        steps too - the RTKLIB solve and the CSRS-PPP poll are supervised
        rather than simply awaited.

    max_workers : int, default 1
        How many flights to solve at once. Threads are enough - the CPU time
        goes to the `rnx2rtkp` child process, so the GIL is not involved.

        Defaults to 1 so that existing scripts keep their exact ordering and
        log. Beyond about four the bottleneck moves from CPU to disk anyway,
        since every worker reads the same base observation file, and leaving a
        core free keeps the machine usable: `min(4, os.cpu_count() - 1)` is a
        reasonable choice for a caller that wants one.

        Progress reports from concurrent flights carry `ProgressEvent.key`, so
        a display can keep them apart.
    on_flight_error : {"skip", "raise"}, default "skip"
        What to do when one flight fails.

        ``skip``
            Log the failure, carry on with the remaining flights, and record
            what was lost in ``df.attrs["failed_flights"]``. Raises only if
            *every* flight failed.
        ``raise``
            Abort the whole run on the first failure.

        The default is ``skip`` because a run covers many flights and takes
        minutes each: losing an hour of completed work because flight 7 has no
        .MRK file helps nobody. Cancellation is never swallowed - it is the
        caller's decision about the whole run, not a property of one flight.

    Returns
    -------
    pd.DataFrame
        Concatenated camera center table for all successful flights, with an
        extra column:

        - flight : the folder name (Path.stem) for grouping / filtering

        Two entries are set in ``df.attrs`` so an incomplete result announces
        itself without the caller having to read the log:

        - ``failed_flights`` : list of ``(flight_name, reason)``
        - ``n_flights_requested`` : how many were asked for

    Raises
    ------
    ValueError
        If `on_flight_error` is not one of the accepted values.
    RuntimeError
        If no flight was processed successfully.
    FileNotFoundError / RuntimeError
        Propagated from lower-level functions when
        ``on_flight_error="raise"`` (e.g. RTKLIB execution, missing base
        files, parsing errors).
    """
    progress = as_progress(progress)
    progress.update("start", "Resolving base station position")

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

    if on_flight_error not in ("skip", "raise"):
        raise ValueError(
            f"[ERROR] on_flight_error must be 'skip' or 'raise', "
            f"got {on_flight_error!r}")

    # The RTKLIB config and the precise ephemerides depend on the base station
    # only, so they are built once here rather than rederived identically for
    # every flight. Same argument as the base position above, and it is what
    # allows flights to be solved concurrently later: the default config path
    # is fixed, so per-flight generation would have workers overwriting a file
    # another one was reading.
    progress.update("start", "Preparing RTKLIB configuration and ephemerides")
    conf_file, ephemeris_files = prepare_ppk_inputs(
        base_obs,
        sum_file_path=sum_file_path,
        base_position=base_position,
    )

    results = []
    failures = []
    n_flights = len(flight_folders)
    done = 0
    lock = threading.Lock()

    def solve(flight_dir: Path):
        """One flight, start to finished camera table."""
        # Each worker reports under its own key so that concurrent streams stay
        # distinguishable; sequentially it is the same behaviour with a label.
        flight_progress = progress.tagged(flight_dir.name)

        rover_raws = list(flight_dir.glob("*_PPKRAW.bin"))
        if not rover_raws:
            raise FileNotFoundError(f"No *_PPKRAW.bin found in {flight_dir}")
        rover_obs, _ = raw2rinex(rover_raws[0], progress=flight_progress)

        pos_df = process_ppk(
            base_obs, base_nav,
            rover_obs=rover_obs,
            sum_file_path=sum_file_path,
            base_position=base_position,
            conf_file=conf_file,
            ephemeris_files=ephemeris_files,
            progress=flight_progress,
        )

        mrks = list(flight_dir.glob("*.MRK"))
        if not mrks:
            raise FileNotFoundError(f"No *.MRK found in {flight_dir}")

        result = compute_camera_position(
            pos_df=pos_df,
            mrk_df=mrk2df(mrks[0]),
            img_df=parse_img_dir(flight_dir, progress=flight_progress),
            full_output=full_output,
        )
        result["flight"] = flight_dir.stem
        return result

    def finish(flight_dir: Path, result, exc):
        """Record one outcome. Called from worker threads, hence the lock."""
        nonlocal done
        with lock:
            done += 1
            if exc is None:
                results.append(result)
                message = f"Finished {flight_dir.name}"
            else:
                logger.error(f"Flight {flight_dir.name} failed and was "
                             f"skipped: {type(exc).__name__}: {exc}")
                failures.append((flight_dir.stem,
                                 f"{type(exc).__name__}: {exc}"))
                message = f"Skipped {flight_dir.name}"
            progress.update("flight", message, current=done, total=n_flights)

    folders = [Path(f) for f in flight_folders]
    progress.update("flight", f"{n_flights} flight(s) to process",
                    current=0, total=n_flights)

    if max_workers <= 1:
        for flight_dir in folders:
            try:
                finish(flight_dir, solve(flight_dir), None)
            except OperationCancelled:
                # A cancel is the user's decision about the whole run, not a
                # property of this flight. Never swallowed.
                raise
            except Exception as exc:
                if on_flight_error == "raise":
                    raise
                finish(flight_dir, None, exc)
    else:
        # Threads, not processes: the CPU time is spent inside the rnx2rtkp
        # child, so the GIL is not on the path, and a thread pool keeps the
        # progress callbacks and the cancel flag shared without any IPC.
        logger.info(f"Solving {n_flights} flights with {max_workers} workers.")
        cancelled = None
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(solve, f): f for f in folders}
            for future in as_completed(futures):
                flight_dir = futures[future]
                try:
                    finish(flight_dir, future.result(), None)
                except OperationCancelled as exc:
                    cancelled = exc
                    # Stop handing out work; those already running will hit
                    # their own next checkpoint and raise too.
                    for pending in futures:
                        pending.cancel()
                except Exception as exc:
                    if on_flight_error == "raise":
                        for pending in futures:
                            pending.cancel()
                        raise
                    finish(flight_dir, None, exc)
        if cancelled is not None:
            raise cancelled

    if failures:
        logger.warning(
            f"{len(failures)} of {n_flights} flights failed and were skipped:")
        for name, why in failures:
            logger.warning(f"  {name}: {why}")

    if not results:
        detail = "\n".join(f"  {n}: {w}" for n, w in failures)
        raise RuntimeError(
            "[ERROR] No flights were successfully processed."
            + (f"\n{detail}" if detail else ""))

    final_df = pd.concat(results, ignore_index=True)
    # Failures travel with the result so a caller does not have to scrape the
    # log to find out the output is incomplete.
    final_df.attrs["failed_flights"] = failures
    final_df.attrs["n_flights_requested"] = n_flights
    return final_df