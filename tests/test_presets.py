"""Tests for the bundled MQTT broker preset system.

Locks the public contract documented in `config.yaml.example` and the
behavior contract in the feat/generalized-mqtt PR.
"""

import logging

import pytest

from repeater.data_acquisition.mqtt_handler import (
    MC2MQTT_FORMATS,
    MeshCoreToMqttPusher,
    _BrokerConnection,
    _expand_preset_entries,
    _merge_overrides_by_name,
    _summarize_payload_for_log,
    _truncate_middle,
    get_mqtt_error_message,
)
from repeater.presets import get_preset, list_presets


# --------------------------------------------------------------------
# Preset loader contract
# --------------------------------------------------------------------
def test_list_presets_returns_bundled_names():
    """The shipped wheel must contain the public-network presets."""
    names = list_presets()
    assert "waev" in names
    assert "letsmesh" in names
    assert "meshmapper" in names
    assert "meshat-se" in names


def test_get_preset_waev_uses_alias_for_server_side_failover():
    """Waev preset ships ONE broker pointing at the alias host.

    The Waev edge Worker (waev/src/router.ts:
    MQTT_PRIMARY_FAILOVER_TIMEOUT_MS) does server-side A/B failover on
    `mqtt.waev.app`. Repeaters connect once and let the Worker handle
    redundancy - we explicitly do NOT want to materialize two independent
    client connections, because that would defeat the dedup-on-pubkey-hash
    contract on the waev ingest side.
    """
    preset = get_preset("waev")
    brokers = preset.get("brokers", [])
    assert len(brokers) == 1, "Waev preset should be a single alias broker"
    broker = brokers[0]
    assert broker["host"] == "mqtt.waev.app"
    assert broker["audience"] == "mqtt.waev.app"
    assert broker.get("format") == "waev"


def test_get_preset_unknown_returns_empty_dict():
    """Unknown preset names resolve to {} - no exception."""
    assert get_preset("definitely-not-a-real-preset") == {}


def test_get_preset_waev_carries_ui_metadata():
    """Waev preset exposes top-level display_name + website for the UI.

    These optional top-level fields are consumed by
    ``GET /api/broker_presets`` so the admin frontend's "From Template"
    dropdown does not need to bundle its own copy of the broker catalogue.
    """
    preset = get_preset("waev")
    assert preset.get("display_name") == "Waev"
    assert preset.get("website") == "https://waev.app"


def test_get_preset_letsmesh_carries_ui_metadata():
    """LetsMesh preset exposes the same top-level UI metadata as Waev."""
    preset = get_preset("letsmesh")
    assert preset.get("display_name") == "LetsMesh"
    assert preset.get("website") == "https://letsmesh.net"


def test_get_preset_meshmapper_is_single_broker_mc2mqtt():
    """MeshMapper preset is a single MC2MQTT broker on mqtt.meshmapper.net.

    The preset intentionally re-uses the `letsmesh` format value because
    MeshMapper today speaks the standard MC2MQTT wire format with no
    documented deviations. A dedicated `meshmapper` format value can be
    introduced later if/when wire-level differentiation lands.
    """
    preset = get_preset("meshmapper")
    assert preset.get("display_name") == "MeshMapper"
    assert preset.get("website") == "https://meshmapper.net"
    brokers = preset.get("brokers", [])
    assert len(brokers) == 1
    broker = brokers[0]
    assert broker["host"] == "mqtt.meshmapper.net"
    assert broker["audience"] == "mqtt.meshmapper.net"
    assert broker.get("format") == "letsmesh"


# --------------------------------------------------------------------
# GET /api/broker_presets - UI-facing shape
# --------------------------------------------------------------------
class _StubEndpoint:
    """Minimal stand-in for APIEndpoints to exercise the broker_presets method.

    The full APIEndpoints.__init__ pulls in ConfigManager, AuthAPIEndpoints,
    CompanionAPIEndpoints, UpdateAPIEndpoints, and CADCalibrationEngine, none
    of which are relevant to this read-only handler. The stub satisfies just
    the four protocol points the method actually touches.
    """

    config = {}

    def _is_cors_enabled(self):
        return False

    def _set_cors_headers(self):
        pass

    def _success(self, data, **kwargs):
        result = {"success": True, "data": data}
        result.update(kwargs)
        return result

    def _error(self, error):
        return {"success": False, "error": str(error)}


