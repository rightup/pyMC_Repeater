from unittest.mock import MagicMock, patch


from repeater.handler_helpers.repeater_cli import MeshCLI, RepeaterCLI
from repeater.identity_manager import IdentityManager


class _FakeIdentity:
    def __init__(self, pubkey: bytes, addr: bytes = b"\xaa\xbb"):
        self._pubkey = pubkey
        self._addr = addr

    def get_public_key(self):
        return self._pubkey

    def get_address_bytes(self):
        return self._addr


def _base_config():
    return {
        "version": "9.9.9",
        "repeater": {
            "name": "node-1",
            "mode": "forward",
            "latitude": 12.3,
            "longitude": 45.6,
            "airtime_factor": 1.1,
            "advert_interval_minutes": 120,
            "flood_advert_interval_hours": 24,
            "max_flood_hops": 32,
            "rx_delay_base": 0.4,
            "tx_delay_factor": 1.2,
            "direct_tx_delay_factor": 0.7,
            "multi_acks": 2,
            "interference_threshold": -111,
            "agc_reset_interval": 8,
        },
        "radio": {
            "frequency": 915000000,
            "bandwidth": 125000,
            "spreading_factor": 7,
            "coding_rate": 5,
            "tx_power": 22,
        },
        "security": {"guest_password": "guest", "allow_read_only": True},
    }


def test_identity_manager_register_lookup_and_collision_paths():
    mgr = IdentityManager(config={})
    id_a = _FakeIdentity(bytes([0x11]) + b"A" * 31, addr=b"\x01\x02")
    id_b_collision = _FakeIdentity(bytes([0x11]) + b"B" * 31, addr=b"\x03\x04")

    assert mgr.register_identity("alpha", id_a, {"k": 1}, "repeater") is True
    assert mgr.has_identity(0x11) is True
    assert mgr.get_identity_by_hash(0x11)[0] is id_a
    assert mgr.get_identity_by_name("alpha")[0] is id_a

    # Collision on first pubkey byte should be rejected.
    assert mgr.register_identity("beta", id_b_collision, {"k": 2}, "room_server") is False


def test_identity_manager_list_and_type_filtering():
    mgr = IdentityManager(config={})
    id_a = _FakeIdentity(bytes([0x22]) + b"A" * 31)
    id_b = _FakeIdentity(bytes([0x33]) + b"B" * 31)

    mgr.register_identity("rep-main", id_a, {"x": 1}, "repeater")
    mgr.register_identity("room-a", id_b, {"y": 2}, "room_server")

    listed = mgr.list_identities()
    assert len(listed) == 2
    assert any(item["hash"] == "0x22" and item["name"] == "repeater:rep-main" for item in listed)
    assert any(item["hash"] == "0x33" and item["type"] == "room_server" for item in listed)

    assert mgr.has_identity_type("repeater") is True
    assert mgr.has_identity_type("room_server") is True
    assert mgr.has_identity_type("unknown") is False

    by_type = mgr.get_identities_by_type("room_server")
    assert len(by_type) == 1
    assert by_type[0][0] == "room-a"


def test_identity_manager_list_handles_none_identity_fields():
    mgr = IdentityManager(config={})
    mgr.identities[0x44] = (None, {}, "repeater")
    mgr.registered_hashes[0x44] = "repeater:ghost"

    listed = mgr.list_identities()
    assert listed[0]["address"] == "N/A"
    assert listed[0]["public_key"] is None


def test_repeater_cli_alias_points_to_mesh_cli():
    assert RepeaterCLI is MeshCLI


def test_cli_non_admin_and_prefix_passthrough():
    cfg = _base_config()
    save = MagicMock()
    cli = MeshCLI("/tmp/config.yaml", cfg, save)

    assert cli.handle_command(b"x", "help", is_admin=False) == "Error: Admin permission required"
    assert cli.handle_command(b"x", "01|help set", is_admin=True).startswith("01|")


