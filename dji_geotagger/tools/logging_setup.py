"""
Package logging.

Progress and diagnostics used to go to ``print()``, which works for a script
run in a terminal and nowhere else. A GUI, a scheduled job or a log file all
need the same information, and none of them can read stdout without scraping
it.

Everything now goes through the standard :mod:`logging` module. The default
configuration is chosen so console output is byte-for-byte what it was before:
the formatter emits ``[LEVEL] message``, and Python's level names (INFO,
WARNING, ERROR) are exactly the prefixes the code used to write by hand.

Consuming the output elsewhere
------------------------------
Attach a handler to the ``dji_geotagger`` logger::

    import logging

    class GuiHandler(logging.Handler):
        def emit(self, record):
            gui.append_line(record.levelname, record.getMessage())

    logging.getLogger("dji_geotagger").addHandler(GuiHandler())

To stop the console output as well::

    from dji_geotagger.tools.logging_setup import configure_logging
    configure_logging(console=False)
"""

from __future__ import annotations

import logging
import sys

PACKAGE_LOGGER = "dji_geotagger"

# Reproduces the previous hand-written prefixes exactly.
DEFAULT_FORMAT = "[%(levelname)s] %(message)s"

_console_handler: logging.Handler | None = None


class _EncodingSafeStreamHandler(logging.StreamHandler):
    """
    A stream handler that survives a destination which cannot spell.

    Messages here contain the odd non-ASCII character - a tick on success, a
    degree sign, a sigma. A Windows console handles them, because Python
    writes to it through the wide API. A *redirected* stdout does not: it
    takes the locale encoding, and ``python -m dji_geotagger > log.txt`` on a
    cp1252 or cp950 machine raised ``UnicodeEncodeError`` on every line
    carrying one.

    Nothing crashed - :mod:`logging` catches the error, prints
    ``--- Logging error ---`` and a traceback to stderr, and carries on - but
    the line itself was lost among a screenful of noise.

    Characters the destination cannot represent are escaped rather than
    dropped, so nothing disappears silently.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            encoding = getattr(self.stream, "encoding", None)
            if encoding:
                text = text.encode(encoding, "backslashreplace").decode(
                    encoding, "replace")
            self.stream.write(text + self.terminator)
            self.flush()
        except RecursionError:  # as logging.Handler itself does
            raise
        except Exception:  # noqa: BLE001 - a log line must not stop the run
            self.handleError(record)


def configure_logging(
    level: int | str = logging.INFO,
    console: bool = True,
    stream=None,
    fmt: str = DEFAULT_FORMAT,
) -> logging.Logger:
    """
    Set up console logging for the package.

    Called automatically on import so that scripts keep working unchanged.
    Safe to call again to change the level or turn the console off.

    Parameters
    ----------
    level : int | str, default logging.INFO
        Threshold for the package logger.
    console : bool, default True
        Whether to emit to a stream handler. Set False when another handler
        (a GUI, a file) is the only destination wanted.
    stream : file-like, optional
        Destination for the console handler. Defaults to ``sys.stdout``,
        matching where ``print()`` used to go.
    fmt : str, optional
        Format string for the console handler.

    Returns
    -------
    logging.Logger
        The package logger.
    """
    global _console_handler

    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(level)
    # Handlers are attached here, not to the root logger, so importing this
    # package never changes logging for the application that embeds it.
    logger.propagate = False

    if _console_handler is not None:
        logger.removeHandler(_console_handler)
        _console_handler = None

    if console:
        handler = _EncodingSafeStreamHandler(stream if stream is not None
                                             else sys.stdout)
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
        _console_handler = handler

    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Return the logger for a module inside this package.

    Parameters
    ----------
    name : str
        Usually ``__name__``.

    Returns
    -------
    logging.Logger
    """
    return logging.getLogger(name)
