# PR: Serialise Radio TX and Close Duty-Cycle TOCTOU Race

**Branch:** `fix/tx-serialization`  
**Base:** `rightup/fix-perfom-speed`  
**Files changed:** `repeater/engine.py` (1 file, ~30 lines net)

---

## Problem

Two separate bugs share the same root cause: concurrent `delayed_send` coroutines
racing each other at transmission time.

### Bug 1 — Interleaved SPI/serial commands to the radio

The queue loop (added in an earlier commit) dispatches each incoming packet as an
`asyncio.create_task`, so multiple `delayed_send` coroutines can have their sleep
timers running concurrently.  That is correct and intentional — it mirrors how
firmware nodes use a hardware timer so the radio keeps listening during a TX delay.

However the LoRa radio is **half-duplex**: it can only transmit one packet at a
time.  When two delay timers expire at nearly the same moment both coroutines call
`dispatcher.send_packet` simultaneously.  `send_packet` issues a sequence of
SPI/serial register writes to the radio; two tasks interleaving these writes
produces undefined radio state and the transmission of neither packet is reliable.

### Bug 2 — TOCTOU gap in duty-cycle enforcement

`__call__` calls `can_transmit()` before scheduling a task:

```python
# __call__ (before this fix)
can_tx, wait_time = self.airtime_mgr.can_transmit(airtime_ms)
if not can_tx:
    ...  # drop or defer
tx_task = await self.schedule_retransmit(fwd_pkt, delay, airtime_ms, ...)
```

`record_tx()` is only called later, inside `delayed_send`, after the sleep
completes.  Between the check and the debit there is a window that spans the
entire TX delay (up to several seconds).  Two packets that both pass the check
before either has slept and recorded its airtime will **both** be transmitted even
if transmitting both would exceed the duty-cycle budget.

Under normal single-packet conditions this window is harmless.  Under burst
conditions — multi-hop amplification, collision retries, or a busy mesh segment
where several packets arrive within the same delay window — multiple tasks pass
the advisory check simultaneously, and the duty-cycle limit is exceeded.

---

## Root Cause

There is no mutual exclusion around the radio send path.  Each `delayed_send`
coroutine independently checks duty-cycle, sleeps, and transmits without
coordinating with any other concurrent coroutine doing the same thing.

---

## Solution

Add `self._tx_lock = asyncio.Lock()` (initialised in `__init__`) and acquire it
inside `delayed_send` **after** the sleep completes:

```
Delay timers run concurrently (unchanged):
  Task A: sleep(1.2s) ──────────────────► acquire _tx_lock → check → TX A → release
  Task B: sleep(0.9s) ──────────────────► acquire _tx_lock (waits) ──────────► check → TX B → release
  Task C: sleep(2.1s) ────────────────────────────────────────────────────────────────► ...

Radio: one packet at a time, duty-cycle state always stable inside the lock.
```

Inside the lock, a **second** `can_transmit()` call is made immediately before
sending.  Because only one task holds the lock at a time, airtime state is stable
at this point and `record_tx()` follows on success — check and debit are
effectively atomic.  This closes the TOCTOU window completely.

The upfront `can_transmit()` in `__call__` is retained as an **advisory** fast
path: it still drops or defers packets that are obviously over budget before a
delay task is even scheduled, avoiding unnecessary sleep timers.  It is no longer
the enforcement point.

---

## Why This Is the Right Approach

### Alternative A — Move `record_tx()` before the sleep

```python
# hypothetical
self.airtime_mgr.record_tx(airtime_ms)   # reserve before sleeping
await asyncio.sleep(delay)
await self.dispatcher.send_packet(...)    # actual TX
```

Records airtime even if the send fails (exception, LBT busy, radio error) —
the budget is debited for a packet that was never transmitted.  Over time this
inflates the apparent airtime, causing the node to throttle legitimate traffic
it actually has budget for.  Requires a compensating `release_airtime()` on
every failure path, creating new complexity and failure modes.

### Alternative B — A single global advisory check (status quo before this PR)

Already demonstrated to fail under burst conditions (two tasks both pass before
either records its airtime).

### Alternative C — asyncio.Lock (this PR)

- Delay timers remain concurrent — no regression on the primary non-blocking TX
  improvement.
- The check-and-debit pair is atomic within the lock — no TOCTOU window.
- No phantom airtime on send failure — `record_tx()` is only called on success.
- One `asyncio.Lock` object, no new state machines or compensating paths.
- The lock is `async`, so it only blocks other TX tasks, not the event loop or
  the packet RX queue.

### Why `asyncio.Lock` rather than `threading.Lock`

The entire repeater runs on a single asyncio event loop.  `asyncio.Lock` only
yields at `await` points; it does not involve OS threads or context switches.
A `threading.Lock` would work but is semantically wrong here (this is not a
thread-safety problem) and would block the event loop thread if held across an
`await`.

---

## Changes

### `repeater/engine.py`

**1. Move `import random` to module level**

