"""Regression test: StorageCollector.cleanup_old_data must accept and pass
through ``companion_events_days``.

engine.py's 6-hourly cleanup passes both kwargs; before this passthrough
existed the call raised TypeError, was swallowed by the caller's broad
except, and SQLite cleanup silently never ran.
"""

from unittest.mock import Mock

from repeater.data_acquisition.storage_collector import StorageCollector


def _bare_collector() -> StorageCollector:
    collector = StorageCollector.__new__(StorageCollector)
    collector.sqlite_handler = Mock()
    return collector


def test_cleanup_passes_companion_events_days_through():
    collector = _bare_collector()
    collector.cleanup_old_data(days=10, companion_events_days=20)
    collector.sqlite_handler.cleanup_old_data.assert_called_once_with(10, companion_events_days=20)


def test_cleanup_defaults_match_engine_callsite():
    # engine.py always passes both kwargs, but a bare call must stay valid
    # for any older callers that only know about ``days``.
    collector = _bare_collector()
    collector.cleanup_old_data(days=7)
    collector.sqlite_handler.cleanup_old_data.assert_called_once_with(7, companion_events_days=None)