def _call_broker_presets():
    """Bind the unbound method onto a stub instance and invoke it."""
    from repeater.web.api_endpoints import APIEndpoints

    return APIEndpoints.broker_presets(_StubEndpoint())


def test_broker_presets_returns_success_with_list_payload():
    """Happy path: response wraps a list of preset entries."""
    response = _call_broker_presets()
    assert response["success"] is True
    assert isinstance(response["data"], list)
    # At least waev + letsmesh + meshmapper ship in the public catalogue.
    assert len(response["data"]) >= 3


def test_broker_presets_waev_entry_is_ui_ready():
    """Waev entry carries id, display name, website, and a single broker.

    The single broker points at the alias `mqtt.waev.app`, which is where
    Waev's edge Worker provides server-side A/B failover. Audience equals
    host so JWT verification stays consistent if/when the Worker turns
    on aud enforcement.
    """
    response = _call_broker_presets()
    waev = next(p for p in response["data"] if p["id"] == "waev")
    assert waev["name"] == "Waev"
    assert waev["website"] == "https://waev.app"
    assert len(waev["brokers"]) == 1
    broker = waev["brokers"][0]
    assert broker["host"] == "mqtt.waev.app"
    assert broker["audience"] == "mqtt.waev.app"


def test_broker_presets_letsmesh_entry_is_ui_ready():
    """LetsMesh entry mirrors the Waev contract."""
    response = _call_broker_presets()
    letsmesh = next(p for p in response["data"] if p["id"] == "letsmesh")
    assert letsmesh["name"] == "LetsMesh"
    assert letsmesh["website"] == "https://letsmesh.net"
    assert len(letsmesh["brokers"]) == 2


# --------------------------------------------------------------------
# Pass 1: preset expansion
# --------------------------------------------------------------------
def test_expand_preset_entries_inlines_bundled_brokers():
    """A {preset: waev} entry expands to the single Waev alias broker."""
    expanded = _expand_preset_entries([{"preset": "waev"}])
    assert len(expanded) == 1
    assert expanded[0]["name"] == "Waev"
    assert expanded[0]["host"] == "mqtt.waev.app"


def test_expand_preset_entries_drops_unknown_preset_with_warning(caplog):
    """An unknown preset is dropped; the daemon does not crash."""
    with caplog.at_level(logging.WARNING, logger="MQTTHandler"):
        expanded = _expand_preset_entries([{"preset": "bogus"}])
    assert expanded == []
    assert any("bogus" in record.message for record in caplog.records)


# --------------------------------------------------------------------
# Pass 2: override-by-name merge
# --------------------------------------------------------------------
def test_merge_overrides_by_name_pins_waev_to_primary():
    """Override AFTER preset wins: an operator can pin to broker A only.

    Use case: an operator wants to bypass the server-side failover and
    target broker A directly (e.g. while debugging a B-specific issue).
    They re-point the single Waev broker's host/audience to mqtt-a.waev.app
    via an override after the preset expansion.
    """
    pre_expanded = _expand_preset_entries([{"preset": "waev"}])
    merged = _merge_overrides_by_name(
        pre_expanded + [{"name": "Waev", "host": "mqtt-a.waev.app", "audience": "mqtt-a.waev.app"}]
    )
    assert len(merged) == 1
    assert merged[0]["host"] == "mqtt-a.waev.app"
    assert merged[0]["audience"] == "mqtt-a.waev.app"


def test_merge_overrides_by_name_later_wins_documented_rule():
    """Override BEFORE preset is overwritten - locks the documented rule.

    The preset-expanded entry comes after the user's override in this case,
    so the preset wins and the user's host override is silently lost. This
    is the published rule ("place override entries AFTER preset entries");
    this test exists so a future refactor can't quietly flip it.
    """
    user_first = [{"name": "Waev", "host": "mqtt-a.waev.app"}]
    pipeline = _merge_overrides_by_name(user_first + _expand_preset_entries([{"preset": "waev"}]))
    # Preset wins - host is reset to the alias.
    assert pipeline[0]["host"] == "mqtt.waev.app"


