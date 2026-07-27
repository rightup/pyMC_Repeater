from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from repeater.handler_helpers.mesh_cli import MeshCLI


def _base_config():
    return {
        "version": "3.2.1",
        "repeater": {
            "name": "node-a",
            "mode": "forward",
            "owner_info": "owner-a",
            "latitude": 1.2,
            "longitude": 3.4,
            "airtime_factor": 1.1,
            "advert_interval_minutes": 120,
            "flood_advert_interval_hours": 24,
            "max_flood_hops": 20,
            "rx_delay_base": 0.2,
            "tx_delay_factor": 1.3,
            "direct_tx_delay_factor": 0.6,
            "multi_acks": 2,
            "interference_threshold": -115,
            "agc_reset_interval": 8,
        },
        "radio": {
            "frequency": 915000000,
            "bandwidth": 125000,
            "spreading_factor": 7,
            "coding_rate": 5,
            "tx_power": 22,
        },
        "mesh": {"path_hash_mode": 0, "loop_detect": "minimal"},
        "security": {"guest_password": "guest", "allow_read_only": True},
    }


def _cfg_mgr(save_ok=True, err=None):
    return SimpleNamespace(
        save_to_file=MagicMock(return_value=(save_ok, err)),
        live_update_daemon=MagicMock(),
    )


def test_handle_command_admin_and_prefix_behavior():
    cli = MeshCLI("/tmp/cfg.yaml", _base_config(), _cfg_mgr())

    assert cli.handle_command(b"a", "help", is_admin=False) == "Error: Admin permission required"
    assert cli.handle_command(b"a", "12|help set", is_admin=True).startswith("12|")


def test_help_routing_and_basic_unknown_paths():
    cli = MeshCLI("/tmp/cfg.yaml", _base_config(), _cfg_mgr(), enable_regions=False)

    assert "openHop CLI Commands" in cli._route_command("help")
    assert "No detailed help" in cli._route_command("help nope")
    assert cli._route_command("start ota").startswith("Error:")
    assert cli._route_command("sensor read").startswith("Error:")
    assert cli._route_command("gps on").startswith("Error:")
    assert cli._route_command("stats-foo").startswith("Error:")
    assert cli._route_command("discover.neighbors").startswith("Error:")
    assert cli._route_command("region load x").startswith("Error: Region commands not available")
    assert cli._route_command("unknown") == "Unknown command"


def test_cmd_advert_branches_and_success_schedule():
    cli = MeshCLI("/tmp/cfg.yaml", _base_config(), _cfg_mgr(), send_advert_callback=MagicMock())

    # No callback configured.
    cli_no_cb = MeshCLI("/tmp/cfg.yaml", _base_config(), _cfg_mgr(), send_advert_callback=None)
    assert cli_no_cb._cmd_advert().startswith("Error: Advert functionality")

    # Callback present but no event loop.
    cli._event_loop = None
    assert cli._cmd_advert() == "Error: Event loop not available"

    # Event loop available/running and schedule succeeds.
    fake_loop = SimpleNamespace(is_running=lambda: True)
    cli._event_loop = fake_loop

    with patch(
        "asyncio.run_coroutine_threadsafe", side_effect=lambda coro, _loop: coro.close()
    ) as run_ts:
        out = cli._cmd_advert()

    assert out == "OK - Advert sent"
    run_ts.assert_called_once()


def test_cmd_password_save_success_failure_and_exception():
    cfg = _base_config()
    ok_mgr = _cfg_mgr(save_ok=True)
    cli_ok = MeshCLI("/tmp/cfg.yaml", cfg, ok_mgr)

    assert cli_ok._cmd_password("password   ") == "Error: Password cannot be empty"
    assert cli_ok._cmd_password("password newpw") == "password now: newpw"
    ok_mgr.live_update_daemon.assert_called_once_with(["security"])

    bad_mgr = _cfg_mgr(save_ok=False, err="disk")
    cli_bad = MeshCLI("/tmp/cfg.yaml", _base_config(), bad_mgr)
    assert "Failed to save config" in cli_bad._cmd_password("password x")

    ex_mgr = SimpleNamespace(
        save_to_file=MagicMock(side_effect=RuntimeError("boom")),
        live_update_daemon=MagicMock(),
    )
    cli_ex = MeshCLI("/tmp/cfg.yaml", _base_config(), ex_mgr)
    assert cli_ex._cmd_password("password x") == "Error: Failed to save password"


