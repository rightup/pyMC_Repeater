"""rx_radio_id flows into policy_context for multi-radio rules."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openhop_core.protocol import Packet
from openhop_core.protocol.constants import PAYLOAD_TYPE_ADVERT
from openhop_core.rf_fabric import FabricRadio
from openhop_core.node.dispatcher import Dispatcher
from openhop_core.protocol.packet_filter import PacketFilter

from repeater.policy_engine import PolicyEngine


class _FakeRadio:
    def __init__(self, name="r"):
        self.name = name
        self.rx_callback = None
        self.sent = []

    def set_rx_callback(self, cb):
        self.rx_callback = cb

    async def send(self, data: bytes):
        self.sent.append(data)
        return {}

    def get_last_rssi(self):
        return -70

    def get_last_snr(self):
        return 4.0


def _advert_bytes():
    pkt = Packet()
    pkt.header = PAYLOAD_TYPE_ADVERT << 2
    pkt.payload = bytearray(b"rxid")
    pkt.payload_len = len(pkt.payload)
    pkt.path_len = 0
    return pkt.write_to()


@pytest.mark.asyncio
async def test_dispatcher_stamps_rx_radio_id_on_packet():
    a = _FakeRadio("a")
    b = _FakeRadio("b")
    fr = FabricRadio(radios=[(a, "local"), (b, "link")], default_radio_id="local")
    d = Dispatcher(radio=fr, packet_filter=PacketFilter())
    seen = []

    async def capture(pkt: Packet):
        seen.append(getattr(pkt, "_rx_radio_id", None))

    # Use raw path analysis via monkeypatch of _dispatch-ish: subscribe after parse
    # by wrapping _process through packet_analysis_callback which gets parsed pkt
    async def analysis(pkt, data):
        seen.append(getattr(pkt, "_rx_radio_id", None))

    d.packet_analysis_callback = analysis
    b.inject = None
    data = _advert_bytes()
    b.rx_callback(data, -80, 2.0)
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == ["link"]


def test_policy_matches_rx_radio_id():
    engine = PolicyEngine(
        {
            "enabled": True,
            "default_action": "allow",
            "rules": [
                {
                    "id": "drop-link-flood",
                    "if": {
                        "all": [
                            {"field": "rx_radio_id", "op": "eq", "value": "link"},
                        ]
                    },
                    "then": "drop",
                }
            ],
        }
    )
    pkt = SimpleNamespace(payload=b"x")
    d = engine.evaluate(pkt, {"rx_radio_id": "link", "mode": "forward"})
    assert d.action == "drop"
    assert d.matched is True

    d2 = engine.evaluate(pkt, {"rx_radio_id": "local", "mode": "forward"})
    assert d2.action == "allow"
    assert d2.matched is False
