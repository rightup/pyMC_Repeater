"""Wire-validation checks for the unversioned companion operator API."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cherrypy
import pytest
from openhop_core import LocalIdentity
from openhop_core.companion.contact_store import ContactStore
from openhop_core.companion.models import Channel

from repeater.companion.bridge import RepeaterCompanionBridge
from repeater.web.companion_endpoints import CompanionAPIEndpoints


_PUBLIC_KEY = "aa" * 32


def _endpoint(body: dict, bridge=None) -> CompanionAPIEndpoints:
    if bridge is None:
        bridge = SimpleNamespace(
            prefs=SimpleNamespace(node_name="operator"),
            channels=SimpleNamespace(max_channels=40),
            send_text_message=AsyncMock(
                return_value=SimpleNamespace(
                    success=True,
                    is_flood=False,
                    expected_ack=123,
                )
            ),
            send_channel_message=AsyncMock(return_value=True),
            send_login=AsyncMock(return_value={"success": True}),
            send_status_request=AsyncMock(return_value={"success": True}),
            send_telemetry_request=AsyncMock(return_value={"success": True}),
            send_repeater_command=AsyncMock(return_value={"success": True}),
        )
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    endpoint._require_post = lambda: None
    endpoint._get_json_body = lambda: dict(body)
    endpoint._get_bridge = lambda **_params: bridge
    endpoint._run_async = lambda coro, timeout=30.0: asyncio.run(coro)
    return endpoint


def _invoke(name: str, endpoint: CompanionAPIEndpoints, **kwargs):
    return getattr(CompanionAPIEndpoints, name).__wrapped__(endpoint, **kwargs)


def test_legacy_operator_body_keeps_no_content_type_compatibility():
    raw = b'{"text":"legacy"}'
    cherrypy.serving.request.headers = {"Content-Length": str(len(raw))}
    cherrypy.serving.request.body = io.BytesIO(raw)
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)

    assert endpoint._get_json_body() == {"text": "legacy"}


@pytest.mark.parametrize(
    "method_name",
    ["index", "self_info", "contacts", "contact", "channels", "stats"],
)
def test_legacy_read_endpoints_reject_post_with_allow_get(method_name):
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    cherrypy.serving.request.method = "POST"
    cherrypy.serving.response.headers = {}

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke(method_name, endpoint)

    assert exc_info.value.status == 405
    assert cherrypy.response.headers["Allow"] == "GET"


def test_legacy_stats_rejects_unknown_type():
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    endpoint._require_get = lambda: None

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("stats", endpoint, type="pakcets")

    assert exc_info.value.status == 400


@pytest.mark.parametrize(
    "public_key",
    [
        "0x" + _PUBLIC_KEY,
        "aa " * 31 + "aa",
        _PUBLIC_KEY[:-1],
        _PUBLIC_KEY + "a",
    ],
)
def test_legacy_public_key_requires_exact_wire_hex(public_key):
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        endpoint._pub_key_from_hex(public_key)

    assert exc_info.value.status == 400


@pytest.mark.parametrize("raw_hash", [-1, 256, "0x100", "not-a-byte"])
def test_companion_selector_rejects_non_byte_hash(raw_hash):
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        endpoint._resolve_bridge_params({"companion_hash": raw_hash})

    assert exc_info.value.status == 400


def test_companion_selector_keeps_legacy_decimal_and_hex_strings():
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)

    assert endpoint._resolve_bridge_params({"companion_hash": "66"}) == {
        "companion_hash": 66
    }
    assert endpoint._resolve_bridge_params({"companion_hash": "0x42"}) == {
        "companion_hash": 0x42
    }


@pytest.mark.parametrize(
    "body",
    [
        {"pub_key": _PUBLIC_KEY, "text": {"not": "text"}},
        {"pub_key": _PUBLIC_KEY, "text": "x" * 161},
        {"pub_key": _PUBLIC_KEY, "text": "ok", "txt_type": 2},
        {"pub_key": _PUBLIC_KEY, "text": "ok", "typo": True},
    ],
)
def test_legacy_direct_send_rejects_invalid_wire_values(body):
    endpoint = _endpoint(body)

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("send_text", endpoint)

    assert exc_info.value.status == 400


def test_legacy_direct_send_preserves_numeric_string_txt_type():
    endpoint = _endpoint(
        {"pub_key": _PUBLIC_KEY, "text": "status", "txt_type": "1"}
    )

    response = _invoke("send_text", endpoint)

    assert response["data"]["sent"] is True
    endpoint._get_bridge().send_text_message.assert_awaited_once_with(
        bytes.fromhex(_PUBLIC_KEY),
        "status",
        txt_type=1,
        wait_for_ack=True,
    )


def test_legacy_direct_send_rejects_nul_before_bridge():
    endpoint = _endpoint(
        {"pub_key": _PUBLIC_KEY, "text": "visible\x00hidden"},
    )
    bridge = endpoint._get_bridge()

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("send_text", endpoint)

    assert exc_info.value.status == 400
    bridge.send_text_message.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"text": "missing channel"},
        {"channel_idx": True, "text": "bad type"},
        {"channel_idx": -1, "text": "negative"},
        {"channel_idx": 256, "text": "too high"},
    ],
)
def test_legacy_channel_send_requires_a_byte_index(body):
    endpoint = _endpoint(body)

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("send_channel_message", endpoint)

    assert exc_info.value.status == 400


def test_legacy_channel_send_checks_runtime_channel_and_text_budget():
    bridge = SimpleNamespace(
        prefs=SimpleNamespace(node_name="n" * 31),
        channels=SimpleNamespace(max_channels=2),
        send_channel_message=AsyncMock(return_value=True),
    )

    with pytest.raises(cherrypy.HTTPError, match="channel_idx out of range"):
        _invoke(
            "send_channel_message",
            _endpoint({"channel_idx": 2, "text": "hello"}, bridge),
        )
    with pytest.raises(cherrypy.HTTPError, match="for this channel sender name"):
        _invoke(
            "send_channel_message",
            _endpoint({"channel_idx": 1, "text": "x" * 128}, bridge),
        )
    bridge.send_channel_message.assert_not_awaited()


def test_legacy_channel_send_rejects_nul_before_bridge():
    endpoint = _endpoint(
        {"channel_idx": 0, "text": "visible\x00hidden"},
    )
    bridge = endpoint._get_bridge()

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("send_channel_message", endpoint)

    assert exc_info.value.status == 400
    bridge.send_channel_message.assert_not_awaited()


def test_legacy_channel_send_rechecks_budget_after_committed_frame_rename():
    injected = []

    async def inject(packet, **_kwargs):
        injected.append(packet)
        return True

    bridge = RepeaterCompanionBridge(
        LocalIdentity(),
        inject,
        node_name="A",
    )
    assert bridge.channels.set(1, Channel(name="test", secret=bytes(32)))
    endpoint = _endpoint({"channel_idx": 1, "text": "x" * 128}, bridge)

    def run_with_parallel_rename(coro, timeout=30.0):
        async def race():
            await bridge.state_mutation_lock.acquire()
            send_task = asyncio.create_task(coro)
            await asyncio.sleep(0)
            # The request validated the short old name before the bridge's
            # committed-state barrier. A parallel Frame command then commits
            # the longest legal name before Core builds the packet.
            bridge.prefs.node_name = "N" * 31
            bridge.state_mutation_lock.release()
            return await send_task

        return asyncio.run(race())

    endpoint._run_async = run_with_parallel_rename
    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("send_channel_message", endpoint)

    assert exc_info.value.status == 400
    assert "text exceeds 127 UTF-8 bytes" in str(exc_info.value)
    assert injected == []


@pytest.mark.parametrize("password", ["x" * 16, "🔐" * 4, 123])
def test_legacy_login_rejects_passwords_that_cannot_fit_wire(password):
    endpoint = _endpoint({"pub_key": _PUBLIC_KEY, "password": password})

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("login", endpoint)

    assert exc_info.value.status == 400


def test_legacy_login_rejects_nul_password_before_bridge():
    endpoint = _endpoint(
        {"pub_key": _PUBLIC_KEY, "password": "visible\x00hidden"},
    )
    bridge = endpoint._get_bridge()

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("login", endpoint)

    assert exc_info.value.status == 400
    bridge.send_login.assert_not_awaited()


@pytest.mark.parametrize(
    "timeout",
    [True, 0, -1, 61, float("nan"), float("inf"), "not-a-timeout"],
)
def test_legacy_status_rejects_unsafe_timeouts(timeout):
    endpoint = _endpoint({"pub_key": _PUBLIC_KEY, "timeout": timeout})

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("request_status", endpoint)

    assert exc_info.value.status == 400


def test_legacy_status_preserves_numeric_string_timeout():
    endpoint = _endpoint({"pub_key": _PUBLIC_KEY, "timeout": "15"})

    assert _invoke("request_status", endpoint)["data"]["success"] is True
    endpoint._get_bridge().send_status_request.assert_awaited_once_with(
        bytes.fromhex(_PUBLIC_KEY),
        timeout=15.0,
    )


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_legacy_telemetry_requires_real_json_booleans(value):
    endpoint = _endpoint(
        {
            "pub_key": _PUBLIC_KEY,
            "want_base": value,
        }
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("request_telemetry", endpoint)

    assert exc_info.value.status == 400


def test_legacy_telemetry_passes_explicit_false_without_coercion():
    endpoint = _endpoint(
        {
            "pub_key": _PUBLIC_KEY,
            "want_base": False,
            "want_location": True,
            "want_environment": False,
            "timeout": 20,
        }
    )

    assert _invoke("request_telemetry", endpoint)["data"]["success"] is True
    endpoint._get_bridge().send_telemetry_request.assert_awaited_once_with(
        bytes.fromhex(_PUBLIC_KEY),
        want_base=False,
        want_location=True,
        want_environment=False,
        timeout=20.0,
    )


def test_legacy_command_validates_combined_wire_length_and_types():
    with pytest.raises(cherrypy.HTTPError, match="command and parameters"):
        _invoke(
            "send_command",
            _endpoint(
                {
                    "pub_key": _PUBLIC_KEY,
                    "command": "x" * 100,
                    "parameters": "y" * 60,
                }
            ),
        )
    with pytest.raises(cherrypy.HTTPError, match="parameters must be a string"):
        _invoke(
            "send_command",
            _endpoint(
                {
                    "pub_key": _PUBLIC_KEY,
                    "command": "set",
                    "parameters": 1,
                }
            ),
        )


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            {
                "pub_key": _PUBLIC_KEY,
                "command": "status\x00ignored",
            },
            "command",
        ),
        (
            {
                "pub_key": _PUBLIC_KEY,
                "command": "set",
                "parameters": "visible\x00ignored",
            },
            "parameters",
        ),
    ],
)
def test_legacy_command_rejects_nul_before_bridge(body, field):
    bridge = SimpleNamespace(
        send_repeater_command=AsyncMock(return_value={"success": True}),
    )
    endpoint = _endpoint(body, bridge=bridge)

    with pytest.raises(cherrypy.HTTPError, match=f"{field} must not contain NUL") as exc_info:
        _invoke("send_command", endpoint)

    assert exc_info.value.status == 400
    bridge.send_repeater_command.assert_not_awaited()


def test_legacy_command_without_parameters_keeps_none_wire_value():
    bridge = SimpleNamespace(
        send_repeater_command=AsyncMock(return_value={"success": True}),
    )
    endpoint = _endpoint(
        {
            "pub_key": _PUBLIC_KEY,
            "command": "status",
        },
        bridge=bridge,
    )

    result = _invoke("send_command", endpoint)

    assert result["success"] is True
    bridge.send_repeater_command.assert_awaited_once_with(
        bytes.fromhex(_PUBLIC_KEY),
        "status",
        None,
    )


@pytest.mark.parametrize(
    "body",
    [
        {"latitude": 1},
        {"longitude": 1},
        {"latitude": float("nan"), "longitude": 1},
        {"latitude": 91, "longitude": 1},
        {"latitude": 1, "longitude": 181},
        {"latitude": 1, "longitude": 1, "latitide": 1},
    ],
)
def test_legacy_location_requires_exact_finite_coordinates(body):
    endpoint = _endpoint(body)

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("set_advert_location", endpoint)

    assert exc_info.value.status == 400


def test_legacy_integer_parser_rejects_fractional_and_boolean_values():
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)

    for value in (True, 1.5, "1.5"):
        with pytest.raises(cherrypy.HTTPError) as exc_info:
            endpoint._legacy_integer(value, "limit", low=1)
        assert exc_info.value.status == 400


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hours", True),
        ("hours", 876_001),
        ("limit", False),
        ("limit", 1_000_001),
    ],
)
def test_legacy_import_rejects_unsafe_numeric_bounds(field, value):
    endpoint = _endpoint(
        {
            "companion_name": "home",
            field: value,
        }
    )

    with pytest.raises(cherrypy.HTTPError) as exc_info:
        _invoke("import_repeater_contacts", endpoint)

    assert exc_info.value.status == 400


def test_legacy_import_skips_corrupt_numeric_rows(caplog):
    good_key = b"\x01" * 32
    invalid_rows = {
        (b"\x02" * 32).hex(): {
            "contact_type": "companion",
            "last_seen": float("nan"),
        },
        (b"\x03" * 32).hex(): {
            "contact_type": "companion",
            "last_seen": 2,
            "latitude": float("inf"),
        },
        (b"\x04" * 32).hex(): {
            "contact_type": "companion",
            "last_seen": -1,
        },
        (b"\x05" * 32).hex(): {
            "contact_type": "companion",
            "last_seen": 1 << 63,
        },
        ("06 " * 31 + "06"): {
            "contact_type": "companion",
            "last_seen": 2,
        },
        (b"\x07" * 32).hex(): {
            "contact_type": "companion",
            "node_name": "bad\x00name",
            "last_seen": 2,
        },
        (b"\x08" * 32).hex(): {
            "contact_type": "companion",
            "node_name": "x" * 32,
            "last_seen": 2,
        },
        good_key.hex(): {
            "contact_type": "companion",
            "node_name": "good",
            "last_seen": 1,
            "latitude": 10,
            "longitude": -20,
        },
    }
    bridge = SimpleNamespace(
        state_mutation_lock=asyncio.Lock(),
        contacts=ContactStore(max_contacts=10),
        _contact_storage_dict=RepeaterCompanionBridge._contact_storage_dict,
        _contact_changes=RepeaterCompanionBridge._contact_changes,
    )

    async def persist(_changes):
        return None

    async def notify(*_args):
        return None

    bridge._persist_contact_changes = persist
    bridge._notify_observers = notify
    endpoint = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    endpoint._get_bridge = lambda **_params: bridge
    endpoint._get_sqlite_handler = lambda: SimpleNamespace(
        get_neighbors=lambda: invalid_rows
    )

    with caplog.at_level("WARNING"):
        result = asyncio.run(
            endpoint._import_repeater_contacts(
                {},
                contact_types=None,
                hours=None,
                limit=None,
            )
        )

    assert result["imported"] == 1
    assert [contact.public_key for contact in bridge.contacts.get_all()] == [good_key]
    assert "Skipping repeater advert" in caplog.text