def test_cmd_get_public_key_and_neighbor_branches():
    cli = MeshCLI("/tmp/cfg.yaml", _base_config(), _cfg_mgr())

    assert cli._cmd_get("public.key") == "Error: Identity not available"

    cli.identity = SimpleNamespace(get_public_key=lambda: b"\x01" * 32)
    assert cli._cmd_get("public.key") == "> " + (b"\x01" * 32).hex()

    cli.identity = SimpleNamespace(get_public_key=MagicMock(side_effect=RuntimeError("bad")))
    assert cli._cmd_get("public.key").startswith("Error:")

    # neighbors: no storage
    assert cli._cmd_neighbors() == "Error: Storage not available"

    # neighbors: empty, filtered empty, then formatted output
    storage = SimpleNamespace(get_neighbors=lambda: {})
    cli.storage_handler = storage
    assert cli._cmd_neighbors() == "No neighbors discovered yet"

    storage.get_neighbors = lambda: {
        "aa": {"is_repeater": False, "zero_hop": False, "last_seen": 1}
    }
    assert "No zero hop repeaters" in cli._cmd_neighbors()

    storage.get_neighbors = lambda: {
        "abcdef12feed": {"is_repeater": True, "zero_hop": False, "last_seen": 10, "snr": 4.9},
        "11223344aabb": {"is_repeater": False, "zero_hop": True, "last_seen": 20, "snr": 1.2},
        "99aabbccddeeff00": {
            "is_repeater": True,
            "zero_hop": True,
            "last_seen": 21,
            "snr": 6.3,
        },
    }
    with patch("time.time", return_value=30):
        out = cli._cmd_neighbors()

    assert "99aabbcc:9:6" in out
    assert "abcdef12:20:4" not in out
    assert "11223344:10:1" not in out

    cli.storage_handler = SimpleNamespace(
        get_neighbors=MagicMock(side_effect=RuntimeError("db fail"))
    )
    assert cli._cmd_neighbors().startswith("Error:")


def test_cmd_get_owner_info_path_hash_mode_and_loop_detect():
    cli = MeshCLI("/tmp/cfg.yaml", _base_config(), _cfg_mgr())

    assert cli._cmd_get("owner.info") == "> owner-a"
    assert cli._cmd_get("path.hash.mode") == "> 0"
    assert cli._cmd_get("loop.detect") == "> minimal"


def test_cmd_set_updates_and_validation_errors():
    cfg = _base_config()
    mgr = _cfg_mgr()
    cli = MeshCLI("/tmp/cfg.yaml", cfg, mgr)

    assert cli._cmd_set("af 2.5") == "OK"
    assert cfg["repeater"]["airtime_factor"] == 2.5

    assert cli._cmd_set("name node-z") == "OK"
    assert cfg["repeater"]["node_name"] == "node-z"

    assert cli._cmd_set("repeat off").endswith("OFF")
    assert cfg["repeater"]["mode"] == "monitor"

    assert cli._cmd_set("radio 900000000 250000 9 6").startswith("OK")
    assert cfg["radio"]["frequency"] == 900000000.0

    assert cli._cmd_set("freq 868000000").startswith("OK")
    assert cli._cmd_set("tx 17") == "OK"
    assert cli._cmd_set("guest.password g") == "OK"
    assert cli._cmd_set("owner.info Alice|Ops") == "OK"
    assert cfg["repeater"]["owner_info"] == "Alice\nOps"
    assert cli._cmd_set("allow.read.only off") == "OK"
    assert cli._cmd_set("path.hash.mode 2") == "OK"
    assert cfg["mesh"]["path_hash_mode"] == 2
    assert cli._cmd_set("path.hash.mode 3") == "Error: path.hash.mode must be 0, 1, or 2"
    assert cli._cmd_set("loop.detect strict") == "OK"
    assert cfg["mesh"]["loop_detect"] == "strict"
    assert (
        cli._cmd_set("loop.detect weird")
        == "Error: loop.detect must be off, minimal, moderate, or strict"
    )

    assert cli._cmd_set("advert.interval 59").startswith("Error: interval range")
    assert cli._cmd_set("flood.advert.interval 2").startswith("Error: interval range")
    assert cli._cmd_set("flood.max 100") == "Error: max 64"
    assert cli._cmd_set("rxdelay -1") == "Error: cannot be negative"
    assert cli._cmd_set("txdelay -1") == "Error: cannot be negative"
    assert cli._cmd_set("direct.txdelay -1") == "Error: cannot be negative"

    assert cli._cmd_set("agc.reset.interval 10") == "OK - interval rounded to 8"
    assert cli._cmd_set("bad") == "Error: Missing value"
    assert cli._cmd_set("tx nope").startswith("Error: invalid value")
    assert cli._cmd_set("unknown.key 1") == "unknown config: unknown.key"


