"""Shared logging setup for the Flask server and the Raspberry Pi display.

Applies a common format and renders log timestamps in Los Angeles time
regardless of the host's system timezone (Heroku runs UTC).
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

FORMAT = "%(asctime)s-%(module)s-%(lineno)d-%(message)s"

_TZ = ZoneInfo("America/Los_Angeles")


def _la_time(*args):
    """logging.Formatter.converter: map a record's epoch to LA wall-clock.

    Accepts *args because assigning a plain function to the class attribute
    ``Formatter.converter`` binds it as a method, so it is called as
    ``converter(self, record.created)`` — the timestamp is the last argument.
    """
    return datetime.fromtimestamp(args[-1], _TZ).timetuple()


def configure_logging(level=logging.INFO) -> None:
    """Configure root logging with FORMAT and Los Angeles timestamps."""
    logging.basicConfig(level=level, format=FORMAT)
    logging.Formatter.converter = _la_time