```python
# before (inside _calculate_tx_delay):
def _calculate_tx_delay(self, packet, snr=0.0):
    import random
    ...

# after (top of file, with other stdlib imports):
import random
```

This is a housekeeping fix bundled with this PR because `random` is a stdlib
module that should never be imported inside a hot-path function — Python caches
the import after the first call, but the attribute lookup and cache check still
run on every call.  Moving it to module level is the standard pattern.

**2. Add `self._tx_lock` to `__init__`**

```python
# Serialise all radio TX calls.
#
# Background: since the queue loop dispatches each packet as an
# asyncio.create_task, multiple _route_packet coroutines can have their
# TX delay timers running concurrently — which is the intended behaviour
# (firmware nodes do the same with a hardware timer).  However, the
# LoRa radio is half-duplex: it can only transmit one packet at a time.
# Without serialisation, two tasks whose delay timers expire near-
# simultaneously both call dispatcher.send_packet, interleaving SPI/serial
# commands to the radio and both passing the LBT check before either has
# actually transmitted.
#
# _tx_lock is acquired after each delay sleep and held for the entire
# send_packet call.  Delays still run concurrently; only the radio
# access is serialised.  This also eliminates the TOCTOU gap in duty-cycle
# enforcement — see schedule_retransmit / delayed_send for details.
self._tx_lock = asyncio.Lock()
```

**3. Acquire lock inside `delayed_send`, add authoritative duty-cycle gate**

```python
async def delayed_send():
    await asyncio.sleep(delay)

    # Acquire the TX lock *after* the delay so that delay timers for
    # multiple packets still run concurrently (matching firmware).  Only
    # one coroutine enters the radio send path at a time.
    async with self._tx_lock:
        # ── Authoritative duty-cycle gate ─────────────────────────────
        # The upfront can_transmit() call in __call__ is advisory: it
        # avoids scheduling packets that are obviously over budget, but
        # it cannot prevent a race between two tasks whose delay timers
        # expire at almost the same moment.  Both tasks pass the advisory
        # check before either has recorded its airtime, then both try to
        # transmit.
        #
        # Inside _tx_lock only one task runs at a time, so airtime state
        # is stable here.  The check and the subsequent record_tx() are
        # effectively atomic — no TOCTOU window.
        if airtime_ms > 0:
            can_tx_now, _ = self.airtime_mgr.can_transmit(airtime_ms)
            if not can_tx_now:
                logger.warning(
                    "Packet dropped at TX time: duty-cycle exceeded "
                    "(airtime=%.1fms)", airtime_ms,
                )
                return

        last_error = None
        for attempt in range(2 if local_transmission else 1):
            try:
                await self.dispatcher.send_packet(fwd_pkt, wait_for_ack=False)
                self._record_packet_sent(fwd_pkt)
                if airtime_ms > 0:
                    self.airtime_mgr.record_tx(airtime_ms)
                ...
```

---

## Invariants Maintained

| Property | Before | After |
|----------|--------|-------|
| Delay timers run concurrently | ✅ | ✅ |
| Radio accessed by one task at a time | ❌ | ✅ |
| Duty-cycle check and debit atomic | ❌ | ✅ |
| Airtime recorded only on TX success | ✅ | ✅ |
| Event loop not blocked by lock | ✅ | ✅ (asyncio.Lock) |

---

## Test Plan

### Unit tests (can run without hardware)

**T1 — Serial TX ordering**

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

async def test_tx_serialized():
    """Two tasks whose delays expire simultaneously must not interleave."""
    send_order = []
    send_lock = asyncio.Lock()

    async def mock_send(pkt, **kw):
        # Confirm the _tx_lock is already held when we enter send_packet
        assert send_lock.locked(), "send_packet called without _tx_lock held"
        send_order.append(pkt)
        await asyncio.sleep(0)  # yield; a second task must not enter here

    engine._tx_lock = send_lock   # replace with the mock lock reference
    engine.dispatcher.send_packet = mock_send

    t1 = asyncio.create_task(engine.schedule_retransmit(pkt_a, delay=0.01, airtime_ms=100))
    t2 = asyncio.create_task(engine.schedule_retransmit(pkt_b, delay=0.01, airtime_ms=100))
    await asyncio.gather(t1, t2)

    assert len(send_order) == 2           # both transmitted
    assert send_order[0] is not send_order[1]  # different packets
```

**T2 — Authoritative duty-cycle gate blocks over-budget second packet**

```python
async def test_second_packet_dropped_when_over_budget():
    """When first TX fills the budget, second task must be dropped inside the lock."""
    # Set a tiny budget: 50ms per minute
    engine.airtime_mgr.max_airtime_per_minute = 50

    sent = []
    async def mock_send(pkt, **kw):
        sent.append(pkt)

    engine.dispatcher.send_packet = mock_send

    # Each packet costs ~111ms (SF8, BW125, 30-byte payload) — first passes, second must not
    t1 = asyncio.create_task(engine.schedule_retransmit(pkt_a, delay=0.01, airtime_ms=111))
    t2 = asyncio.create_task(engine.schedule_retransmit(pkt_b, delay=0.01, airtime_ms=111))
    await asyncio.gather(t1, t2)

    assert len(sent) == 1, f"Expected 1 TX, got {len(sent)}"