def test_cli_help_and_route_unknown_commands():
    cli = MeshCLI("/tmp/config.yaml", _base_config(), MagicMock())

    help_text = cli._route_command("help")
    assert "pyMC CLI Commands" in help_text

    assert "No detailed help" in cli._route_command("help not-a-topic")
    assert cli._route_command("start ota").startswith("Error:")
    assert cli._route_command("gps now").startswith("Error:")
    assert cli._route_command("stats-air").startswith("Error:")
    assert cli._route_command("totally-unknown") == "Unknown command"


def test_cli_reboot_uses_service_utils_result():
    cli = MeshCLI("/tmp/config.yaml", _base_config(), MagicMock())

    with patch("repeater.service_utils.restart_service", return_value=(True, "restarted")):
        assert cli._cmd_reboot() == "OK - restarted"

    with patch("repeater.service_utils.restart_service", return_value=(False, "denied")):
        assert cli._cmd_reboot() == "Error: denied"


def test_cli_clock_time_password_and_version_commands():
    cfg = _base_config()
    save = MagicMock()
    cli = MeshCLI("/tmp/config.yaml", cfg, save, identity_type="room_server")

    assert "UTC" in cli._cmd_clock("clock")
    assert "not needed" in cli._cmd_clock("clock sync")
    assert cli._cmd_clock("clock bad") == "Unknown clock command"
    assert cli._cmd_time("time 1 2").startswith("Error:")

    assert cli._cmd_password("password   ") == "Error: Password cannot be empty"
    assert cli._cmd_password("password newpass") == "password now: newpass"
    assert cfg["security"]["password"] == "newpass"
    save.assert_called()

    assert cli._cmd_version() == "pyMC_room_server v9.9.9"


def test_cli_get_commands_cover_expected_fields():
    cli = MeshCLI("/tmp/config.yaml", _base_config(), MagicMock())

    assert cli._cmd_get("af") == "> 1.1"
    assert cli._cmd_get("name") == "> node-1"
    assert cli._cmd_get("repeat") == "> on"
    assert cli._cmd_get("lat") == "> 12.3"
    assert cli._cmd_get("lon") == "> 45.6"
    assert cli._cmd_get("radio") == "> 915.0,125.0,7,5"
    assert cli._cmd_get("freq") == "> 915.0"
    assert cli._cmd_get("tx") == "> 22"
    assert cli._cmd_get("role") == "> repeater"
    assert cli._cmd_get("guest.password") == "> guest"
    assert cli._cmd_get("allow.read.only") == "> on"
    assert cli._cmd_get("advert.interval") == "> 120"
    assert cli._cmd_get("flood.advert.interval") == "> 24"
    assert cli._cmd_get("flood.max") == "> 32"
    assert cli._cmd_get("rxdelay") == "> 0.4"
    assert cli._cmd_get("txdelay") == "> 1.2"
    assert cli._cmd_get("direct.txdelay") == "> 0.7"
    assert cli._cmd_get("multi.acks") == "> 2"
    assert cli._cmd_get("int.thresh") == "> -111"
    assert cli._cmd_get("agc.reset.interval") == "> 8"
    assert cli._cmd_get("public.key").startswith("Error:")
    assert cli._cmd_get("missing") == "??: missing"


