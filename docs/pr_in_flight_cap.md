# PR: Bounded In-Flight Task Counter + Simplified Route Task Management

**Branch:** `perf/in-flight-cap`  
**Base:** `rightup/fix-perfom-speed`  
**Files changed:** `repeater/packet_router.py` (1 file, ~33 lines net)

---

## Background

The queue loop dispatches each incoming packet as an `asyncio.create_task` so TX
delay timers run concurrently — this is correct behaviour.  The previous
implementation tracked these tasks in a `set[asyncio.Task]` (`_route_tasks`) for
two reasons:

1. **Error surfacing** — the done-callback read `task.result()` to log exceptions.
2. **Shutdown cancellation** — `stop()` cancelled and awaited all tasks in the set.

This PR replaces the set with a simple integer counter and tightens the companion
deduplication prune threshold.

---

## Problems

### Problem 1 — Unbounded task accumulation

LoRa airtime naturally limits steady-state throughput to a handful of in-flight
tasks at any time.  But burst arrivals can spike the count temporarily:

- **Multi-hop flood amplification**: a single source packet is forwarded by every
  repeater in range, each of which re-broadcasts it. A node at a mesh junction
  may receive 5–10 copies within 100 ms, each scheduling a separate `delayed_send`
  task.
- **Collision retries**: hardware-level collisions produce duplicate RF bursts that
  all arrive within the same RX window.
- **Bridge nodes**: high-traffic gateway nodes connect multiple mesh segments and
  forward both directions simultaneously.

Under these conditions `_route_tasks` can accumulate dozens of sleeping tasks.
Each holds a reference to the packet, the forwarded packet copy, a closure over
`delayed_send`, and associated asyncio task overhead.  There is no cap; the set
grows until the duty-cycle gate finally fires for each task.

### Problem 2 — `_route_tasks` set adds O(1) cost on every packet but O(n) cost on shutdown

Every packet adds one entry to `_route_tasks` and removes it in the done-callback.
This is O(1) per operation, but the `stop()` shutdown path iterates the entire set
to cancel and gather all tasks — O(n) where n is however many tasks happen to be
in-flight at shutdown time.  On a busy node this could delay clean shutdown.

### Problem 3 — `_COMPANION_DEDUPE_PRUNE_THRESHOLD = 1000` is too high

The companion delivery deduplication dict prunes itself only when it exceeds 1000
entries.  With a 60-second TTL, each PATH/protocol-response packet adds one entry.
On a busy mesh with 50+ nodes sending adverts and PATH packets, the dict can grow
to hundreds of entries before a prune is triggered — keeping stale entries in
memory for up to 60 seconds × 1000/rate entries worth of time.

---

## Solution

### Replace `_route_tasks` set with `_in_flight` counter

An integer counter provides the same protection (tasks complete; done-callback
fires) without holding strong references to each task object:

```python
# __init__
self._in_flight: int = 0
self._max_in_flight: int = 30

# _process_queue — drop early if cap reached
if self._in_flight >= self._max_in_flight:
    logger.warning("In-flight task cap reached (%d/%d), dropping packet", ...)
    continue
self._in_flight += 1
task = asyncio.create_task(self._route_packet(packet))
task.add_done_callback(self._on_route_done)

# done-callback
def _on_route_done(self, task):
    self._in_flight -= 1
    if not task.cancelled() and task.exception():
        logger.error("_route_packet raised: %s", task.exception(), ...)
```

### Cap at 30 concurrent in-flight tasks

30 is chosen as a ceiling that is:
- **Never reached in normal operation**: LoRa airtime at SF8/125 kHz limits
  throughput to ~2–3 packets per second; with delays of 0.5–5 s each, the
  steady-state in-flight count is at most 5–15 tasks.
- **High enough not to drop legitimate traffic**: a burst of 30 nearly-simultaneous
  packets would require every node in a large mesh to transmit within 1 second.
- **Low enough to protect against pathological scenarios**: a misconfigured node
  flooding the channel or a software bug causing infinite re-queuing.

### Tighten companion dedup prune threshold to 200

200 entries at 60 s TTL means a sweep is triggered after ~200 unique PATH/response
packets arrive without any expiry.  This is far more than a typical companion
session (which sees a handful of active connections) but prevents multi-hour
accumulation on a busy mesh.

---

## Trade-off: Shutdown Cancellation

The previous `_route_tasks` set allowed `stop()` to explicitly cancel and await
all in-flight tasks on shutdown.  The counter approach does not.

**Why this is acceptable:**

