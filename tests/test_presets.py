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
    """The shipped wheel must contain at least 'waev' and 'letsmesh'."""
    names = list_presets()
    assert "waev" in names
    assert "letsmesh" in names


def test_get_preset_waev_has_two_brokers():
    """Waev preset shape: top-level 'brokers' list with two MC2MQTT entries."""
    preset = get_preset("waev")
    brokers = preset.get("brokers", [])
    assert len(brokers) == 2
    for b in brokers:
        assert "name" in b
        assert "host" in b
        assert b.get("format") == "waev"


def test_get_preset_unknown_returns_empty_dict():
    """Unknown preset names resolve to {} - no exception."""
    assert get_preset("definitely-not-a-real-preset") == {}


# --------------------------------------------------------------------
# Pass 1: preset expansion
# --------------------------------------------------------------------
def test_expand_preset_entries_inlines_bundled_brokers():
    """A {preset: waev} entry expands to the two Waev broker dicts."""
    expanded = _expand_preset_entries([{"preset": "waev"}])
    assert len(expanded) == 2
    names = [b["name"] for b in expanded]
    assert "waev-a" in names
    assert "waev-b" in names


def test_expand_preset_entries_drops_unknown_preset_with_warning(caplog):
    """An unknown preset is dropped; the daemon does not crash."""
    with caplog.at_level(logging.WARNING, logger="MQTTHandler"):
        expanded = _expand_preset_entries([{"preset": "bogus"}])
    assert expanded == []
    assert any("bogus" in record.message for record in caplog.records)


# --------------------------------------------------------------------
# Pass 2: override-by-name merge
# --------------------------------------------------------------------
def test_merge_overrides_by_name_disables_one_preset_broker():
    """Override AFTER preset wins: documented happy-path."""
    pre_expanded = _expand_preset_entries([{"preset": "waev"}])
    merged = _merge_overrides_by_name(pre_expanded + [{"name": "waev-b", "enabled": False}])
    assert len(merged) == 2
    by_name = {b["name"]: b for b in merged}
    assert by_name["waev-a"]["enabled"] is True
    assert by_name["waev-b"]["enabled"] is False


def test_merge_overrides_by_name_later_wins_documented_rule():
    """Override BEFORE preset is overwritten - locks the documented rule.

    The preset-expanded entry comes after the user's override in this case,
    so the preset wins and the user's `enabled: False` is silently lost. This
    is the published rule ("place override entries AFTER preset entries");
    this test exists so a future refactor can't quietly flip it.
    """
    user_first = [{"name": "waev-b", "enabled": False}]
    pipeline = _merge_overrides_by_name(user_first + _expand_preset_entries([{"preset": "waev"}]))
    by_name = {b["name"]: b for b in pipeline}
    # Preset wins - waev-b is enabled despite the user trying to disable it earlier.
    assert by_name["waev-b"]["enabled"] is True


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