def test_cli_set_commands_apply_and_validate_ranges():
    cfg = _base_config()
    save = MagicMock()
    cli = MeshCLI("/tmp/config.yaml", cfg, save)

    assert cli._cmd_set("af 2.5") == "OK"
    assert cfg["repeater"]["airtime_factor"] == 2.5

    assert cli._cmd_set("name repeater-z") == "OK"
    assert cfg["repeater"]["name"] == "repeater-z"

    assert cli._cmd_set("repeat off").endswith("OFF")
    assert cfg["repeater"]["mode"] == "monitor"

    assert cli._cmd_set("lat 1.25") == "OK"
    assert cli._cmd_set("lon 2.5") == "OK"

    assert cli._cmd_set("radio 900000000 250000 9 6").startswith("OK")
    assert cfg["radio"]["frequency"] == 900000000.0

    assert cli._cmd_set("freq 868000000").startswith("OK")
    assert cli._cmd_set("tx 17") == "OK"
    assert cli._cmd_set("guest.password gpw") == "OK"
    assert cli._cmd_set("allow.read.only off") == "OK"

    assert cli._cmd_set("advert.interval 59").startswith("Error: interval range")
    assert cli._cmd_set("advert.interval 60") == "OK"

    assert cli._cmd_set("flood.advert.interval 2").startswith("Error: interval range")
    assert cli._cmd_set("flood.advert.interval 168") == "OK"

    assert cli._cmd_set("flood.max 65") == "Error: max 64"
    assert cli._cmd_set("flood.max 64") == "OK"

    assert cli._cmd_set("rxdelay -1") == "Error: cannot be negative"
    assert cli._cmd_set("txdelay -1") == "Error: cannot be negative"
    assert cli._cmd_set("direct.txdelay -1") == "Error: cannot be negative"

    assert cli._cmd_set("multi.acks 5") == "OK"
    assert cli._cmd_set("int.thresh -120") == "OK"
    assert cli._cmd_set("agc.reset.interval 10") == "OK - interval rounded to 8"


def test_cli_set_command_error_paths():
    cfg = _base_config()
    save = MagicMock()
    cli = MeshCLI("/tmp/config.yaml", cfg, save)

    assert cli._cmd_set("af") == "Error: Missing value"
    assert cli._cmd_set("radio 1 2 3") == "Error: Expected freq bw sf cr"
    assert cli._cmd_set("unknown.key 1") == "unknown config: unknown.key"
    assert cli._cmd_set("tx not-int").startswith("Error: invalid value")

    cli.save_config = MagicMock(side_effect=RuntimeError("disk full"))
    assert cli._cmd_set("name x").startswith("Error:")


def test_cli_setperm_region_neighbor_tempradio_log_paths():
    cli = MeshCLI("/tmp/config.yaml", _base_config(), MagicMock(), enable_regions=False)

    assert cli._cmd_setperm("setperm") == "Err - bad params"
    assert cli._cmd_setperm("setperm deadbeef zz") == "Err - invalid permissions"
    assert cli._cmd_setperm("setperm deadbeef 2").startswith("Error:")

    assert "not available" in cli._route_command("region load us")

    cli_regions = MeshCLI("/tmp/config.yaml", _base_config(), MagicMock(), enable_regions=True)
    assert cli_regions._cmd_region("region").startswith("Error:")
    assert cli_regions._cmd_region("region load x").startswith("Error:")
    assert cli_regions._cmd_region("region save").startswith("Error:")
    assert cli_regions._cmd_region("region allowf").startswith("Error:")
    assert cli_regions._cmd_region("region what").startswith("Err -")

    assert cli._cmd_neighbors().startswith("Error:")
    assert cli._cmd_neighbor_remove("neighbor.remove   ") == "ERR: Missing pubkey"
    assert cli._cmd_neighbor_remove("neighbor.remove 001122").startswith("Error:")

    assert cli._cmd_tempradio("tempradio 1 2 3").startswith("Error:")
    assert cli._cmd_tempradio("tempradio 299 125 7 5 10") == "Error: invalid frequency"
    assert cli._cmd_tempradio("tempradio 915 6 7 5 10") == "Error: invalid bandwidth"
    assert cli._cmd_tempradio("tempradio 915 125 4 5 10") == "Error: invalid spreading factor"
    assert cli._cmd_tempradio("tempradio 915 125 7 9 10") == "Error: invalid coding rate"
    assert cli._cmd_tempradio("tempradio 915 125 7 5 0") == "Error: invalid timeout"
    assert cli._cmd_tempradio("tempradio 915 125 7 5 x") == "Error, invalid params"
    assert cli._cmd_tempradio("tempradio 915 125 7 5 10").startswith("Error:")

    assert cli._cmd_log("log start").startswith("Error:")
    assert cli._cmd_log("log stop").startswith("Error:")
    assert cli._cmd_log("log erase").startswith("Error:")
    assert cli._cmd_log("log") == "Error: Use journalctl to view logs"
    assert cli._cmd_log("log weird") == "Unknown log command"
