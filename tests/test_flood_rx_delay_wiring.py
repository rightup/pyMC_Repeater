"""Wiring for the flood reception-quality delay (MeshCore "set rxdelay").

The delay itself lives in the core Dispatcher; the repeater's job is to feed
``delays.rx_delay_base`` from its config into ``dispatcher.rx_delay_base`` at
startup and on live config updates, and to expose the same value through the
mesh CLI. The packet scorer the delay uses is shared with the engine's
TX-delay scoring.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from openhop_core.protocol.packet_utils import packet_score

from repeater.config_manager import ConfigManager
from repeater.engine import RepeaterHandler
from repeater.handler_helpers.mesh_cli import MeshCLI


class _DummyDispatcher:
    def __init__(self):
        self.rx_delay_base = 0.0

    def set_default_path_hash_mode(self, mode):
        pass


def _daemon_with_dispatcher(config):
    return SimpleNamespace(
        config=config,
        radio=None,
        repeater_handler=None,
        advert_helper=None,
        dispatcher=_DummyDispatcher(),
    )


def _cfg_mgr():
    return SimpleNamespace(
        save_to_file=MagicMock(return_value=(True, None)),
        live_update_daemon=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Live config update -> dispatcher
# ---------------------------------------------------------------------------
def test_live_update_applies_rx_delay_base_to_dispatcher():
    config = {"delays": {"rx_delay_base": 10.0}}
    daemon = _daemon_with_dispatcher(config)
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["delays"])
    assert daemon.dispatcher.rx_delay_base == 10.0


def test_live_update_clears_rx_delay_base_when_removed():
    config = {"delays": {}}
    daemon = _daemon_with_dispatcher(config)
    daemon.dispatcher.rx_delay_base = 10.0
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["delays"])
    assert daemon.dispatcher.rx_delay_base == 0.0


def test_live_update_without_dispatcher_does_not_crash():
    config = {"delays": {"rx_delay_base": 5.0}}
    daemon = _daemon_with_dispatcher(config)
    daemon.dispatcher = None
    manager = ConfigManager("/tmp/config.yaml", config, daemon)

    assert manager.live_update_daemon(["delays"])


# ---------------------------------------------------------------------------
# Mesh CLI reads/writes the delays section (the one the dispatcher uses)
# ---------------------------------------------------------------------------
def test_cli_set_rxdelay_writes_delays_section():
    config = {"repeater": {}, "mesh": {}}
    cli = MeshCLI("/tmp/cfg.yaml", config, _cfg_mgr())

    assert cli._cmd_set("rxdelay 10") == "OK"
    assert config["delays"]["rx_delay_base"] == 10.0
    assert "rx_delay_base" not in config["repeater"]
    cli.config_manager.live_update_daemon.assert_called_with(["repeater", "delays"])


def test_cli_get_rxdelay_reads_delays_section():
    config = {"repeater": {"rx_delay_base": 99.0}, "mesh": {}, "delays": {"rx_delay_base": 7.5}}
    cli = MeshCLI("/tmp/cfg.yaml", config, _cfg_mgr())

    assert cli._cmd_get("rxdelay") == "> 7.5"


def test_cli_get_rxdelay_defaults_to_disabled():
    cli = MeshCLI("/tmp/cfg.yaml", {"repeater": {}, "mesh": {}}, _cfg_mgr())

    assert cli._cmd_get("rxdelay") == "> 0.0"


# ---------------------------------------------------------------------------
# Engine scoring delegates to the shared core scorer
# ---------------------------------------------------------------------------
def test_engine_score_matches_shared_packet_score():
    for snr, packet_len, sf in ((-5.0, 24, 7), (-10.0, 128, 12), (5.0, 50, 8), (0.0, 200, 10)):
        assert RepeaterHandler.calculate_packet_score(snr, packet_len, sf) == packet_score(
            snr, sf, packet_len
        )