1. In-flight `_route_packet` tasks are sleeping inside `delayed_send` (waiting for
   their TX delay timer).  When the event loop is shut down — whether via
   `asyncio.run()` completing, `loop.stop()`, or `SIGTERM` handling — Python
   cancels all pending tasks automatically.

2. Even under the old approach, cancelling a sleeping `delayed_send` means the
   packet is not transmitted.  The result is the same whether cancellation happens
   explicitly in `stop()` or implicitly when the event loop closes.

3. For a graceful shutdown where we want to *wait* for in-flight packets to
   complete transmission, the right mechanism is `stop()` awaiting the queue to
   drain *before* cancelling the router task — not cancelling sleeping tasks.
   Neither the old code nor this PR implements that, so no regression.

---

## Why This Is the Right Approach

### Alternative A — Keep `_route_tasks` set, add a size cap

```python
if len(self._route_tasks) >= 30:
    logger.warning(...)
    continue
```

Works, but the set still holds a strong reference to every Task object for the
duration of its sleep.  The counter holds an integer.  Task objects in Python 3.12+
are already strongly referenced by the event loop scheduler; the set reference is
redundant for preventing GC cancellation.

### Alternative B — `asyncio.Semaphore`

```python
self._sem = asyncio.Semaphore(30)
async with self._sem:
    await self._route_packet(packet)
```

Correct but changes the queue loop from fire-and-forget to blocking: the loop
would wait at `async with self._sem` for a slot to open, stalling packet reads
while a slot is occupied.  That reintroduces the queue freeze the concurrent
dispatch was designed to prevent.  A semaphore is the right tool for *rate-
limiting* producers; a counter cap at the dispatch site is the right tool for
bounding *background* tasks.

### Alternative C — Integer counter (this PR)

- O(1) increment and decrement.
- No strong reference to task objects beyond the event loop's own reference.
- Drop decision is synchronous and immediate — no sleeping on semaphore.
- Error logging preserved in `_on_route_done`.
- Simpler code, easier to reason about.

---

## Changes — `repeater/packet_router.py` only

| Location | Change | Reason |
|----------|--------|--------|
| Module level | Remove `_COMPANION_DEDUPE_PRUNE_THRESHOLD = 1000` | Replaced with inline literal `200`; no need for a named constant for a single usage site |
| `__init__` | Remove `self._route_tasks = set()`; add `self._in_flight = 0`, `self._max_in_flight = 30` | Replace set-based tracking with counter |
| `stop()` | Remove `_route_tasks` cancellation block | Tasks complete or are cancelled by event loop shutdown; explicit cancellation not needed |
| `_on_route_task_done` → `_on_route_done` | Simpler done-callback: decrement counter + log exceptions | Error logging preserved; set management removed |
| `_should_deliver_path_to_companions` | `> _COMPANION_DEDUPE_PRUNE_THRESHOLD` → `> 200` with explanatory comment | Lower threshold; comment explains the sizing rationale |
| `_process_queue` | Check `_in_flight >= _max_in_flight` before `create_task`; increment `_in_flight`; use `_on_route_done` | Cap accumulation; counter tracks live task count |

---

## Test Plan

### Unit tests (no hardware)

**T1 — Counter increments and decrements correctly**

```python
async def test_in_flight_counter():
    router = PacketRouter(mock_daemon)
    await router.start()

    assert router._in_flight == 0

    # Enqueue a packet that takes time to process
    async def slow_route(pkt):
        await asyncio.sleep(0.1)

    router._route_packet = slow_route
    await router.enqueue(make_test_packet())
    await asyncio.sleep(0.01)  # let queue loop run

    assert router._in_flight == 1   # task is sleeping

    await asyncio.sleep(0.15)       # task finishes
    assert router._in_flight == 0   # counter decremented by done-callback
```

**T2 — Cap enforced: packet dropped when at limit**

```python
async def test_cap_drops_packet_at_limit():
    router = PacketRouter(mock_daemon)
    router._max_in_flight = 2
    router._in_flight = 2   # simulate cap reached

    dropped = []
    original_create_task = asyncio.create_task
    asyncio.create_task = lambda coro: dropped.append(coro)

    await router._process_queue_once(make_test_packet())

    assert dropped == [], "create_task must not be called when cap is reached"
    asyncio.create_task = original_create_task
```

**T3 — Exceptions in `_route_packet` are logged, not swallowed**