def test_misc_commands_and_routes():
    cli = MeshCLI("/tmp/cfg.yaml", _base_config(), _cfg_mgr(), enable_regions=True)

    cli.storage_handler = SimpleNamespace(get_transport_keys=lambda: [])

    assert cli._cmd_region("region") == "* F"
    assert cli._cmd_region("region load us").startswith("Err -")
    assert cli._cmd_region("region save") == "OK"
    assert cli._cmd_region("region remove x") == "Err - not found"
    assert cli._cmd_region("region unknown").startswith("Err -")

    assert cli._cmd_setperm("setperm") == "Err - bad params"
    assert cli._cmd_setperm("setperm abc zz") == "Err - invalid permissions"
    assert cli._cmd_setperm("setperm abc 2").startswith("Error:")

    assert cli._cmd_tempradio("tempradio 1 2 3").startswith("Error: Expected")
    assert cli._cmd_tempradio("tempradio 299 125 7 5 10") == "Error: invalid frequency"
    assert cli._cmd_tempradio("tempradio 915 6 7 5 10") == "Error: invalid bandwidth"
    assert cli._cmd_tempradio("tempradio 915 125 4 5 10") == "Error: invalid spreading factor"
    assert cli._cmd_tempradio("tempradio 915 125 7 9 10") == "Error: invalid coding rate"
    assert cli._cmd_tempradio("tempradio 915 125 7 5 0") == "Error: invalid timeout"
    assert cli._cmd_tempradio("tempradio 915 125 7 5 nope") == "Error, invalid params"
    assert cli._cmd_tempradio("tempradio 915 125 7 5 10").startswith("Error:")

    assert cli._cmd_neighbor_remove("neighbor.remove") == "ERR: Missing pubkey"

    storage = SimpleNamespace(delete_neighbors_by_pubkey_prefix=MagicMock(return_value=1))
    cli.storage_handler = storage
    assert cli._cmd_neighbor_remove("neighbor.remove abc") == "OK"
    storage.delete_neighbors_by_pubkey_prefix.assert_called_with("abc")

    assert cli._cmd_neighbor_remove("neighbor.remove    ") == "OK"
    storage.delete_neighbors_by_pubkey_prefix.assert_called_with(None)

    assert cli._cmd_neighbor_remove("neighbor.remove zz") == "ERR: bad pubkey"

    no_delete = SimpleNamespace()
    cli.storage_handler = no_delete
    assert "not supported" in cli._cmd_neighbor_remove("neighbor.remove ab")

    cli.storage_handler = None
    assert cli._cmd_neighbor_remove("neighbor.remove ab") == "Error: Storage not available"

    discovery = SimpleNamespace(
        cleanup_sessions=MagicMock(),
        create_session=MagicMock(return_value={"session_id": "s1"}),
        start_session_task=MagicMock(),
    )
    loop = SimpleNamespace(is_running=lambda: True, call_soon_threadsafe=MagicMock())
    cli._event_loop = loop
    cli.config_manager.daemon = SimpleNamespace(discovery_helper=discovery)

    assert cli._cmd_discover_neighbors("discover.neighbors") == "OK - Discover sent"
    discovery.cleanup_sessions.assert_called_once()
    assert callable(discovery.create_session.call_args.kwargs["result_enricher"])
    loop.call_soon_threadsafe.assert_called_once_with(discovery.start_session_task, "s1")

    assert cli._cmd_discover_neighbors("discover.neighbors foo").startswith("Err -")

    cli.config_manager.daemon = SimpleNamespace(discovery_helper=None)
    assert cli._cmd_discover_neighbors("discover.neighbors").startswith("Error: Discovery helper")

    assert cli._cmd_log("log start").startswith("Error:")
    assert cli._cmd_log("log stop").startswith("Error:")
    assert cli._cmd_log("log erase").startswith("Error:")
    assert cli._cmd_log("log") == "Error: Use journalctl to view logs"
    assert cli._cmd_log("log whatever") == "Unknown log command"


