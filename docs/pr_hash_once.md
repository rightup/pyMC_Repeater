# PR: Compute Packet Hash Once Per Forwarded Packet

**Branch:** `perf/hash-once`  
**Base:** `rightup/fix-perfom-speed`  
**Files changed:** `repeater/engine.py` (1 file, ~51 lines net)

---

## Problem

`packet.calculate_packet_hash()` runs a SHA-256 digest over the full serialised
packet bytes, converts the result to a hex string, and uppercases it.  Before
this change the hot forwarding path triggered this computation **three times per
packet**:

| Call site | Where | When |
|-----------|-------|------|
| `__call__` line 162 | `pkt_hash_full = packet.calculate_packet_hash()...` | Every received packet |
| `flood_forward` / `direct_forward` via `is_duplicate` | `pkt_hash = packet.calculate_packet_hash()...` | Every packet that reaches the forward check |
| `flood_forward` / `direct_forward` via `mark_seen` | `pkt_hash = packet_hash or packet.calculate_packet_hash()...` | Every packet that passes the duplicate check |

And on the drop path, a fourth computation:

| Call site | Where | When |
|-----------|-------|------|
| `_get_drop_reason` → `is_duplicate` | `pkt_hash = packet.calculate_packet_hash()...` | Every dropped packet |

The hash computed in `__call__` was already available as `pkt_hash_full` but was
never passed into `process_packet`, `flood_forward`, `direct_forward`,
`is_duplicate`, `mark_seen`, or `_get_drop_reason`.  Each of those methods
recomputed it independently.

---

## Root Cause

The `packet_hash` optional parameter existed on `mark_seen` but not on
`is_duplicate`, `flood_forward`, `direct_forward`, `process_packet`, or
`_get_drop_reason`.  The call chain therefore had no way to propagate the
already-computed hash.

---

## Solution

Thread the pre-computed `pkt_hash_full` from `__call__` down through the call
chain as an optional `packet_hash: Optional[str] = None` parameter.  Each method
uses the provided hash if present, or falls back to computing it — preserving
backward compatibility for any caller that doesn't have a pre-computed hash.

```
Before:
  __call__            → calculate_packet_hash()          #1
  → process_packet
    → flood_forward
      → is_duplicate  → calculate_packet_hash()          #2
      → mark_seen     → calculate_packet_hash()          #3
  (drop path)
  → _get_drop_reason
    → is_duplicate    → calculate_packet_hash()          #4

After:
  __call__            → calculate_packet_hash()          #1 (only computation)
  → process_packet(packet_hash=pkt_hash_full)
    → flood_forward(packet_hash=pkt_hash_full)
      → is_duplicate(packet_hash=pkt_hash_full)    uses provided hash ✓
      → mark_seen(packet_hash=pkt_hash_full)        uses provided hash ✓
  (drop path)
  → _get_drop_reason(packet_hash=pkt_hash_full)
    → is_duplicate(packet_hash=pkt_hash_full)      uses provided hash ✓
```

---

## Methods Changed

### `is_duplicate(packet, packet_hash=None)`

```python
# Before
def is_duplicate(self, packet: Packet) -> bool:
    pkt_hash = packet.calculate_packet_hash().hex().upper()  # always recomputed
    if pkt_hash in self.seen_packets:
        return True
    return False

# After
def is_duplicate(self, packet: Packet, packet_hash: Optional[str] = None) -> bool:
    """...
    INVARIANT: purely synchronous — no await points.  The caller relies on
    is_duplicate + mark_seen being atomic within the asyncio event loop.
    Do NOT add any await here without revisiting that invariant.
    """
    pkt_hash = packet_hash or packet.calculate_packet_hash().hex().upper()
    return pkt_hash in self.seen_packets
```

### `_get_drop_reason(packet, packet_hash=None)`

```python
# Before
def _get_drop_reason(self, packet: Packet) -> str:
    if self.is_duplicate(packet): ...   # recomputes hash

# After
def _get_drop_reason(self, packet: Packet, packet_hash: Optional[str] = None) -> str:
    if self.is_duplicate(packet, packet_hash=packet_hash): ...   # propagates hash
```

### `flood_forward(packet, packet_hash=None)`

