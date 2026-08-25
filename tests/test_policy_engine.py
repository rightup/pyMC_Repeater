from unittest.mock import patch

import yaml
from openhop_core.protocol.constants import PAYLOAD_TYPE_GRP_TXT
from openhop_core.protocol.identity import LocalIdentity
from openhop_core.protocol.packet_builder import PacketBuilder

from repeater.policy_engine import PolicyEngine


class _DummyPacket:
    def __init__(
        self,
        payload=b"\x01\x02",
        path_hashes=None,
        transport_codes=None,
        payload_type=None,
    ):
        self.payload = bytearray(payload)
        self.transport_codes = transport_codes or [0, 0]
        self._path_hashes = path_hashes or []
        self._payload_type = payload_type

    def get_path_hashes_hex(self):
        return self._path_hashes

    def get_payload_type(self):
        return self._payload_type


def test_policy_engine_disabled_allows():
    engine = PolicyEngine({"enabled": False, "rules": []})
    pkt = _DummyPacket()

    decision = engine.evaluate(pkt, {"hop_count": 3})

    assert decision.action == "allow"
    assert decision.matched is False


def test_policy_engine_first_match_wins_drop():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "rules": [
                {
                    "id": 10,
                    "enabled": True,
                    "if": {"all": [{"field": "hop_count", "op": "greater_than", "value": 2}]},
                    "then": {"action": "drop"},
                },
                {
                    "id": 20,
                    "enabled": True,
                    "if": {"all": [{"field": "hop_count", "op": "greater_than", "value": 1}]},
                    "then": {"action": "allow"},
                },
            ],
        }
    )
    pkt = _DummyPacket()

    decision = engine.evaluate(pkt, {"hop_count": 3})

    assert decision.action == "drop"
    assert decision.matched is True
    assert decision.rule_id == 10


def test_policy_engine_default_action_applies_when_no_match():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "drop",
            "rules": [
                {
                    "id": 1,
                    "enabled": True,
                    "if": {"all": [{"field": "route_type", "op": "equals", "value": 1}]},
                    "then": {"action": "allow"},
                }
            ],
        }
    )
    pkt = _DummyPacket()

    decision = engine.evaluate(pkt, {"route_type": 2})

    assert decision.action == "drop"
    assert decision.matched is False


def test_policy_engine_log_only_action_is_returned_when_rule_matches():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "rules": [
                {
                    "id": 11,
                    "enabled": True,
                    "if": {"all": [{"field": "route_type", "op": "equals", "value": 1}]},
                    "then": {"action": "log_only"},
                }
            ],
        }
    )
    pkt = _DummyPacket()

    decision = engine.evaluate(pkt, {"route_type": 1})

    assert decision.matched is True
    assert decision.action == "log_only"