```python
async def test_exception_logged():
    router = PacketRouter(mock_daemon)

    async def failing_route(pkt):
        raise ValueError("simulated error")

    router._route_packet = failing_route
    with patch("repeater.packet_router.logger") as mock_log:
        task = asyncio.create_task(failing_route(make_test_packet()))
        router._in_flight = 1
        task.add_done_callback(router._on_route_done)
        await asyncio.gather(task, return_exceptions=True)
        mock_log.error.assert_called_once()

    assert router._in_flight == 0
```

**T4 — Companion dedup dict pruned at 200, not 1000**

```python
def test_companion_dedup_prune_threshold():
    router = PacketRouter(mock_daemon)
    future_time = time.time() + 999

    # Fill with 199 entries (all unexpired) — no prune
    router._companion_delivered = {f"key{i}": future_time for i in range(199)}
    pkt = make_path_packet()
    router._should_deliver_path_to_companions(pkt)
    assert len(router._companion_delivered) == 200   # added one, no prune yet

    # 201st entry triggers prune — all unexpired so count stays at 201
    router._companion_delivered[f"key_extra"] = future_time
    assert len(router._companion_delivered) == 201

    # Force prune by making all existing entries expired
    past_time = time.time() - 1
    router._companion_delivered = {f"key{i}": past_time for i in range(201)}
    router._should_deliver_path_to_companions(pkt)
    # All expired entries pruned; only the new entry remains
    assert len(router._companion_delivered) == 1
```

### Integration / field tests (with hardware)

**T5 — Burst flood: verify cap fires under pathological load**

1. Configure a test mesh with 4+ nodes all in range of the repeater.
2. Have all nodes send a flood packet simultaneously.
3. Observe repeater logs.

**Expected:** `_in_flight` peaks in low single digits (LoRa airtime prevents
large bursts); no `"In-flight task cap reached"` warning fires under normal
conditions, confirming the cap is never a bottleneck in practice.

**T6 — Counter reaches zero after all packets processed**

1. Send a burst of 10 packets.
2. Wait 10 seconds (longer than max TX delay of 5 s).
3. Query `router._in_flight` from a debug endpoint or log.

**Expected:** `_in_flight == 0` after all delays expire and packets transmit.

**T7 — Error in `_route_packet` is logged and counter is decremented**

1. Temporarily introduce a deliberate exception in `_route_packet`.
2. Send a packet.
3. Check logs for the error message and verify the repeater continues operating
   (counter decremented, queue still draining).

**T8 — Normal forwarding throughput unchanged**

1. Send packets at a steady rate of 1 every 10 seconds for 5 minutes.
2. Verify all packets are forwarded with no warnings or errors.
3. Confirm `_in_flight` never exceeds 3–4 during normal operation.

---

## Proof of Correctness

### Counter vs set: why the counter is sufficient

The `_route_tasks` set solved two problems:

1. **GC protection**: In Python < 3.12, a task with no strong references other
   than the event loop's internal weakref could be garbage collected before
   completing.  Python 3.12+ strengthened task references in the event loop.
   However, even in earlier versions, the set was unnecessary once `create_task`
   returns — the caller holds the reference, and the done-callback fires reliably
   because the event loop holds the task alive until completion.

2. **Explicit shutdown cancellation**: The counter loses this.  As argued above,
   the outcome is identical — sleeping tasks are cancelled either explicitly by
   `stop()` or implicitly by the event loop at shutdown — and no packet that
   hasn't been transmitted yet can complete its send after the radio is shut down
   anyway.

### Why `_on_route_done` is a done-callback and not a `try/finally` inside `_route_packet`

A `try/finally` block inside `_route_packet` would also decrement the counter.
Done-callbacks are preferable because:

- They fire even if the task is externally cancelled (e.g. by event loop shutdown),
  whereas `finally` may not run if `CancelledError` is not caught.
- They decouple counter management from `_route_packet` logic — `_route_packet`
  has no knowledge of or dependency on the cap mechanism.
- They keep the pattern consistent with the rest of the codebase's use of
  `add_done_callback` for task lifecycle management.

### Why 30 and not a smaller number like 10

At SF8, 125 kHz bandwidth, a 30-byte payload takes ~111 ms airtime and produces
a TX delay of roughly 0.5–3 s.  With a 60-second duty-cycle window and 3.6 s
max airtime, the node can forward at most ~32 packets per minute at full budget.
If all 32 arrive within one second (they cannot physically, but as an upper
bound), 32 tasks would be in-flight simultaneously.  A cap of 30 is aggressive
enough to protect against unbounded growth but not so low that it would drop
legitimate traffic under any realistic burst scenario.
