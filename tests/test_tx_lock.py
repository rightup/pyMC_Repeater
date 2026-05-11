"""
Tests for TX lock serialisation and duty-cycle TOCTOU fix.
Addresses the three concerns raised in PR 190 review:

  1. Concurrent delayed_sends must not interleave send_packet calls.
  2. Duty-cycle TOCTOU must be fixed: the second packet is dropped when the
     first consumes the airtime budget inside the lock.
  3. Local retry must NOT hold _tx_lock during the 1-second backoff sleep —
     other queued packets must be able to transmit during that window.

Run with:
    python -m pytest tests/test_tx_lock.py -v
or:
    python -m unittest tests.test_tx_lock
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Minimal handler factory
# ---------------------------------------------------------------------------

def _make_handler():
    """
    Return a RepeaterHandler instance with all external I/O mocked.

    Uses __new__ + manual attribute injection to bypass StorageCollector,
    radio hardware, and other heavy dependencies that are irrelevant to the
    TX lock behaviour under test.
    """
    from repeater.engine import RepeaterHandler

    radio = MagicMock()
    radio.spreading_factor = 9
    radio.bandwidth = 62500
    radio.coding_rate = 5
    radio.preamble_length = 17
    radio.frequency = 915_000_000
    radio.tx_power = 14

    dispatcher = MagicMock()
    dispatcher.radio = radio
    dispatcher.local_identity = None
    dispatcher.send_packet = AsyncMock()

    h = RepeaterHandler.__new__(RepeaterHandler)
    h.config = {
        "repeater": {"mode": "forward", "cache_ttl": 3600,
                     "send_advert_interval_hours": 0},
        "delays": {"tx_delay_factor": 1.0, "direct_tx_delay_factor": 0.5},
        "duty_cycle": {"enforcement_enabled": True,
                       "max_airtime_per_minute": 3600},
        "storage": {},
        "mesh": {},
    }
    h.dispatcher = dispatcher
    h.airtime_mgr = MagicMock()
    h.airtime_mgr.can_transmit.return_value = (True, 0.0)
    h.airtime_mgr.calculate_airtime.return_value = 100.0
    h._tx_lock = asyncio.Lock()
    h.sent_flood_count = 0
    h.sent_direct_count = 0
    # Stub out _record_packet_sent so it doesn't touch packet.header constants
    h._record_packet_sent = MagicMock()
    return h


def _make_packet(size: int = 50) -> MagicMock:
    pkt = MagicMock()
    pkt.get_raw_length.return_value = size
    pkt.header = 0x00
    return pkt


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTxLockSerialisation(unittest.IsolatedAsyncioTestCase):

    # ── Test 1: no interleaving ─────────────────────────────────────────────

    async def test_concurrent_sends_do_not_interleave(self):
        """
        Two delayed_sends with identical delays race to the radio.
        send_packet must never be called while another call is already
        in-flight — i.e. _tx_lock must gate them sequentially.
        """
        h = _make_handler()
        pkt = _make_packet()

        in_flight = [False]
        overlap_detected = [False]

        async def send_with_overlap_check(*args, **kwargs):
            if in_flight[0]:
                overlap_detected[0] = True
            in_flight[0] = True
            await asyncio.sleep(0.05)   # simulate ~50ms radio TX
            in_flight[0] = False

        h.dispatcher.send_packet.side_effect = send_with_overlap_check

        # Both tasks use the same tiny delay so their timers expire together.
        t1 = await h.schedule_retransmit(pkt, delay=0.01, airtime_ms=0)
        t2 = await h.schedule_retransmit(pkt, delay=0.01, airtime_ms=0)
        await asyncio.gather(t1, t2, return_exceptions=True)

        self.assertFalse(
            overlap_detected[0],
            "send_packet was entered while another call was already in-flight "
            "— _tx_lock is not serialising correctly",
        )
        self.assertEqual(h.dispatcher.send_packet.call_count, 2,
                         "Expected exactly 2 send_packet calls")

    # ── Test 2: TOCTOU duty-cycle fix ──────────────────────────────────────

    async def test_duty_cycle_toctou_is_fixed(self):
        """
        When two tasks both pass the advisory can_transmit() check in __call__
        before either has recorded airtime, the authoritative check inside
        _tx_lock must ensure only one of them actually transmits.

        Simulated here by making can_transmit return True for the first
        in-lock check and False for every subsequent one.
        """
        h = _make_handler()
        pkt = _make_packet()
        airtime_ms = 100.0

        # First lock-holder gets True; second gets False (budget consumed).
        allow = [True]

        def can_tx(ms):
            if allow[0]:
                allow[0] = False
                return (True, 0.0)
            return (False, 5.0)

        h.airtime_mgr.can_transmit.side_effect = can_tx

        # Both tasks start simultaneously (delay=0).
        t1 = await h.schedule_retransmit(pkt, delay=0.0, airtime_ms=airtime_ms)
        t2 = await h.schedule_retransmit(pkt, delay=0.0, airtime_ms=airtime_ms)
        await asyncio.gather(t1, t2, return_exceptions=True)

        self.assertEqual(
            h.dispatcher.send_packet.call_count, 1,
            "Both packets were sent — duty-cycle TOCTOU race was NOT fixed",
        )

    # ── Test 3: retry backoff does not hold the lock ────────────────────────

    async def test_local_retry_releases_lock_during_backoff(self):
        """
        When a local_transmission send fails on the first attempt, the 1-second
        backoff sleep must happen with _tx_lock released.

        We schedule:
          - pkt_local: local_transmission=True, delay=0s, fails on attempt 1
          - pkt_other: local_transmission=False, delay=0.1s

        pkt_other fires at ~0.1s.  Without the fix, the backoff sleep holds
        the lock until ~1.0s, so pkt_other would have to wait.  With the fix,
        pkt_other sends freely at ~0.1s, well before pkt_local retries at ~1.0s.
        """
        h = _make_handler()
        pkt_local = _make_packet()
        pkt_other = _make_packet()

        send_times: dict[int, float] = {}
        first_local_call = [True]

        async def tracked_send(*args, **kwargs):
            pkt = args[0]
            if pkt is pkt_local and first_local_call[0]:
                first_local_call[0] = False
                raise RuntimeError("simulated transient radio failure")
            send_times[id(pkt)] = time.monotonic()

        h.dispatcher.send_packet.side_effect = tracked_send

        t_local = await h.schedule_retransmit(
            pkt_local, delay=0.0, airtime_ms=0, local_transmission=True
        )
        t_other = await h.schedule_retransmit(
            pkt_other, delay=0.1, airtime_ms=0, local_transmission=False
        )
        await asyncio.gather(t_local, t_other, return_exceptions=True)

        self.assertIn(id(pkt_other), send_times,
                      "pkt_other was never sent")
        self.assertIn(id(pkt_local), send_times,
                      "pkt_local retry was never sent")

        # pkt_other fires at ~0.1s; pkt_local retry fires at ~1.0s.
        # If the lock were held during backoff, pkt_other would block until ~1.0s
        # and would be recorded AFTER pkt_local's retry — the assertion below
        # would fail.
        self.assertLess(
            send_times[id(pkt_other)],
            send_times[id(pkt_local)],
            "pkt_other sent AFTER pkt_local retry — "
            "_tx_lock was still held during the 1-second backoff sleep",
        )

    # ── Test 4: non-local single-attempt re-raises on failure ──────────────

    async def test_non_local_failure_propagates(self):
        """A relayed (non-local) packet that fails send_packet raises immediately."""
        h = _make_handler()
        pkt = _make_packet()

        h.dispatcher.send_packet.side_effect = RuntimeError("radio error")

        task = await h.schedule_retransmit(pkt, delay=0.0, airtime_ms=0,
                                           local_transmission=False)
        with self.assertRaises(RuntimeError):
            await task

        # Only one attempt should have been made.
        self.assertEqual(h.dispatcher.send_packet.call_count, 1)

    # ── Test 5: duty-cycle check re-runs after backoff ──────────────────────

    async def test_duty_cycle_rechecked_on_retry(self):
        """
        If the duty cycle is exhausted during the 1-second backoff, the retry
        attempt must still be dropped — i.e. the duty-cycle gate runs on every
        lock acquisition, not just the first.
        """
        h = _make_handler()
        pkt = _make_packet()

        # First attempt: send_packet raises → triggers backoff.
        # Between attempts the budget is consumed, so the retry lock sees False.
        transmit_seq = iter([(True, 0.0), (False, 5.0)])
        h.airtime_mgr.can_transmit.side_effect = lambda ms: next(transmit_seq)

        send_calls = [0]

        async def failing_then_gone(*args, **kwargs):
            send_calls[0] += 1
            if send_calls[0] == 1:
                raise RuntimeError("transient failure")
            # Should not reach here on attempt 1 (gate rejects)
            pytest_fail = AssertionError("send_packet called on attempt 1 despite gate rejection")
            raise pytest_fail

        h.dispatcher.send_packet.side_effect = failing_then_gone

        task = await h.schedule_retransmit(
            pkt, delay=0.0, airtime_ms=100.0, local_transmission=True
        )
        await task  # should complete without error (gate returns silently)

        self.assertEqual(send_calls[0], 1,
                         "send_packet called on retry despite duty-cycle rejection")


if __name__ == "__main__":
    unittest.main()