```

**T3 — Airtime not debited on TX failure**

```python
async def test_airtime_not_recorded_on_send_failure():
    before = engine.airtime_mgr.total_airtime_ms

    async def failing_send(pkt, **kw):
        raise RuntimeError("radio error")

    engine.dispatcher.send_packet = failing_send

    with pytest.raises(RuntimeError):
        await engine.schedule_retransmit(pkt, delay=0, airtime_ms=100)

    assert engine.airtime_mgr.total_airtime_ms == before, \
        "Airtime must not be recorded when send raises"
```

**T4 — Advisory check still drops before scheduling (fast path not regressed)**

```python
async def test_advisory_check_still_drops_obvious_overage():
    """__call__ should not even schedule a task when clearly over budget."""
    engine.airtime_mgr.max_airtime_per_minute = 0   # budget exhausted

    tasks_created = []
    original = asyncio.create_task
    asyncio.create_task = lambda coro: tasks_created.append(coro) or original(coro)

    await engine(over_budget_packet, metadata={})

    assert not tasks_created, "No task should be created when advisory check fails"
```

### Integration / field tests (with hardware)

**T5 — Burst scenario: 5 packets arrive within the same delay window**

1. Connect the repeater to a radio.
2. Using a second node, send 5 FLOOD packets in quick succession (< 100 ms apart)
   with a low RSSI score so the repeater's delay is ~1–2 s for all of them.
3. Monitor the radio with a spectrum analyser or a third node running in monitor
   mode.

**Expected (after this fix):**
- Transmissions are sequential — no overlapping on-air signals.
- `Retransmitted packet` log lines appear one after another, each with a non-zero
  airtime value.
- No `Retransmit failed` errors in the log.
- Duty-cycle log shows airtime accumulating correctly.

**Expected (before this fix, to confirm the bug existed):**
- Occasional `Retransmit failed` errors under burst load.
- Airtime tracking diverging from actual on-air time (double-counted or missed).

**T6 — Duty-cycle enforcement under burst**

1. Set `max_airtime_per_minute` to a low value (e.g. 500 ms) in config.
2. Send 10 packets rapidly so the repeater tries to forward all 10.
3. Observe logs.

**Expected:**
- First N packets transmitted (total airtime ≤ 500 ms).
- Subsequent packets log `"Packet dropped at TX time: duty-cycle exceeded"` from
  inside `delayed_send` (not just the advisory drop).
- `airtime_mgr.get_stats()["utilization_percent"]` reads ≤ 100%.

**T7 — Normal single-packet forwarding not regressed**

1. Send one packet every 5 seconds (well within duty-cycle budget).
2. Verify each packet is forwarded with correct airtime logged.
3. Verify no lock contention warnings in the log.

**T8 — Local TX retry path (local_transmission=True) still works**

1. Send a command that triggers a local transmission (e.g. a ping reply).
2. Briefly block the radio (simulate with a mock) so the first attempt fails.
3. Verify the retry fires after 1 s and the packet is eventually transmitted.

---

## Proof of Correctness

### Why `asyncio.Lock` is sufficient (no OS-level synchronisation needed)

Python's asyncio event loop is **single-threaded**.  All coroutines share one
thread and only yield execution at `await` points.  Between two consecutive
`await` calls in a coroutine, the event loop does not switch to another coroutine.

`asyncio.Lock.acquire()` suspends the current coroutine if the lock is held,
returning control to the event loop.  `asyncio.Lock.release()` wakes the next
waiter.  Because `send_packet` is awaited inside the lock, no other TX task can
run until the current one releases the lock and the event loop gets a chance to
schedule the next waiter.

There is no possibility of the race seen with `threading.Lock` where an OS thread
can be preempted mid-instruction.

### Why the advisory check in `__call__` cannot be removed

The advisory check is still necessary as a fast path.  If it were removed, every
incoming packet — even when the node is clearly at 100% duty-cycle — would
schedule a `delayed_send` task that would sleep for the full TX delay (up to
several seconds) before the lock drops it.  Under a sustained flood of incoming
packets this wastes memory and CPU.  The advisory check prunes the queue early at
negligible cost.

### Why `record_tx()` must be inside the lock (not before or after)

- **Before the send:** records airtime for a packet that may never be transmitted
  (send could fail, LBT could reject it).  Budget is overcounted.
- **After releasing the lock:** a second task could pass the authoritative
  `can_transmit()` check between `send_packet` returning and `record_tx()` being
  called — the TOCTOU window reopens at a smaller scale.
- **Inside the lock, after a successful send:** the budget is debited exactly once
  for exactly the packets that were actually transmitted.  The lock ensures no
  other task reads airtime state between the check and the debit.