# --------------------------------------------------------------------
# MC2MQTT family parity in topic resolution
# --------------------------------------------------------------------
def _make_broker_connection(format_value: str) -> _BrokerConnection:
    """Build a minimal _BrokerConnection for topic-structure assertions."""
    broker = {
        "name": f"test-{format_value}",
        "host": "test.example",
        "port": 443,
        "format": format_value,
        "enabled": True,
    }
    return _BrokerConnection(
        broker=broker,
        local_identity=object(),
        public_key="ABCD" * 16,  # 64-char hex stand-in
        iata_code="LAX",
        jwt_expiry_minutes=10,
        email="",
        owner="",
        broker_index=0,
        node_name="testnode",
    )


def test_mc2mqtt_formats_share_topic_structure():
    """Every MC2MQTT family member resolves to the canonical topic prefix."""
    expected_mc2mqtt = "meshcore/LAX/" + ("ABCD" * 16)
    for fmt in MC2MQTT_FORMATS:
        conn = _make_broker_connection(fmt)
        assert conn.base_topic == expected_mc2mqtt, f"format '{fmt}' should be MC2MQTT family"

    # Legacy custom-MQTT format uses a different (operator-defined) prefix.
    legacy = _make_broker_connection("mqtt")
    assert legacy.base_topic == "meshcore/repeater/testnode"


# --------------------------------------------------------------------
# Legacy `letsmesh:` block migration
# --------------------------------------------------------------------
@pytest.mark.parametrize(
    "broker_index, expected_disabled_names",
    [
        (-1, set()),  # both brokers enabled (preset default)
        (0, {"US West (LetsMesh v1)"}),  # EU only - US disabled
        (1, {"Europe (LetsMesh v1)"}),  # US only - EU disabled
    ],
)
def test_legacy_letsmesh_block_migrates_to_preset_for_each_broker_index(
    broker_index, expected_disabled_names
):
    """Legacy letsmesh.broker_index produces the same broker set as before.

    The new migrator emits {preset: letsmesh} plus disable overrides; running
    that through the expansion+merge pipeline must preserve the legacy
    enabled/disabled topology.
    """
    legacy_cfg = {"enabled": True, "broker_index": broker_index}
    # Call the unbound method - it doesn't read instance state.
    entries = MeshCoreToMqttPusher.convert_letsmesh_to_broker_config(
        MeshCoreToMqttPusher.__new__(MeshCoreToMqttPusher), legacy_cfg
    )

    expanded = _expand_preset_entries(entries)
    merged = _merge_overrides_by_name(expanded)

    # Always two LetsMesh brokers come out of the pipeline.
    assert len(merged) == 2
    by_name = {b["name"]: b for b in merged}
    for name, broker in by_name.items():
        if name in expected_disabled_names:
            assert broker["enabled"] is False, f"{name} should be disabled for index {broker_index}"
        else:
            assert broker["enabled"] is True, f"{name} should be enabled for index {broker_index}"


def test_disconnect_error_message_uses_paho_legacy_connection_lost_string():
    """Legacy paho disconnect rc=16 should not be mislabeled as a protocol error."""
    assert get_mqtt_error_message(16, is_disconnect=True) == "The connection was lost."


def test_disconnect_error_message_preserves_mqtt_v5_reason_codes():
    """Real MQTT v5 disconnect reason codes should still decode to their reason names."""
    assert get_mqtt_error_message(130, is_disconnect=True) == "Protocol error (code 130)"


def test_connect_failure_schedules_reconnect_with_actual_error_reason(monkeypatch):
    """Reconnect logs should reflect the connect failure, not the default reason string."""
    conn = _make_broker_connection("letsmesh")
    captured = {}

    def fake_schedule_reconnect(reason="connection lost"):
        captured["reason"] = reason

    monkeypatch.setattr(conn, "_schedule_reconnect", fake_schedule_reconnect)

    conn._on_connect(client=None, userdata=None, flags=None, rc=5)

    assert captured["reason"] == "Not authorized (JWT signature/format invalid)"