```python
# Before
def flood_forward(self, packet: Packet) -> Optional[Packet]:
    ...
    if self.is_duplicate(packet): ...   # recomputes
    self.mark_seen(packet)              # recomputes

# After
def flood_forward(self, packet: Packet, packet_hash: Optional[str] = None) -> Optional[Packet]:
    """...
    INVARIANT: purely synchronous — no await points.
    """
    ...
    if self.is_duplicate(packet, packet_hash=packet_hash): ...   # propagates
    self.mark_seen(packet, packet_hash=packet_hash)              # propagates
```

### `direct_forward(packet, packet_hash=None)` — same pattern as `flood_forward`

### `process_packet(packet, snr=0.0, packet_hash=None)`

```python
# Before
def process_packet(self, packet, snr=0.0):
    fwd_pkt = self.flood_forward(packet)    # no hash

# After
def process_packet(self, packet, snr=0.0, packet_hash=None):
    """...
    packet_hash: pre-computed SHA-256 hex from __call__; eliminates 2 SHA-256
    calls per forwarded packet by propagating the hash through the call chain.
    """
    fwd_pkt = self.flood_forward(packet, packet_hash=packet_hash)
```

### `__call__` — two call-site changes

```python
# Before
result = (None if ... else self.process_packet(processed_packet, snr))
...
drop_reason = processed_packet.drop_reason or self._get_drop_reason(processed_packet)

# After
result = (None if ... else self.process_packet(processed_packet, snr, packet_hash=pkt_hash_full))
...
drop_reason = processed_packet.drop_reason or self._get_drop_reason(
    processed_packet, packet_hash=pkt_hash_full
)
```

---

## What Was Not Changed

`record_packet_only` (line 446) and `record_duplicate` (line 486) each compute
the hash independently.  These are separate recording paths (called from the
inject path and from the raw-packet subscriber, respectively) that have no
`pkt_hash_full` from `__call__` in scope.  Changing them would require a larger
refactor with no benefit to the forwarding hot path, so they are left unchanged.

The fallback `packet_hash or packet.calculate_packet_hash()...` pattern in
`is_duplicate`, `mark_seen`, and `_build_packet_record` ensures external callers
(e.g. `TraceHelper.is_duplicate(packet)` from trace processing) continue to work
without any change.

---

## Invariant Comments Added

`flood_forward`, `direct_forward`, and `is_duplicate` now carry explicit docstring
invariants:

> **INVARIANT:** purely synchronous — no await points.  The is_duplicate +
> mark_seen pair is atomic within the asyncio event loop.  Do NOT add any await
> here without revisiting that invariant in `__call__` / `process_packet`.

These invariants were implicit before.  Making them explicit means a future
contributor adding an `await` inside these methods will see the warning and
understand the consequence: the duplicate-check and mark-seen can no longer be
guaranteed atomic, allowing the same packet to be forwarded twice under concurrent
task dispatch.

---

## Quantification

On a Raspberry Pi running CPython 3.13, `hashlib.sha256` on a 50–200 byte
LoRa payload takes approximately 1–3 µs.  The `.hex().upper()` string conversion
adds another ~0.5 µs.  Savings per forwarded packet: ~3–8 µs.

At 3 packets/second sustained forwarding rate this saves ~10–25 µs/second, which
is negligible in absolute terms.  The more significant benefit is correctness and
clarity:

- One canonical hash value per packet in the forwarding path.
- No possibility of the hash changing between the `is_duplicate` check and the
  `mark_seen` call if `calculate_packet_hash` had any mutable state (it doesn't,
  but the pattern is now provably correct).
- Explicit invariant documentation closes a latent trap for future contributors.

---

## Test Plan

### Unit tests (no hardware)

**T1 — Hash computed exactly once per forwarded packet**

```python
async def test_hash_computed_once_for_flood():
    call_count = 0
    original = Packet.calculate_packet_hash

    def counting_hash(self):
        nonlocal call_count
        call_count += 1
        return original(self)

    with patch.object(Packet, "calculate_packet_hash", counting_hash):
        await engine(flood_packet, metadata={})

    assert call_count == 1, f"Expected 1 hash computation, got {call_count}"
```

**T2 — Hash computed exactly once per dropped (duplicate) packet**

