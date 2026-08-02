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
import sys
import threading
import time
from dataclasses import dataclass


class OperationCancelled(Exception):
    """Raised at a checkpoint when the caller has asked to stop."""


def _hide_console(kwargs: dict) -> None:
    """
    Stop a console child from opening a window of its own, on Windows.

    RTKLIB's tools are console programs. Started from a windowed process -
    which is what a packaged GUI is - each one flashes up a command prompt,
    and a nineteen-flight run flashes nineteen of them.

    Output is already piped or discarded at every call site, so there is
    nothing to see in that window in any case. Left alone if the caller has
    asked for particular flags.
    """
    if sys.platform != "win32" or "creationflags" in kwargs:
        return
    # 0x08000000, defined only on Windows builds of the standard library.
    kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


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
    key : str | None
        Which concurrent unit of work this refers to, when several run at
        once - a flight name while a pool of solvers is going. Without it a
        display has no way to tell four interleaved streams apart, and their
        percentages appear to jump backwards. None for sequential work.
    """

    stage: str
    message: str = ""
    current: int | None = None
    total: int | None = None
    key: str | None = None

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

    def __init__(self, on_progress=None, should_cancel=None, key=None):
        self._on_progress = on_progress
        self._should_cancel = should_cancel
        self._key = key

    def tagged(self, key: str) -> "Progress":
        """
        A view of this Progress whose reports are labelled `key`.

        Handed to each worker when flights are solved concurrently, so their
        interleaved reports stay distinguishable. Cancellation is shared -
        it is the same flag, so stopping stops everything.
        """
        return Progress(on_progress=self._on_progress,
                        should_cancel=self._should_cancel,
                        key=key)

    def update(self, stage: str, message: str = "",
               current: int = None, total: int = None,
               key: str = None) -> None:
        """Report progress and check for cancellation."""
        if self._on_progress is not None:
            try:
                self._on_progress(
                    ProgressEvent(stage=stage, message=message,
                                  current=current, total=total,
                                  key=key if key is not None else self._key))
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
                       on_line=None, **kwargs) -> int:
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
        on_line : callable, optional
            Called with each line the child writes to stderr, stripped. Used
            to turn a tool's own chatter into progress - ``rnx2rtkp`` reports
            the epoch it is working on there. The reader runs on its own
            thread because the child fills the pipe faster than the poll loop
            wakes, and a full pipe would deadlock the child. Exceptions raised
            here are suppressed: a faulty display must not kill a solve.
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
        _hide_console(kwargs)

        if not self.is_active and on_line is None:
            # Nothing to cancel and nothing to report: keep the simple path.
            return subprocess.run(cmd, **kwargs).returncode

        if on_line is not None:
            # RTKLIB separates its progress lines with carriage returns, which
            # text mode treats as line endings, so iteration yields one epoch
            # report at a time.
            kwargs.setdefault("stderr", subprocess.PIPE)
            kwargs.setdefault("text", True)
            kwargs.setdefault("bufsize", 1)

        proc = subprocess.Popen(cmd, **kwargs)

        reader = None
        if on_line is not None and proc.stderr is not None:
            def pump():
                try:
                    for line in proc.stderr:
                        try:
                            on_line(line.strip())
                        except Exception:  # noqa: BLE001
                            pass
                except (ValueError, OSError):
                    pass  # pipe closed under us during termination

            reader = threading.Thread(target=pump, daemon=True)
            reader.start()

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
            if reader is not None:
                reader.join(timeout=2)
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