def test_schedule_reconnect_uses_exponential_backoff_and_cap(monkeypatch):
    conn = _make_broker_connection("letsmesh")

    captured = {"delay": None, "started": False}

    class _Timer:
        def __init__(self, delay, cb):
            captured["delay"] = delay
            self.daemon = False
            self._cb = cb

        def start(self):
            captured["started"] = True

        def cancel(self):
            return None

    monkeypatch.setattr("repeater.data_acquisition.mqtt_handler.threading.Timer", _Timer)

    conn._reconnect_attempts = 0
    conn._schedule_reconnect("first")
    assert captured["delay"] == 5
    assert captured["started"] is True

    # Large attempt count should clamp to max delay.
    conn._reconnect_attempts = 99
    conn._schedule_reconnect("later")
    assert captured["delay"] == conn._max_reconnect_delay


def test_on_disconnect_without_running_flag_still_schedules_reconnect(monkeypatch):
    conn = _make_broker_connection("letsmesh")
    conn._running = False

    called = {"count": 0}

    def _fake_ensure(reason="connection lost"):
        called["count"] += 1

    monkeypatch.setattr(conn, "ensure_reconnect_scheduled", _fake_ensure)

    # A callback can race with state clearing. Recovery must not depend on
    # _running still being true when the callback arrives.
    conn._on_disconnect(client=None, userdata=None, rc=1)
    assert called["count"] == 1


def test_clean_broker_disconnect_schedules_reconnect(monkeypatch):
    conn = _make_broker_connection("mqtt")
    conn._running = True
    called = []

    monkeypatch.setattr(
        conn,
        "ensure_reconnect_scheduled",
        lambda reason="connection lost": called.append(reason),
    )

    conn._on_disconnect(client=None, userdata=None, rc=0)

    assert called == ["broker closed connection"]


def test_intentional_disconnect_sets_shutdown_guard(monkeypatch):
    conn = _make_broker_connection("mqtt")
    conn._running = True
    monkeypatch.setattr(conn.client, "loop_stop", lambda: None)
    monkeypatch.setattr(conn.client, "disconnect", lambda: None)

    conn.disconnect()

    assert conn._shutdown_requested is True


def test_attempt_reconnect_failure_reschedules(monkeypatch):
    conn = _make_broker_connection("letsmesh")
    conn._running = False
    conn._reconnect_timer = object()

    monkeypatch.setattr(conn, "_set_credentials", lambda: None)
    monkeypatch.setattr(conn.client, "loop_stop", lambda: None)
    monkeypatch.setattr(conn.client, "loop_start", lambda: None)

    def _boom_connect(*args, **kwargs):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(conn.client, "connect", _boom_connect)

    called = {"count": 0}

    def _fake_schedule(reason="connection lost"):
        called["count"] += 1

    monkeypatch.setattr(conn, "_schedule_reconnect", _fake_schedule)

    conn._attempt_reconnect("network")
    assert called["count"] == 1


def test_on_pre_connect_refreshes_jwt_credentials(monkeypatch):
    """JWT credentials should be refreshed on each (re)connect attempt."""
    conn = _make_broker_connection("letsmesh")
    conn.use_jwt_auth = True

    called = {"count": 0}

    def fake_set_credentials():
        called["count"] += 1

    monkeypatch.setattr(conn, "_set_credentials", fake_set_credentials)

    conn._on_pre_connect(client=None, userdata=None)

    assert called["count"] == 1


def test_payload_summary_omits_full_raw_dump_for_packet_logs():
    """MQTT debug logging should summarize packet payloads instead of dumping JSON blobs."""
    payload = {
        "type": "PACKET",
        "packet_type": "4",
        "route": "F",
        "origin": "NWTBASE02",
        "len": "120",
        "payload_len": "115",
        "raw": "aa" * 120,
        "hash": "DD63C8077B5912FC",
    }

    summary = _summarize_payload_for_log(payload)

    assert "type=PACKET" in summary
    assert "route=F" in summary
    assert "raw_bytes=120" in summary
    assert '"raw"' not in summary
    assert "aa" * 20 not in summary


def test_truncate_middle_preserves_topic_prefix_and_suffix():
    """Long MQTT topics should keep both routing context and the final path segment visible."""
    topic = "meshcore/BOH/BEEF2F7F8632ADE3461D42D1653A0229310E424C37324A6768071A629DFDAA32/packets"

    truncated = _truncate_middle(topic)

    assert truncated.startswith("meshcore/BOH/BEEF2F7F863")
    assert truncated.endswith("9DFDAA32/packets")
    assert " ... " in truncated