def test_region_command_response_shapes_and_ops():
    keys = [
        {
            "id": 1,
            "name": "alpha",
            "flood_policy": "allow",
            "parent_id": None,
        },
        {
            "id": 2,
            "name": "#beta",
            "flood_policy": "deny",
            "parent_id": 1,
        },
    ]

    created = []
    updated = []
    deleted = []

    def get_transport_keys():
        return list(keys)

    def update_transport_key(key_id, **kwargs):
        updated.append((key_id, kwargs))
        for rec in keys:
            if rec["id"] == key_id:
                rec.update(kwargs)
                return True
        return False

    def create_transport_key(name, flood_policy, transport_key, parent_id, last_used):
        new_id = max([r["id"] for r in keys] + [0]) + 1
        keys.append(
            {
                "id": new_id,
                "name": name,
                "flood_policy": flood_policy,
                "parent_id": parent_id,
            }
        )
        created.append((name, flood_policy, parent_id))
        return new_id

    def delete_transport_key(key_id):
        for idx, rec in enumerate(keys):
            if rec["id"] == key_id:
                deleted.append(key_id)
                del keys[idx]
                return True
        return False

    cfg = _base_config()
    cfg["mesh"] = {"unscoped_flood_allow": True, "default_region": None}
    cli = MeshCLI("/tmp/cfg.yaml", cfg, _cfg_mgr(), enable_regions=True)
    cli.storage_handler = SimpleNamespace(
        get_transport_keys=get_transport_keys,
        update_transport_key=update_transport_key,
        create_transport_key=create_transport_key,
        delete_transport_key=delete_transport_key,
    )

    tree = cli._cmd_region("region")
    assert "* F" in tree
    assert " alpha F" in tree
    assert "  beta" in tree

    assert cli._cmd_region("region get alpha") == " alpha F"
    assert cli._cmd_region("region get beta") == " beta (alpha)"
    assert cli._cmd_region("region get missing") == "Err - unknown region"

    assert cli._cmd_region("region allowf beta") == "OK"
    assert keys[1]["flood_policy"] == "allow"
    assert cli._cmd_region("region denyf beta") == "OK"
    assert keys[1]["flood_policy"] == "deny"

    assert cli._cmd_region("region home alpha") == " home is now alpha"
    assert cli._cmd_region("region home") == " home is alpha"

    assert cli._cmd_region("region default") == " default scope is <null>"
    assert cli._cmd_region("region default alpha") == " default scope is now alpha"
    assert cfg["mesh"]["default_region"] == "alpha"
    assert cli._cmd_region("region default") == " default scope is alpha"
    assert cli._cmd_region("region default <null>") == " default scope is now <null>"
    assert cfg["mesh"]["default_region"] is None

    assert cli._cmd_region("region put gamma alpha") == "OK - (flood allowed)"
    assert any(r[0] == "gamma" for r in created)

    assert cli._cmd_region("region remove alpha") == "Err - not empty"
    assert cli._cmd_region("region remove gamma") == "OK"
    assert deleted

    assert cli._cmd_region("region list denied") == "beta"
    listed = cli._cmd_region("region list allowed")
    assert listed.startswith("*,")
    assert "alpha" in listed
    assert cli._cmd_region("region list nope") == "Err - use 'allowed' or 'denied'"


def test_discovery_auto_add_skips_local_node_and_persists_remote():
    identity = SimpleNamespace(get_public_key=lambda: bytes.fromhex("11" * 32))
    storage = SimpleNamespace(record_advert=MagicMock())
    cli = MeshCLI(
        "/tmp/cfg.yaml", _base_config(), _cfg_mgr(), identity=identity, storage_handler=storage
    )

    self_result = cli._auto_add_discovery_result({"pub_key": "11" * 8, "node_type": 2})
    assert self_result["is_self"] is True
    storage.record_advert.assert_not_called()

    remote_result = cli._auto_add_discovery_result(
        {
            "pub_key": "22" * 32,
            "node_name": "Remote Repeater",
            "node_type": 2,
            "rssi": -70,
            "response_snr": 4.25,
        }
    )
    assert remote_result["auto_added"] is True
    storage.record_advert.assert_called_once()
