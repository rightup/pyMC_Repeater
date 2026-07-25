"""Shared time-window parsing/clamping for the Mobile Companion API's RF
observation surface (design doc §10.1).

All three read-only endpoints in ``repeater/web/mobile_endpoints.py``
(receptions, contact paths, transmission repeats) take a ``?window=`` query
param and must apply the same "mandatory bounded window (default 24 h, max
7 d)" rule from the design doc -- one helper, one set of tests, instead of
three copies that could drift.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

from repeater.retention import storage_retention_days

#: design doc §10.1 defaults
DEFAULT_WINDOW_SECONDS = 24 * 3600
#: a window smaller than this isn't a meaningful RF correlation window and
#: risks a client typo (``window=0``) turning into a full-table-adjacent scan
#: with no time bound in practice -- clamp up rather than reject.
MIN_WINDOW_SECONDS = 60
#: design doc §10.1 hard max -- clamp, don't error.
MAX_WINDOW_SECONDS = 7 * 24 * 3600

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_PATTERN = re.compile(r"^(\d+)\s*([smhd])?$", re.IGNORECASE)


def parse_window_seconds(value: Optional[str]) -> int:
    """Parse a ``window`` query param into clamped seconds.

    Accepts bare integers (seconds) or an integer suffixed with one of
    ``s``/``m``/``h``/``d`` (e.g. ``30m``, ``24h``, ``7d``). Missing/blank
    input returns the default. Out-of-range values are clamped into
    ``[MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS]`` rather than raising --
    only a malformed string (not an integer, no known unit suffix, or a
    negative/zero-length match) raises ``ValueError`` for the caller to
    turn into a 400.
    """
    if value is None:
        return DEFAULT_WINDOW_SECONDS
    text = str(value).strip()
    if not text:
        return DEFAULT_WINDOW_SECONDS

    match = _PATTERN.match(text.lower())
    if not match:
        raise ValueError(f"Invalid window: {value!r}")

    amount = int(match.group(1))
    unit = match.group(2) or "s"
    seconds = amount * _UNIT_SECONDS[unit]
    return max(MIN_WINDOW_SECONDS, min(seconds, MAX_WINDOW_SECONDS))


def packets_retention_days(config: Optional[Dict[str, Any]]) -> int:
    """Return the configured ``packets`` table retention window, in days.

    Reads ``storage.retention.sqlite_cleanup_days`` -- the same key
    ``engine.py`` reads before calling ``SQLiteHandler.cleanup_old_data`` --
    so the RF observation endpoints' "pruned" signal (design doc §10.6)
    always matches the retention that's actually enforced, not a
    separately-maintained constant.
    """
    return storage_retention_days({} if config is None else config)[0]


def observations_pruned(window_start: float, config: Optional[Dict[str, Any]]) -> bool:
    """True when ``window_start`` reaches back past the packets retention
    floor -- i.e. some of the requested window's raw rows have already been
    pruned by the normal retention cleanup (design doc §10.6), so a caller
    should not read "fewer observations than expected" as "quiet RF
    conditions" without this caveat.
    """
    retention_days = packets_retention_days(config)
    floor = time.time() - (retention_days * 24 * 3600)
    return window_start < floor
