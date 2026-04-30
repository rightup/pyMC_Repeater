import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("psutil", types.ModuleType("psutil"))

nacl_module = types.ModuleType("nacl")
nacl_signing_module = types.ModuleType("nacl.signing")


class _SigningKeyStub:
    pass


nacl_signing_module.SigningKey = _SigningKeyStub
nacl_module.signing = nacl_signing_module

sys.modules.setdefault("nacl", nacl_module)
sys.modules.setdefault("nacl.signing", nacl_signing_module)

from repeater.data_acquisition.storage_collector import StorageCollector


def _make_collector() -> StorageCollector:
    with (
        patch("repeater.data_acquisition.storage_collector.SQLiteHandler"),
        patch("repeater.data_acquisition.storage_collector.RRDToolHandler"),
        patch("repeater.data_acquisition.hardware_stats.HardwareStatsCollector"),
    ):
        collector = StorageCollector(
            config={"storage": {"storage_dir": "/tmp/pymc_repeater_test"}}
        )

    collector.sqlite_handler = MagicMock()
    collector.sqlite_handler.sqlite_path = Path(":memory:")
    collector.repeater_handler = SimpleNamespace(start_time=100.0)
    return collector


# ===========================================================================
# get_node_name_by_pubkey
# ===========================================================================

class TestGetNodeNameByPubkey:

    # -----------------------------------------------------------------------
    # Alias takes priority over advert
    # -----------------------------------------------------------------------

    def test_alias_returned_when_present(self):
        collector = _make_collector()
        collector.sqlite_handler.get_pubkey_alias.return_value = "Alice"

        name = collector.get_node_name_by_pubkey("aabbccddeeff0011")

        assert name == "Alice"

    def test_alias_takes_priority_over_advert(self):
        collector = _make_collector()
        collector.sqlite_handler.get_pubkey_alias.return_value = "Alias Name"

        # Even if an advert row exists it must not be reached
        with patch("sqlite3.connect") as mock_connect:
            name = collector.get_node_name_by_pubkey("aabbccddeeff0011")

        assert name == "Alias Name"
        mock_connect.assert_not_called()

    # -----------------------------------------------------------------------
    # Falls back to advert when no alias
    # -----------------------------------------------------------------------

    def test_falls_back_to_advert_when_no_alias(self):
        collector = _make_collector()
        collector.sqlite_handler.get_pubkey_alias.return_value = None

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = ("NodeAlpha",)

        with patch("sqlite3.connect", return_value=mock_conn):
            name = collector.get_node_name_by_pubkey("aabbccddeeff0011")

        assert name == "NodeAlpha"

    def test_advert_query_uses_correct_pubkey(self):
        collector = _make_collector()
        collector.sqlite_handler.get_pubkey_alias.return_value = None

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = ("NodeBeta",)

        pubkey = "deadbeef12345678"
        with patch("sqlite3.connect", return_value=mock_conn):
            collector.get_node_name_by_pubkey(pubkey)

        call_args = mock_conn.execute.call_args
        assert pubkey in call_args[0][1]

    # -----------------------------------------------------------------------
    # Returns None when neither source has a name
    # -----------------------------------------------------------------------

    def test_returns_none_when_no_alias_and_no_advert(self):
        collector = _make_collector()
        collector.sqlite_handler.get_pubkey_alias.return_value = None

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None

        with patch("sqlite3.connect", return_value=mock_conn):
            name = collector.get_node_name_by_pubkey("aabbccddeeff0011")

        assert name is None

    def test_returns_none_when_advert_row_has_no_name(self):
        collector = _make_collector()
        collector.sqlite_handler.get_pubkey_alias.return_value = None

        mock_conn = MagicMock()
        mock_conn.__enter__ = lambda s: mock_conn
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchone.return_value = None

        with patch("sqlite3.connect", return_value=mock_conn):
            name = collector.get_node_name_by_pubkey("aabbccddeeff0011")

        assert name is None

    # -----------------------------------------------------------------------
    # Empty / None pubkey input
    # -----------------------------------------------------------------------

    def test_returns_none_for_none_pubkey(self):
        collector = _make_collector()
        name = collector.get_node_name_by_pubkey(None)
        assert name is None

    def test_returns_none_for_empty_string_pubkey(self):
        collector = _make_collector()
        name = collector.get_node_name_by_pubkey("")
        assert name is None

    def test_alias_handler_not_called_for_empty_pubkey(self):
        collector = _make_collector()
        collector.get_node_name_by_pubkey("")
        collector.sqlite_handler.get_pubkey_alias.assert_not_called()

    def test_alias_handler_not_called_for_none_pubkey(self):
        collector = _make_collector()
        collector.get_node_name_by_pubkey(None)
        collector.sqlite_handler.get_pubkey_alias.assert_not_called()

    # -----------------------------------------------------------------------
    # Exception resilience
    # -----------------------------------------------------------------------

    def test_returns_none_on_sqlite_exception(self):
        collector = _make_collector()
        collector.sqlite_handler.get_pubkey_alias.side_effect = Exception("db error")

        name = collector.get_node_name_by_pubkey("aabbccddeeff0011")

        assert name is None