```python
async def test_hash_computed_once_for_duplicate():
    # Mark packet seen first
    engine.seen_packets[packet.calculate_packet_hash().hex().upper()] = time.time()

    call_count = 0
    original = Packet.calculate_packet_hash
    def counting_hash(self):
        nonlocal call_count; call_count += 1; return original(self)

    with patch.object(Packet, "calculate_packet_hash", counting_hash):
        await engine(packet, metadata={})

    # One computation in __call__ for pkt_hash_full; should not trigger again
    # in process_packet → flood_forward → is_duplicate (drop path via _get_drop_reason)
    assert call_count == 1
```

**T3 — External callers of `is_duplicate` without hash still work**

```python
def test_is_duplicate_without_hash():
    """TraceHelper and other external callers pass no hash — must still work."""
    pkt = make_test_packet()
    engine.seen_packets[pkt.calculate_packet_hash().hex().upper()] = time.time()

    assert engine.is_duplicate(pkt) is True   # no packet_hash arg
    assert engine.is_duplicate(pkt, packet_hash="WRONGHASH") is False
```

**T4 — mark_seen / is_duplicate agree on the same hash**

```python
def test_mark_then_is_duplicate_consistent():
    pkt = make_test_packet()
    pkt_hash = pkt.calculate_packet_hash().hex().upper()

    assert engine.is_duplicate(pkt, packet_hash=pkt_hash) is False
    engine.mark_seen(pkt, packet_hash=pkt_hash)
    assert engine.is_duplicate(pkt, packet_hash=pkt_hash) is True
    # Same result without the pre-computed hash (fallback path)
    assert engine.is_duplicate(pkt) is True
```

**T5 — flood_forward / direct_forward signatures are backward compatible**

```python
def test_flood_forward_no_hash_arg():
    """Callers that don't pass packet_hash must still work (fallback compute)."""
    pkt = make_flood_packet()
    result = engine.flood_forward(pkt)   # no packet_hash — must not raise
    assert result is not None or pkt.drop_reason is not None
```

### Integration / field tests (with hardware)

**T6 — Forwarding throughput unchanged**

1. Forward 100 packets at maximum duty-cycle budget.
2. Verify all eligible packets are forwarded (same count as before change).
3. Verify no `Duplicate` drops that were not present before.

**T7 — Duplicate detection unchanged**

1. Send the same packet twice within 1 second.
2. Verify the first is forwarded and the second is logged as `"Duplicate"`.

**T8 — CPU profile shows reduced `calculate_packet_hash` calls**

1. Enable Python profiling (`cProfile`) on the repeater for 60 seconds.
2. Compare `calculate_packet_hash` call count before and after.

**Expected:** call count approximately halved for workloads where most packets
are forwarded (≤ 1 call per forwarded packet vs ≥ 3 before).

---

## Proof of Correctness

### Why the fallback `packet_hash or packet.calculate_packet_hash()` is safe

`packet_hash` is either the correct hash (passed from `__call__`) or `None`.
If it is `None`, the fallback computes the hash fresh — identical to the old
behaviour.  There is no case where a wrong hash is used: the only source of a
non-None `packet_hash` is `pkt_hash_full = packet.calculate_packet_hash()...`
in `__call__`, computed over the same `processed_packet` (a deep copy of the
received packet, unchanged between hash computation and the call to
`process_packet`).

### Why passing the hash through a deep-copied packet is correct

`processed_packet = copy.deepcopy(packet)` (line 178) happens before
`pkt_hash_full` is passed to `process_packet`.  The deep copy does not change
the packet's wire representation — `calculate_packet_hash()` calls
`packet.write_to()` which serialises the packet's fields.  The copy has the
same fields, so `deepcopy(packet).calculate_packet_hash() == packet.calculate_packet_hash()`.
Passing the hash computed from the original to the copy is correct.

### Why the invariant is critical

asyncio only yields execution at `await` points.  `flood_forward` and
`direct_forward` have no `await`, so they run atomically from the event loop's
perspective.  The `is_duplicate` check and the `mark_seen` call inside them
cannot be interleaved with another coroutine.  If a future change added an
`await` between them, two concurrent `_route_packet` tasks could both pass the
duplicate check for the same packet before either marked it seen — sending the
same packet twice.  The invariant comment documents this so the risk is visible
at the point where it could be broken.
