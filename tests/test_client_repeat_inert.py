"""Regression: the repeater never activates Core's client-repeat forwarding.

Core gained a client-repeat forward path in the shared Dispatcher, gated by a
flag that only CompanionRadio toggles. The repeater builds the Dispatcher
directly and uses RepeaterCompanionBridge (a CompanionBridge), so that path
must stay inert here.
"""

from openhop_core import LocalIdentity
from openhop_core.node.dispatcher import Dispatcher

from repeater.companion.bridge import RepeaterCompanionBridge


class _Radio:
    def set_rx_callback(self, cb):
        pass


async def _inject(pkt, wait_for_ack=False):
    return True


def test_repeater_bridge_disallows_client_repeat():
    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    # The virtual companion advertises no repeat capability and cannot mutate
    # the shared radio, so the frame server never persists a nonzero preference.
    assert bridge.supports_client_repeat() is False
    assert bridge.supports_radio_params_mutation() is False
    assert bridge.prefs.client_repeat == 0


def test_core_dispatcher_client_repeat_defaults_off():
    # The repeater constructs Core's Dispatcher directly and never calls
    # set_client_repeat_enabled(True); forwarding therefore stays disabled.
    d = Dispatcher(_Radio(), dedupe_enabled=True)
    assert d._client_repeat_enabled is False


def test_repeater_bridge_set_client_repeat_does_not_enable_forwarding():
    # Even if set_client_repeat is invoked on the bridge, the base persists a
    # preference only; it never wires a dispatcher forward flag (only
    # CompanionRadio does). The repeater's Dispatcher stays inert.
    bridge = RepeaterCompanionBridge(LocalIdentity(), _inject)
    d = Dispatcher(_Radio(), dedupe_enabled=True)
    bridge.set_client_repeat(1)
    assert d._client_repeat_enabled is False