def test_load_config_reads_sibling_policy_yaml(tmp_path):
    from repeater.config import load_config

    config_path = tmp_path / "config.yaml"
    policy_path = tmp_path / "policy.yaml"

    config_path.write_text(
        yaml.safe_dump(
            {
                "repeater": {
                    "node_name": "test",
                    "security": {
                        "max_clients": 1,
                        "admin_password": "a",
                        "guest_password": "g",
                        "allow_read_only": False,
                        "jwt_secret": "x",
                        "jwt_expiry_minutes": 60,
                    },
                },
                "radio": {"frequency": 869618000, "bandwidth": 62500},
            }
        ),
        encoding="utf-8",
    )

    policy_path.write_text(
        yaml.safe_dump(
            {
                "policy_engine": {
                    "enabled": True,
                    "default_action": "allow",
                    "rules": [
                        {
                            "id": 1,
                            "enabled": True,
                            "if": {
                                "all": [
                                    {
                                        "field": "hop_count",
                                        "op": "greater_than",
                                        "value": 4,
                                    }
                                ]
                            },
                            "then": {"action": "drop"},
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    with patch("repeater.config._load_or_create_identity_key", return_value=b"k" * 32):
        cfg = load_config(str(config_path))

    assert cfg["policy_engine"]["enabled"] is True
    assert len(cfg["policy_engine"]["rules"]) == 1
    assert cfg["policy_file_path"].endswith("policy.yaml")


def test_load_config_missing_policy_yaml_uses_safe_defaults(tmp_path):
    from repeater.config import load_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "repeater": {
                    "node_name": "test",
                    "security": {
                        "max_clients": 1,
                        "admin_password": "a",
                        "guest_password": "g",
                        "allow_read_only": False,
                        "jwt_secret": "x",
                        "jwt_expiry_minutes": 60,
                    },
                },
                "radio": {"frequency": 869618000, "bandwidth": 62500},
            }
        ),
        encoding="utf-8",
    )

    with patch("repeater.config._load_or_create_identity_key", return_value=b"k" * 32):
        cfg = load_config(str(config_path))

    assert cfg["policy_engine"]["enabled"] is False
    assert cfg["policy_engine"]["rules"] == []


def test_policy_engine_path_hash_intersects_group_object():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {
                "channel_hash_groups": {
                    "ops_channels": ["0x42", "0xAA"],
                }
            },
            "rules": [
                {
                    "id": 101,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "path_hashes",
                                "op": "intersects",
                                "value": "@channel_hash_groups.ops_channels",
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )
    pkt = _DummyPacket(path_hashes=["0x10", "0x42"])

    decision = engine.evaluate(pkt, {"hop_count": 2})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_in_operator_for_scalar_vs_group_list():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {"allow_modes": {"prod_modes": ["forward", "monitor"]}},
            "rules": [
                {
                    "id": 202,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "mode",
                                "op": "in",
                                "value": "@allow_modes.prod_modes",
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )
    pkt = _DummyPacket()

    decision = engine.evaluate(pkt, {"mode": "forward"})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_matches_decrypted_channel_message_body_from_policy_objects():
    channel_secret = (b"policy-channel-secret" + b"\x00" * 32)[:32].hex()
    packet = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="hello mesh from channel",
        sender_name="Alice",
        channels_config=[{"name": "ops", "secret": channel_secret}],
    )

    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {
                "channels": {
                    "ops": {
                        "secret": channel_secret,
                    }
                }
            },
            "rules": [
                {
                    "id": 303,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_message_body",
                                "op": "contains",
                                "value": "hello mesh",
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )

    decision = engine.evaluate(packet, {"payload_type": packet.get_payload_type()})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_matches_decrypted_channel_sender_from_policy_objects():
    channel_secret = (b"policy-channel-secret" + b"\x00" * 32)[:32].hex()
    packet = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="hello mesh from channel",
        sender_name="Alice",
        channels_config=[{"name": "ops", "secret": channel_secret}],
    )

    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {
                "channels": {
                    "ops": {
                        "secret": channel_secret,
                    }
                }
            },
            "rules": [
                {
                    "id": 304,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_sender",
                                "op": "equals",
                                "value": "Alice",
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )

    decision = engine.evaluate(packet, {"payload_type": packet.get_payload_type()})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_path_hashes_intersects_normalized_literal_list():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "rules": [
                {
                    "id": 404,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "path_hashes",
                                "op": "intersects",
                                "value": ["0x002A", "00AA"],
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )
    pkt = _DummyPacket(path_hashes=["002A", "00AA"])

    decision = engine.evaluate(pkt, {"hop_count": 2})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_path_hashes_do_not_match_different_byte_lengths():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "rules": [
                {
                    "id": 405,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "path_hashes",
                                "op": "contains",
                                "value": "0x42",
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )
    pkt = _DummyPacket(path_hashes=["0042"])

    decision = engine.evaluate(pkt, {"hop_count": 2})

    assert decision.matched is False
    assert decision.action == "allow"


def test_policy_engine_path_hashes_reject_mixed_literal_byte_lengths():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "rules": [
                {
                    "id": 406,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "path_hashes",
                                "op": "intersects",
                                "value": ["42", "0042"],
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )
    pkt = _DummyPacket(path_hashes=["42"])

    decision = engine.evaluate(pkt, {"hop_count": 2})

    assert decision.matched is False
    assert decision.action == "allow"


def test_policy_engine_channel_hash_in_group_object():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {
                "channel_hash_groups": {
                    "ops_channels": ["0x42", "0x99"],
                }
            },
            "rules": [
                {
                    "id": 407,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_hash",
                                "op": "in",
                                "value": "@channel_hash_groups.ops_channels",
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )
    pkt = _DummyPacket(payload=b"\x42\xaa\xbb\xcc", payload_type=PAYLOAD_TYPE_GRP_TXT)

    decision = engine.evaluate(pkt, {"hop_count": 2})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_channel_hash_rejects_oversized_literal():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "rules": [
                {
                    "id": 408,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_hash",
                                "op": "equals",
                                "value": "0x8B3387E9C5CDE8000000000000000000",
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )
    pkt = _DummyPacket(payload=b"\x11\xaa\xbb\xcc", payload_type=PAYLOAD_TYPE_GRP_TXT)

    decision = engine.evaluate(pkt, {"hop_count": 2})

    assert decision.matched is False
    assert decision.action == "allow"


def test_policy_engine_channel_hash_accepts_full_secret_literal():
    public_secret = "8b3387e9c5cdea6ac9e5edbaa115cd72"
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "rules": [
                {
                    "id": 409,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_hash",
                                "op": "equals",
                                "value": public_secret,
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )
    # Derived hash for the secret above is 0x11.
    pkt = _DummyPacket(payload=b"\x11\xaa\xbb\xcc", payload_type=PAYLOAD_TYPE_GRP_TXT)

    decision = engine.evaluate(pkt, {"hop_count": 2})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_channel_decryptable_true_with_matching_secret():
    channel_secret = (b"policy-channel-secret" + b"\x00" * 32)[:32].hex()
    packet = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="decryptable test",
        sender_name="Alice",
        channels_config=[{"name": "ops", "secret": channel_secret}],
    )

    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {
                "channels": {
                    "ops": {
                        "secret": channel_secret,
                    }
                }
            },
            "rules": [
                {
                    "id": 410,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_decryptable",
                                "op": "equals",
                                "value": True,
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )

    decision = engine.evaluate(packet, {"payload_type": packet.get_payload_type()})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_channel_decryptable_false_with_non_matching_secret():
    packet_secret = (b"packet-channel-secret" + b"\x00" * 32)[:32].hex()
    wrong_secret = (b"wrong-channel-secret" + b"\x00" * 32)[:32].hex()
    packet = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="undecryptable test",
        sender_name="Alice",
        channels_config=[{"name": "ops", "secret": packet_secret}],
    )

    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {
                "channels": {
                    "ops": {
                        "secret": wrong_secret,
                    }
                }
            },
            "rules": [
                {
                    "id": 411,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_decryptable",
                                "op": "equals",
                                "value": False,
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )

    decision = engine.evaluate(packet, {"payload_type": packet.get_payload_type()})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_channel_decryptable_accepts_channels_list_with_psk():
    channel_secret = (b"policy-channel-secret" + b"\x00" * 32)[:32].hex()
    packet = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="list schema decryptable test",
        sender_name="Alice",
        channels_config=[{"name": "ops", "secret": channel_secret}],
    )

    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {
                "channels": [
                    {
                        "name": "ops",
                        "psk": channel_secret,
                    }
                ]
            },
            "rules": [
                {
                    "id": 412,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_decryptable",
                                "op": "equals",
                                "value": True,
                            }
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )

    decision = engine.evaluate(packet, {"payload_type": packet.get_payload_type()})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_channel_decryptable_uses_inline_channel_hash_secret_literal():
    channel_secret = (b"policy-channel-secret" + b"\x00" * 32)[:32].hex()
    packet = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="inline secret decryptable test",
        sender_name="Alice",
        channels_config=[{"name": "ops", "secret": channel_secret}],
    )

    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {"channels": {}},
            "rules": [
                {
                    "id": 413,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_hash",
                                "op": "equals",
                                "value": channel_secret,
                            },
                            {
                                "field": "channel_decryptable",
                                "op": "equals",
                                "value": True,
                            },
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )

    decision = engine.evaluate(packet, {"payload_type": packet.get_payload_type()})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_channel_decryptable_uses_channel_hash_group_secret():
    channel_secret = (b"policy-channel-secret" + b"\x00" * 32)[:32].hex()
    packet = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="group secret decryptable test",
        sender_name="Alice",
        channels_config=[{"name": "ops", "secret": channel_secret}],
    )

    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {
                "channels": {},
                "channel_hash_groups": {
                    "ops_channels": [channel_secret],
                },
            },
            "rules": [
                {
                    "id": 414,
                    "enabled": True,
                    "if": {
                        "all": [
                            {
                                "field": "channel_hash",
                                "op": "in",
                                "value": "@channel_hash_groups.ops_channels",
                            },
                            {
                                "field": "channel_decryptable",
                                "op": "equals",
                                "value": True,
                            },
                        ]
                    },
                    "then": {"action": "drop"},
                }
            ],
        }
    )

    decision = engine.evaluate(packet, {"payload_type": packet.get_payload_type()})

    assert decision.matched is True
    assert decision.action == "drop"


def test_policy_engine_channel_decrypt_cache_not_keyed_by_id():
    channel_secret = (b"cache-secret" + b"\x00" * 32)[:32].hex()
    other_secret = (b"cache-other" + b"\x00" * 32)[:32].hex()
    decryptable = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="hello from cache test",
        sender_name="Alice",
        channels_config=[{"name": "ops", "secret": channel_secret}],
    )
    undecryptable = PacketBuilder.create_group_datagram(
        group_name="ops",
        local_identity=LocalIdentity(),
        message="different payload",
        sender_name="Eve",
        channels_config=[{"name": "ops", "secret": other_secret}],
    )

    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "objects": {"channels": {"ops": {"secret": channel_secret}}},
            "rules": [],
        }
    )

    # Python reuses id() once old Packet objects are freed, so an id()-keyed
    # cache can serve a stale undecryptable entry for an unrelated later
    # packet. Force both packets to report the same id() to expose that the
    # cache must actually be keyed by content.
    with patch("repeater.policy_engine.id", return_value=424242):
        engine._get_channel_decrypt_info(undecryptable)
        info = engine._get_channel_decrypt_info(decryptable)

    assert info["decryptable"] is True
    assert info["sender"] == "Alice"
