import logging
from typing import Any

TRACE_LEVEL = 5
logging.addLevelName(TRACE_LEVEL, "TRACE")


def normalize_log_level(level: Any) -> int:
    if isinstance(level, int):
        return level
    if not isinstance(level, str):
        return logging.INFO

    level_name = level.strip().upper()
    if not level_name:
        return logging.INFO
    if level_name == "TRACE":
        return TRACE_LEVEL

    resolved = getattr(logging, level_name, None)
    if isinstance(resolved, int):
        return resolved

    try:
        return int(level_name)
    except ValueError:
        return logging.INFO
