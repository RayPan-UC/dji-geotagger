"""
Progress reporting and cancellation.

A geotagging run is minutes to hours of work: RINEX conversion, an RTKLIB
solve per flight, and possibly a CSRS-PPP submission that sits in a queue.
Two things follow from that, and neither is possible with a plain function
call that blocks until it returns.

* The caller needs to know **where it is**, not just that it is busy.
* The caller needs to be able to **stop**, without killing the process.

Both are expressed through a single optional `progress` argument threaded
through the pipeline. Passing nothing keeps the previous behaviour exactly:
:data:`NULL_PROGRESS` reports nowhere and never cancels.

Usage from a GUI::

    from dji_geotagger.tools.progress import Progress, OperationCancelled

    progress = Progress(
        on_progress=lambda ev: gui.update(ev.stage, ev.fraction, ev.message),
        should_cancel=lambda: gui.cancel_button_was_pressed,
    )
    try:
        df = dgt.geotag(flights, base_obs, base_nav, progress=progress)
    except OperationCancelled:
        gui.show("Cancelled.")

Cancellation is cooperative. It takes effect at the next checkpoint, which
inside a long external step - an RTKLIB solve, a CSRS-PPP poll - is at most
about a second away, because those steps are supervised rather than simply
awaited.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass


class OperationCancelled(Exception):
    """Raised at a checkpoint when the caller has asked to stop."""


@dataclass(frozen=True)
class ProgressEvent:
    """
    One progress report.

    Attributes
    ----------
    stage : str
        Coarse phase, e.g. ``"ppk"``, ``"ppp"``, ``"images"``.
    message : str
        Human-readable detail.
    current : int | None
        Units completed, when countable.
    total : int | None
        Units expected, when known.
    """

    stage: str
    message: str = ""
    current: int | None = None
    total: int | None = None

    @property
    def fraction(self) -> float | None:
        """Completion in ``[0, 1]``, or None when the total is unknown."""
        if self.total in (None, 0) or self.current is None:
            return None
        return min(1.0, max(0.0, self.current / self.total))


class Progress:
    """
    Progress sink and cancellation source.

    Parameters
    ----------
    on_progress : callable, optional
        Called with a :class:`ProgressEvent` at each reporting point.
        Exceptions raised here are suppressed: a faulty display must not
        abort a long-running computation.
    should_cancel : callable, optional
        Called at each checkpoint. Return True to stop the run.
    """

    def __init__(self, on_progress=None, should_cancel=None):
        self._on_progress = on_progress
        self._should_cancel = should_cancel

    def update(self, stage: str, message: str = "",
               current: int = None, total: int = None) -> None:
        """Report progress and check for cancellation."""
        if self._on_progress is not None:
            try:
                self._on_progress(
                    ProgressEvent(stage=stage, message=message,
                                  current=current, total=total))
            except Exception:  # noqa: BLE001 - display failure is not fatal
                pass
        self.check()

    def check(self) -> None:
        """
        Raise :class:`OperationCancelled` if the caller has asked to stop.

        Raises
        ------
        OperationCancelled
        """
        if self._should_cancel is not None and self._should_cancel():
            raise OperationCancelled("[INFO] Cancelled by user.")

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested, without raising."""
        return bool(self._should_cancel is not None and self._should_cancel())

    @property
    def is_active(self) -> bool:
        """Whether anything is actually listening."""
        return self._on_progress is not None or self._should_cancel is not None

    def sleep(self, seconds: float, tick: float = 1.0) -> None:
        """
        Sleep, but stay cancellable.

        A bare ``time.sleep(30)`` in a poll loop means a cancel request waits
        up to 30 seconds to be noticed. This wakes every `tick` seconds to
        check instead.

        Parameters
        ----------
        seconds : float
            Total time to wait.
        tick : float, default 1.0
            Checkpoint interval.

        Raises
        ------
        OperationCancelled
        """
        remaining = seconds
        while remaining > 0:
            self.check()
            time.sleep(min(tick, remaining))
            remaining -= tick
        self.check()

    def run_subprocess(self, cmd: list[str], tick: float = 0.5,
                       **kwargs) -> int:
        """
        Run an external command under supervision, so it can be cancelled.

        ``subprocess.run`` blocks until the child exits, which for an RTKLIB
        solve can be minutes with no way to interrupt it. This polls instead
        and terminates the child if cancellation is requested.

        Parameters
        ----------
        cmd : list[str]
            Command and arguments.
        tick : float, default 0.5
            How often to check for cancellation.
        **kwargs
            Passed to :class:`subprocess.Popen`.

        Returns
        -------
        int
            The child's exit code.

        Raises
        ------
        OperationCancelled
            If cancelled; the child is terminated first, escalating to kill
            if it does not exit promptly.
        """
        if not self.is_active:
            # Nothing to cancel and nothing to report: keep the simple path.
            return subprocess.run(cmd, **kwargs).returncode

        proc = subprocess.Popen(cmd, **kwargs)
        try:
            while True:
                try:
                    return proc.wait(timeout=tick)
                except subprocess.TimeoutExpired:
                    if self.cancelled:
                        break
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        raise OperationCancelled("[INFO] Cancelled by user.")


#: Shared do-nothing instance, used whenever a caller passes no progress.
NULL_PROGRESS = Progress()


def as_progress(progress: Progress | None) -> Progress:
    """
    Normalise an optional progress argument.

    Parameters
    ----------
    progress : Progress | None

    Returns
    -------
    Progress
        The given object, or :data:`NULL_PROGRESS`.
    """
    return progress if progress is not None else NULL_PROGRESS
