import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy dependencies before loading api_endpoints directly via importlib.
# This avoids triggering repeater/web/__init__.py which pulls in http_server
# → cherrypy_cors, a package we don't need in tests.
# ---------------------------------------------------------------------------

def _pkg(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__path__ = []
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return sys.modules[name]

def _mod(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return sys.modules[name]

# cherrypy — tools must absorb any attribute access (json_out, gzip, …)
_tools = MagicMock()
_tools.json_out.return_value = lambda f: f
_tools.json_in.return_value = lambda f: f
_tools.gzip.return_value = lambda f: f
_cp = _pkg("cherrypy")
_cp.expose = lambda f: f
_cp.tools = _tools
_cp.request = SimpleNamespace(method="GET", json=None, params={})
_cp.response = SimpleNamespace(headers={})
_cp.HTTPError = type("HTTPError", (Exception,), {})
_pkg("cherrypy.lib")

_pkg("pymc_core")
_pkg("pymc_core.protocol", CryptoUtils=MagicMock(), PacketBuilder=MagicMock())
_mod("pymc_core.protocol.constants")

# repeater sub-packages / modules that api_endpoints imports from
_pkg("repeater.companion")
_mod("repeater.companion.identity_resolve",
     derive_companion_public_key_hex=MagicMock(),
     find_companion_index=MagicMock(),
     heal_companion_empty_names=MagicMock())
_mod("repeater.config", update_unscoped_flood_policy=MagicMock())
_mod("repeater.service_utils", get_buildroot_image_info=MagicMock())
_mod("repeater.config_manager", ConfigManager=MagicMock())
_pkg("repeater.web")
_pkg("repeater.web.auth")
_mod("repeater.web.auth.middleware", require_auth=lambda f: f)
_mod("repeater.web.auth_endpoints", AuthAPIEndpoints=MagicMock())
_mod("repeater.web.cad_calibration_engine", CADCalibrationEngine=MagicMock())
_mod("repeater.web.companion_endpoints", CompanionAPIEndpoints=MagicMock())
_mod("repeater.web.update_endpoints", UpdateAPIEndpoints=MagicMock())

import repeater as _repeater_pkg  # noqa: E402
if not hasattr(_repeater_pkg, "__version__"):
    _repeater_pkg.__version__ = "0.0.0-test"

# Load api_endpoints directly — bypasses repeater/web/__init__.py.
_ae_path = Path(__file__).parent.parent / "repeater" / "web" / "api_endpoints.py"
_spec = importlib.util.spec_from_file_location("repeater.web.api_endpoints", _ae_path)
_ae_mod = importlib.util.module_from_spec(_spec)
sys.modules["repeater.web.api_endpoints"] = _ae_mod
_spec.loader.exec_module(_ae_mod)

APIEndpoints = _ae_mod.APIEndpoints


# ---------------------------------------------------------------------------
# Factory — build a minimal APIEndpoints with mocked daemon
# ---------------------------------------------------------------------------

def _make_api(storage=None, companion_resolver=None):
    """Return an APIEndpoints instance with mocked daemon and helpers."""
    daemon = MagicMock()
    daemon.repeater_handler = MagicMock()
    daemon.repeater_handler.storage = storage or MagicMock()

    with patch("repeater.web.api_endpoints.CADCalibrationEngine", MagicMock()):
        api = APIEndpoints.__new__(APIEndpoints)

    api.daemon_instance = daemon
    api.config = {}
    api._config_path = "/tmp/test.yaml"
    api.config_manager = MagicMock()
    api.auth = MagicMock()
    api.companion = MagicMock()
    api.update = MagicMock()
    api.cad_calibration = MagicMock()

    if companion_resolver is not None:
        api._get_companion_name_by_pubkey = companion_resolver
    else:
        api._get_companion_name_by_pubkey = MagicMock(return_value=None)

    return api


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestResolvePubkeyNames:

    # -----------------------------------------------------------------------
    # Happy path — alias-resolved names
    # -----------------------------------------------------------------------

    def test_known_pubkey_returns_name(self):
        storage = MagicMock()
        storage.get_node_name_by_pubkey.side_effect = lambda pk: "Alice" if pk == "aa" * 32 else None

        api = _make_api(storage=storage)
        result = api.resolve_pubkey_names(pubkeys="aa" * 32)

        assert result["success"] is True
        assert result["data"]["aa" * 32] == "Alice"

    def test_multiple_known_pubkeys_all_resolved(self):
        pk_a = "aa" * 32
        pk_b = "bb" * 32
        name_map = {pk_a: "Alice", pk_b: "Bob"}

        storage = MagicMock()
        storage.get_node_name_by_pubkey.side_effect = lambda pk: name_map.get(pk)

        api = _make_api(storage=storage)
        result = api.resolve_pubkey_names(pubkeys=f"{pk_a},{pk_b}")

        assert result["success"] is True
        assert result["data"][pk_a] == "Alice"
        assert result["data"][pk_b] == "Bob"

    # -----------------------------------------------------------------------
    # Unknown pubkeys → None in map
    # -----------------------------------------------------------------------

    def test_unknown_pubkey_value_is_none(self):
        storage = MagicMock()
        storage.get_node_name_by_pubkey.return_value = None

        api = _make_api(storage=storage)
        api._get_companion_name_by_pubkey = MagicMock(return_value=None)

        result = api.resolve_pubkey_names(pubkeys="cc" * 32)

        assert result["success"] is True
        assert result["data"]["cc" * 32] is None

    def test_mix_of_known_and_unknown(self):
        pk_known = "dd" * 32
        pk_unknown = "ee" * 32

        storage = MagicMock()
        storage.get_node_name_by_pubkey.side_effect = (
            lambda pk: "Dave" if pk == pk_known else None
        )

        api = _make_api(storage=storage)
        api._get_companion_name_by_pubkey = MagicMock(return_value=None)

        result = api.resolve_pubkey_names(pubkeys=f"{pk_known},{pk_unknown}")

        assert result["success"] is True
        assert result["data"][pk_known] == "Dave"
        assert result["data"][pk_unknown] is None

    # -----------------------------------------------------------------------
    # Empty pubkeys param → empty dict
    # -----------------------------------------------------------------------

    def test_empty_pubkeys_param_returns_empty_dict(self):
        api = _make_api()
        result = api.resolve_pubkey_names(pubkeys="")

        assert result["success"] is True
        assert result["data"] == {}

    def test_whitespace_only_param_returns_empty_dict(self):
        api = _make_api()
        result = api.resolve_pubkey_names(pubkeys="   ,  ,  ")

        assert result["success"] is True
        assert result["data"] == {}

    # -----------------------------------------------------------------------
    # Companion-resolved names
    # -----------------------------------------------------------------------

    def test_companion_name_used_as_fallback(self):
        pk = "ff" * 32
        storage = MagicMock()
        storage.get_node_name_by_pubkey.return_value = None  # no alias / advert

        companion_resolver = MagicMock(return_value="CompanionFred")
        api = _make_api(storage=storage, companion_resolver=companion_resolver)

        result = api.resolve_pubkey_names(pubkeys=pk)

        assert result["success"] is True
        assert result["data"][pk] == "CompanionFred"

    def test_alias_takes_priority_over_companion(self):
        pk = "11" * 32
        storage = MagicMock()
        storage.get_node_name_by_pubkey.return_value = "AliasName"

        companion_resolver = MagicMock(return_value="CompanionName")
        api = _make_api(storage=storage, companion_resolver=companion_resolver)

        result = api.resolve_pubkey_names(pubkeys=pk)

        assert result["success"] is True
        assert result["data"][pk] == "AliasName"
        # Companion resolver must NOT be called if alias already resolved
        companion_resolver.assert_not_called()

    def test_mix_of_alias_and_companion_resolved(self):
        pk_alias = "22" * 32
        pk_companion = "33" * 32

        storage = MagicMock()
        storage.get_node_name_by_pubkey.side_effect = (
            lambda pk: "AliasUser" if pk == pk_alias else None
        )

        companion_resolver = MagicMock(
            side_effect=lambda pk: "CompanionUser" if pk == pk_companion else None
        )
        api = _make_api(storage=storage, companion_resolver=companion_resolver)

        result = api.resolve_pubkey_names(pubkeys=f"{pk_alias},{pk_companion}")

        assert result["success"] is True
        assert result["data"][pk_alias] == "AliasUser"
        assert result["data"][pk_companion] == "CompanionUser"

    # -----------------------------------------------------------------------
    # Whitespace trimming in pubkeys param
    # -----------------------------------------------------------------------

    def test_pubkeys_trimmed_of_whitespace(self):
        pk = "44" * 32
        storage = MagicMock()
        storage.get_node_name_by_pubkey.side_effect = (
            lambda p: "Trimmed" if p == pk else None
        )

        api = _make_api(storage=storage)
        result = api.resolve_pubkey_names(pubkeys=f"  {pk}  ")

        assert result["success"] is True
        assert result["data"][pk] == "Trimmed"

    # -----------------------------------------------------------------------
    # Storage unavailable → error response
    # -----------------------------------------------------------------------

    def test_storage_exception_returns_error(self):
        api = _make_api()
        api._get_storage = MagicMock(side_effect=Exception("Storage unavailable"))

        result = api.resolve_pubkey_names(pubkeys="aa" * 32)

        assert result["success"] is False
        assert "Storage unavailable" in result["error"]
