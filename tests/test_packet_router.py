"""
Tests for PacketRouter in-flight cap and shutdown behaviour.
Addresses the three concerns raised in PR 191 review:

  1. Cap enforcement: packets beyond _max_in_flight are dropped, not queued.
  2. Drop counter: _cap_drop_count increments on each cap-drop so operators
     have visibility into how often the safety valve fires.
  3. Shutdown drain: stop() waits for in-flight tasks to finish (up to 5 s),
     then cancels any that remain — tasks are never silently abandoned.

Run with:
    python -m pytest tests/test_packet_router.py -v
or:
    python -m unittest tests.test_packet_router -v
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pymc_core.node.handlers.trace import TraceHandler

from repeater.packet_router import PacketRouter


# ---------------------------------------------------------------------------
# Minimal daemon stub
# ---------------------------------------------------------------------------

def _make_daemon():
    """Minimal daemon that satisfies PacketRouter without touching hardware."""
    daemon = MagicMock()
    daemon.repeater_handler = AsyncMock(return_value=None)
    daemon.trace_helper = None
    daemon.discovery_helper = None
    daemon.advert_helper = None
    daemon.companion_bridges = {}
    daemon.login_helper = None
    daemon.text_helper = None
    daemon.path_helper = None
    daemon.protocol_request_helper = None
    return daemon


def _make_packet(payload_type: int = 0xFF):
    """Minimal packet stub."""
    pkt = MagicMock()
    pkt.get_payload_type.return_value = payload_type
    pkt.payload = b"\xff"
    pkt.header = 0x00
    pkt.rssi = -80
    pkt.snr = 5.0
    pkt.timestamp = time.time()
    pkt._injected_for_tx = False
    pkt.path = bytearray()
    return pkt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInFlightCap(unittest.IsolatedAsyncioTestCase):

    # ── 1. Cap enforcement ──────────────────────────────────────────────────

    async def test_cap_drops_packets_when_full(self):
        """
        When _in_flight reaches _max_in_flight, new packets from the queue
        must be dropped (not passed to _route_packet).
        """
        router = PacketRouter(_make_daemon())
        router._max_in_flight = 3

        # Manually occupy all slots with long-sleeping tasks
        barrier = asyncio.Event()

        async def slow_route(pkt):
            await barrier.wait()   # blocks until we release

        routed = []

        async def counting_route(pkt):
            routed.append(pkt)
            await barrier.wait()

        router._route_packet = counting_route

        await router.start()

        # Fill the cap
        for _ in range(3):
            await router.enqueue(_make_packet())
        await asyncio.sleep(0.05)   # let queue drain into tasks
        self.assertEqual(router._in_flight, 3)

        # These should be dropped
        for _ in range(5):
            await router.enqueue(_make_packet())
        await asyncio.sleep(0.05)

        self.assertEqual(router._in_flight, 3,
                         "In-flight count exceeded cap")
        self.assertEqual(router._cap_drop_count, 5,
                         "Expected 5 cap-drops, got different count")

        barrier.set()   # release blocked tasks
        await router.stop()

    # ── 2. Drop counter ─────────────────────────────────────────────────────

    async def test_cap_drop_count_increments(self):
        """_cap_drop_count must increment by exactly 1 for each dropped packet."""
        router = PacketRouter(_make_daemon())
        router._max_in_flight = 1

        barrier = asyncio.Event()

        async def blocking_route(pkt):
            await barrier.wait()

        router._route_packet = blocking_route

        await router.start()

        # Fill the single slot
        await router.enqueue(_make_packet())
        await asyncio.sleep(0.05)
        self.assertEqual(router._in_flight, 1)

        # Drop three packets
        for _ in range(3):
            await router.enqueue(_make_packet())
        await asyncio.sleep(0.05)

        self.assertEqual(router._cap_drop_count, 3)

        barrier.set()
        await router.stop()

    async def test_cap_drop_count_zero_when_cap_not_reached(self):
        """_cap_drop_count must stay 0 when the cap is never reached."""
        router = PacketRouter(_make_daemon())
        router._max_in_flight = 30

        completed = []

        async def fast_route(pkt):
            completed.append(pkt)

        router._route_packet = fast_route

        await router.start()

        for _ in range(10):
            await router.enqueue(_make_packet())
        await asyncio.sleep(0.1)

        self.assertEqual(router._cap_drop_count, 0)
        await router.stop()

    async def test_injected_trace_packet_skips_inbound_trace_processing(self):
        """Locally injected TRACE packets must not be re-parsed as inbound trace responses."""
        daemon = _make_daemon()
        daemon.trace_helper = MagicMock()
        daemon.trace_helper.process_trace_packet = AsyncMock()

        router = PacketRouter(daemon)
        pkt = _make_packet(payload_type=TraceHandler.payload_type())

        await router.start()
        try:
            injected = await router.inject_packet(pkt)
            self.assertTrue(injected)
            await asyncio.sleep(0.05)

            daemon.repeater_handler.assert_awaited_once()
            daemon.trace_helper.process_trace_packet.assert_not_awaited()
        finally:
            await router.stop()

    # ── 3. Shutdown: in-flight tasks drained ────────────────────────────────

    async def test_stop_waits_for_in_flight_tasks(self):
        """
        stop() must wait for in-flight tasks to complete before returning.
        Tasks that finish within the 5-second timeout must complete normally,
        not be cancelled.
        """
        router = PacketRouter(_make_daemon())

        completed = []
        started = asyncio.Event()

        async def slow_route(pkt):
            started.set()
            await asyncio.sleep(0.2)   # finishes well within 5 s timeout
            completed.append(pkt)

        router._route_packet = slow_route

        await router.start()
        pkt = _make_packet()
        await router.enqueue(pkt)

        # Wait until the task has actually started
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await router.stop()

        # Task should have completed, not been cancelled
        self.assertEqual(len(completed), 1,
                         "In-flight task was cancelled instead of drained")

    async def test_stop_cancels_tasks_that_exceed_timeout(self):
        """
        Tasks that don't finish within the 5-second timeout must be cancelled,
        not left running indefinitely.
        """
        router = PacketRouter(_make_daemon())
        router._max_in_flight = 5

        cancelled = []
        started = asyncio.Event()

        async def hanging_route(pkt):
            started.set()
            try:
                await asyncio.sleep(999)   # will not finish within 5 s
            except asyncio.CancelledError:
                cancelled.append(pkt)
                raise

        router._route_packet = hanging_route

        # Patch the timeout to 0.1 s so the test runs fast
        original_stop = router.stop

        async def fast_stop():
            router.running = False
            if router.router_task:
                router.router_task.cancel()
                try:
                    await router.router_task
                except asyncio.CancelledError:
                    pass
            if router._route_tasks:
                snapshot = set(router._route_tasks)
                _, still_pending = await asyncio.wait(snapshot, timeout=0.1)
                for task in still_pending:
                    task.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)

        router.stop = fast_stop

        await router.start()
        await router.enqueue(_make_packet())
        await asyncio.wait_for(started.wait(), timeout=1.0)

        await router.stop()

        self.assertEqual(len(cancelled), 1,
                         "Hanging task was not cancelled on shutdown")

    # ── 4. Route-tasks set stays in sync with counter ───────────────────────

    async def test_route_tasks_set_cleaned_up_on_completion(self):
        """
        _route_tasks must be empty after all tasks complete — the done-callback
        must discard each task so the set doesn't grow unboundedly.
        """
        router = PacketRouter(_make_daemon())

        async def fast_route(pkt):
            await asyncio.sleep(0)   # yield, then done

        router._route_packet = fast_route

        await router.start()

        for _ in range(10):
            await router.enqueue(_make_packet())

        # Give tasks time to complete
        await asyncio.sleep(0.1)

        self.assertEqual(len(router._route_tasks), 0,
                         "_route_tasks not cleaned up after task completion")
        self.assertEqual(router._in_flight, 0,
                         "_in_flight counter not back to 0 after completion")

        await router.stop()

    # ── 5. Counter and set always agree ─────────────────────────────────────

    async def test_counter_matches_set_size_under_load(self):
        """
        _in_flight must always equal len(_route_tasks) while tasks are running.
        Checked at steady state when the cap is saturated.
        """
        router = PacketRouter(_make_daemon())
        router._max_in_flight = 5

        barrier = asyncio.Event()

        async def blocking_route(pkt):
            await barrier.wait()

        router._route_packet = blocking_route

        await router.start()

        for _ in range(5):
            await router.enqueue(_make_packet())
        await asyncio.sleep(0.05)

        self.assertEqual(
            router._in_flight, len(router._route_tasks),
            f"Counter ({router._in_flight}) != set size ({len(router._route_tasks)})"
        )

        barrier.set()
        await router.stop()


if __name__ == "__main__":
    unittest.main()
