"""
Base / rover observation time coverage.

PPK is a *differential* solution: every rover epoch is resolved against a
simultaneous base observation. Epochs with no base coverage cannot be fixed at
all, and RTKLIB does not refuse the job - it produces a shorter or lower
quality trajectory, and the missing stretch only shows up later as images with
no position.

Checking the overlap first turns a puzzling result into an explicit statement,
and does so before RTKLIB spends minutes on a flight it cannot solve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dji_geotagger.ppk.ephemeris_downloader import parse_obs_time_range
from dji_geotagger.tools.logging_setup import get_logger

logger = get_logger(__name__)

# Below this, a "solution" would be too short to be worth running.
MIN_USEFUL_OVERLAP_S = 60.0


@dataclass
class TimeCoverage:
    """
    Result of comparing a rover observation window against the base.

    Attributes
    ----------
    base_start, base_end : datetime
        Base station observation window (UTC).
    rover_start, rover_end : datetime
        Rover observation window (UTC).
    overlap_start, overlap_end : datetime | None
        Intersection, or None when the windows are disjoint.
    overlap_seconds : float
        Length of the intersection.
    rover_seconds : float
        Length of the rover window.
    """

    base_start: datetime
    base_end: datetime
    rover_start: datetime
    rover_end: datetime
    overlap_start: datetime | None
    overlap_end: datetime | None
    overlap_seconds: float
    rover_seconds: float

    @property
    def covered_fraction(self) -> float:
        """Fraction of the rover window that the base covers, in ``[0, 1]``."""
        if self.rover_seconds <= 0:
            return 0.0
        return min(1.0, self.overlap_seconds / self.rover_seconds)

    @property
    def is_complete(self) -> bool:
        """Whether the base covers the whole rover window."""
        return self.covered_fraction >= 0.999

    @property
    def is_usable(self) -> bool:
        """Whether there is enough overlap to be worth solving."""
        return self.overlap_seconds >= MIN_USEFUL_OVERLAP_S

    def summary(self) -> str:
        """One-line human-readable verdict."""
        fmt = "%H:%M:%S"
        base = f"{self.base_start:{fmt}}-{self.base_end:{fmt}}"
        rover = f"{self.rover_start:{fmt}}-{self.rover_end:{fmt}}"
        if self.overlap_seconds <= 0:
            return f"base {base} vs rover {rover}: NO OVERLAP"
        return (f"base {base} vs rover {rover}: "
                f"{self.covered_fraction * 100:.1f}% covered "
                f"({self.overlap_seconds / 60:.1f} min)")


def check_time_overlap(
    base_obs: str | Path,
    rover_obs: str | Path,
    *,
    strict: bool = True,
) -> TimeCoverage:
    """
    Verify that the base station observed while the rover was flying.

    Parameters
    ----------
    base_obs : str | Path
        Base station RINEX observation file.
    rover_obs : str | Path
        Rover RINEX observation file.
    strict : bool, default True
        Raise when the overlap is unusable. Set False to report and continue,
        e.g. when the caller wants to skip that flight rather than abort.

    Returns
    -------
    TimeCoverage

    Raises
    ------
    ValueError
        If `strict` and the windows do not usefully overlap.

    Notes
    -----
    Partial coverage is reported as a warning rather than an error. A flight
    that starts a minute before the base was switched on is still worth
    solving; the uncovered epochs simply yield no position, and those images
    surface later as NaN rather than as silently wrong coordinates.
    """
    base_obs, rover_obs = Path(base_obs), Path(rover_obs)
    b0, b1 = parse_obs_time_range(base_obs)
    r0, r1 = parse_obs_time_range(rover_obs)

    o0, o1 = max(b0, r0), min(b1, r1)
    overlap = max(0.0, (o1 - o0).total_seconds())

    cov = TimeCoverage(
        base_start=b0, base_end=b1,
        rover_start=r0, rover_end=r1,
        overlap_start=o0 if overlap > 0 else None,
        overlap_end=o1 if overlap > 0 else None,
        overlap_seconds=overlap,
        rover_seconds=max(0.0, (r1 - r0).total_seconds()),
    )

    if not cov.is_usable:
        message = (
            f"[ERROR] Base and rover observations do not overlap usefully.\n"
            f"        base  : {b0:%Y-%m-%d %H:%M:%S} -> {b1:%H:%M:%S} UTC\n"
            f"        rover : {r0:%Y-%m-%d %H:%M:%S} -> {r1:%H:%M:%S} UTC "
            f"({rover_obs.name})\n"
            f"        overlap: {cov.overlap_seconds:.0f} s\n"
            "        PPK is differential: without simultaneous base data the "
            "rover cannot be resolved.\n"
            "        Check that the base was logging during this flight, and "
            "that both files are from the same day."
        )
        if strict:
            raise ValueError(message)
        logger.error(message)
        return cov

    if not cov.is_complete:
        logger.warning(
            f"Base covers only {cov.covered_fraction * 100:.1f}% of "
            f"{rover_obs.name} ({cov.overlap_seconds / 60:.1f} of "
            f"{cov.rover_seconds / 60:.1f} min).")
        logger.warning(
            "Exposures outside the covered window will have no position.")
    else:
        logger.info(f"Time coverage OK: {cov.summary()}")

    return cov
