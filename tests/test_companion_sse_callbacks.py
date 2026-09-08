"""The companion SSE stream must survive a companion app connecting over TCP.

A companion bridge is shared: the SSE endpoints and the TCP frame server both
subscribe to its push events. The frame server used to clear every callback on
each client connection, which silently unsubscribed the SSE stream for the rest
of the daemon's life.
"""

import pytest
from openhop_core.companion import CompanionBridge
from openhop_core.companion.frame_server import CompanionFrameServer
from openhop_core.protocol import LocalIdentity, Packet


class _Injector:
    async def __call__(self, pkt: Packet, **kwargs) -> bool:
        return True


def _endpoints(bridge):
    """A CompanionAPIEndpoints wired to one bridge, recording its broadcasts."""
    from repeater.web.companion_endpoints import CompanionAPIEndpoints

    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._sse_callbacks = []
    ep._get_bridge = lambda **kw: bridge
    ep.broadcasts = []
    ep._broadcast_sse = ep.broadcasts.append
    return ep


def _advert_subs(bridge):
    return bridge._push_callbacks["advert_received"]


@pytest.fixture
def bridge():
    return CompanionBridge(LocalIdentity(), _Injector())


@pytest.mark.asyncio
async def test_sse_survives_a_companion_client_connecting(bridge):
    """The regression: SSE went silent as soon as a MeshCore app connected."""
    ep = _endpoints(bridge)
    ep._ensure_callbacks()

    # A companion client connecting runs _setup_push_callbacks.
    CompanionFrameServer(bridge, "hash", port=0)._setup_push_callbacks()

    await bridge._fire_callbacks("advert_received", "contact")

    assert [b["event"] for b in ep.broadcasts] == ["advert_received"]


@pytest.mark.asyncio
async def test_sse_survives_repeated_client_reconnects(bridge):
    ep = _endpoints(bridge)
    ep._ensure_callbacks()
    server = CompanionFrameServer(bridge, "hash", port=0)

    for _ in range(3):
        server._setup_push_callbacks()

    await bridge._fire_callbacks("advert_received", "contact")

    assert len(ep.broadcasts) == 1


@pytest.mark.asyncio
async def test_repeated_stream_opens_do_not_multiply_events(bridge):
    """_ensure_callbacks runs per SSE client; re-asserting must stay a no-op."""
    ep = _endpoints(bridge)

    ep._ensure_callbacks()
    ep._ensure_callbacks()
    ep._ensure_callbacks()

    assert len(_advert_subs(bridge)) == 1
    await bridge._fire_callbacks("advert_received", "contact")
    assert len(ep.broadcasts) == 1


@pytest.mark.asyncio
async def test_legacy_event_registration_does_not_stack(bridge):
    """message_received goes through a legacy adapter closure, which is the one
    registration that identity-based dedupe cannot catch on its own."""
    ep = _endpoints(bridge)

    ep._ensure_callbacks()
    ep._ensure_callbacks()

    assert len(bridge._push_callbacks["message_event"]) == 1


@pytest.mark.asyncio
async def test_a_full_clear_is_repaired_on_the_next_stream_open(bridge):
    """clear_push_callbacks remains a legitimate full reset; the stream should
    recover on its own rather than stay dead until a restart."""
    ep = _endpoints(bridge)
    ep._ensure_callbacks()

    bridge.clear_push_callbacks()
    assert _advert_subs(bridge) == []

    ep._ensure_callbacks()

    await bridge._fire_callbacks("advert_received", "contact")
    assert len(ep.broadcasts) == 1


def test_no_bridge_yet_leaves_registration_pending():
    """Called before any companion is loaded, it must stay retryable."""
    import cherrypy

    from repeater.web.companion_endpoints import CompanionAPIEndpoints

    ep = CompanionAPIEndpoints.__new__(CompanionAPIEndpoints)
    ep._sse_callbacks = []

    def _no_bridge(**kwargs):
        raise cherrypy.HTTPError(503, "No companion bridges configured")

    ep._get_bridge = _no_bridge
    ep._ensure_callbacks()

    assert ep._sse_callbacks == []
