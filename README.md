# MESI cache simulator

A dependency-free Python simulator for private, set-associative caches in a
multi-core processor. It implements snooping MESI coherence, per-core LRU
replacement, and write-back/write-allocate caching. Four cores are simulated by
default. It supports both ordered single-trace replay and host-threaded
execution with one worker per core.

## Requirements and running

Python 3.11 or newer is required. There is no compilation or dependency
installation step. Run these commands from the repository root:

```powershell
python -m cache_simulator examples\mesi.trace
python -m cache_simulator examples\mesi.trace --json --events
python -m cache_simulator examples\lru.trace --sets 1 --ways 2 --block-size 16
python -m cache_simulator examples\private.trace --cores 4 --ways 8
python -m cache_simulator --core-traces examples\core0.trace examples\core1.trace examples\core2.trace examples\core3.trace --events
python -m cache_simulator --help
```

On Unix shells, use `/` in example paths instead of `\`.
Use `-` as the trace filename to read standard input. JSON output can be
redirected to a file; diagnostics go to standard error and invalid input exits
with status 2. A malformed trace produces no partial report.

## Trace format

Each nonempty line describes one memory access:

```text
<core> <R|W> <byte-address>
```

```text
# Core IDs start at zero. Comments may also follow an access.
0 R 0x1000
1 W 4096
2 r 0x1008  # Same cache block with the default 64-byte block size.
```

Core IDs are decimal and must be less than the configured core count.
Addresses are unsigned 64-bit byte addresses, either decimal or `0x`-prefixed
hexadecimal. Leading zeroes in decimal numbers do not select octal.
Operations are case-insensitive. Blank lines and `#` comments are ignored;
other malformed records are rejected with filename and line number.
Each access touches the single block containing its address, regardless of
alignment. Traces do not include access sizes, data values, timestamps, or
instruction execution.

### Per-core traces

Use `--core-traces` to enable threaded execution. Supply exactly one file per
configured core, in core-ID order. Each file omits the core column:

```text
R 0x1000
W 0x1008
```

Address syntax, comments, and validation are otherwise unchanged. A bare
number such as `1000` is decimal; use `0x1000` for hexadecimal. A combined
trace and `--core-traces` cannot be used together. Standard input is supported
only for combined traces. Empty per-core files are allowed.

## Configuration

| Option | Default | Meaning |
| --- | ---: | --- |
| `--cores` | 4 | Number of private caches |
| `--sets` | 64 | Sets in **each** cache; power of two |
| `--ways` | 4 | Lines per set; any positive integer |
| `--block-size` | 64 | Bytes per block; power of two |
| `--hit-latency` | 1 | Lookup cost paid for every access |
| `--bus-latency` | 2 | Arbitration/address cost per bus transaction |
| `--memory-latency` | 100 | Cost per full-block memory read or write |
| `--transfer-latency` | 10 | Cost per full-block peer-cache transfer |
| `--json` | off | Machine-readable statistics |
| `--events` | off | Per-access states, bus operation, and cycle costs |
| `--core-traces FILE ...` | off | Threaded execution; one file per core |

All numerical options must be positive integers. Cache capacity per core is
`sets * ways * block_size` bytes, excluding metadata. The default is 16 KiB
per core. The block number is `address // block_size`; its set index is
`block_number % sets`. LRU ordering is independent in every set and cache.
Only local accesses update LRU; snoops do not. Invalidations free ways
immediately, and replacement prefers those free ways before evicting valid data.

## Coherence and write policy

| Local access | Initial state | Action | Final state |
| --- | --- | --- | --- |
| Read | I | `BusRd`; fetch from a peer if present, otherwise memory | S with peers, E otherwise |
| Read | S, E, M | Local hit | Unchanged |
| Write | I | `BusRdX`; allocate a block and invalidate all peer copies | M |
| Write | S | `BusUpgr`; invalidate all peer copies, without fetching data | M |
| Write | E, M | Local hit, no bus transaction | M |

On `BusRd`, peers in E or M downgrade to S. On `BusRdX` or `BusUpgr`, peer
copies are invalidated. An M owner supplies the latest block and writes it
back to memory before losing ownership or becoming S. A clean E/S peer can
also supply a block; when there are multiple S peers, the lowest-numbered
core is the single supplier. An M victim is written back before the new
block is fetched; clean victims are discarded without bus traffic.
Evicting another sharer does not silently promote S to E.

Write misses fetch the entire block even for writes, because this is a
write-allocate model of potentially partial-block stores. Writes to M remain
local. There is **no final cache flush**: dirty resident blocks are reported
but not charged as memory writes at trace end.

## Execution and timing model

With a combined trace, accesses execute on the calling thread in file order.
With `--core-traces`, the simulator starts one actual host worker thread per
core. Workers read their own traces concurrently and submit accesses in
deterministic round-robin order: core 0, core 1, and so on, then repeat.
Each active core gets one access per turn; exhausted cores are skipped.
An empty or shorter trace never prevents the other cores from finishing.
This arbitration is independent of host thread scheduling and does not
prioritize a core based on its modeled cycle count.

A condition variable coordinates turns. The simulator also protects every
complete cache access with a shared lock, including local hits, evictions,
peer snoops, state transitions, and statistics. State inspection and report
snapshots use the same lock. This prevents a snoop from racing with another
core's local access. Failed reads or accesses cancel waiting workers, and
all workers are joined before an error is returned. A failed API run may
leave earlier successful accesses in the simulator; the CLI emits no partial
report.

**Threaded execution does not imply overlapping simulated cycles or a
performance speedup.** Both modes retain the same conservative, atomic-access
timing model. Cache lookups, bus transfers, and memory operations are
serialized; total cycles are service costs, not the elapsed runtime of an
overlapping multi-core processor. Python's GIL normally prevents parallel
execution of Python bytecode, and the shared coherence lock further limits
concurrency. Trace reads can overlap, but no host-runtime speedup is promised.
There is no out-of-order pipeline, per-core compute timing, idle-cycle model,
or cycle-accurate bus contention model.

Each access pays the lookup cost. A miss or S-state write also pays one bus
transaction cost. A miss then pays either a memory read cost or a peer
transfer cost. A dirty peer additionally pays a memory write cost.
A dirty eviction first pays a separate bus transaction and memory write cost.
The dirty-peer flush belongs to the demand transaction, so it does not add
a second arbitration cost. In this conservative timing model, dirty data
forwarding and its memory writeback are serialized, not overlapped.

Thus total cycles equal:

```text
accesses * hit_latency
  + bus_transactions * bus_latency
  + (memory_reads + memory_writes) * memory_latency
  + cache_to_cache_transfers * transfer_latency
```

Per-core cycles charge all work triggered by that core's requests, including
peer writebacks. Their sum equals total cycles. Snoop probe counters count
one lookup in each other cache per demand/upgrade transaction; they add no
extra latency beyond the bus cost. Eviction writebacks do not probe peers.
The model has one cache level per core and shared backing memory, not a
shared L2/L3, network topology, or modeled memory contents.

## Statistics

Text reports include aggregate and per-core misses, cycles, evictions,
invalidations, bus operation counts, transfers, and memory/data traffic.
`--json` additionally exposes separate read/write counters and miss rates,
upgrades, intervention writebacks, final resident-state counts, and all
configuration values. `--events` adds per-access results; without it the
trace is streamed without retaining access history. Threaded mode holds at
most one pending access per core in addition to cache contents. CLI JSON
includes an `execution` object with the mode, worker count, and arbitration
policy.

- A hit means a valid local line was present, so an S-to-M upgrade is a
  **write hit**, not a miss.
- Miss rates are fractions in JSON and percentages in text; empty traces
  have zero rates and zero cycles.
- `bus_writeback` counts standalone dirty-eviction transactions.
  `writebacks` and `memory_writes` also include dirty-owner interventions.
  `transactions` sums `bus_rd`, `bus_rdx`, `bus_upgr`, and `bus_writeback`.
- Per-core `invalidations` count copies invalidated in that core, while
  `intervention_writebacks` count dirty blocks supplied by that core.
- `memory_traffic_bytes` counts all full-block memory reads and writes.
  `data_traffic_bytes` additionally counts peer transfers. Dirty forwarding
  plus memory writeback count as two block movements. Control/address bytes
  are not modeled.
- A lone remaining S copy stays S. Final resident counts include only valid
  lines, not unused capacity or invalidated entries.

For `examples\mesi.trace`, defaults yield 7 accesses, 3 hits, 4 misses,
447 cycles, 1 memory read, 3 memory writes, and 3 peer transfers.
For `examples\lru.trace` with the configuration above, expect 6 accesses,
5 misses, 3 evictions (1 dirty), and 618 cycles.
The four `examples\core*.trace` files together yield 8 accesses, 6 misses,
552 cycles, 3 memory reads, and 2 memory writes.

## Python API

```python
from cache_simulator import CacheConfig, Simulator

sim = Simulator(CacheConfig(cores=4, sets=64, ways=4, block_size=64))
sim.access(0, "W", 0x1000)
result = sim.access(1, "R", 0x1000)
assert result.state_after == "S"
assert sim.state(0, 0x1000) == "S"
report = sim.report()
```

For threaded replay, provide a separate iterable of accesses for each core:

```python
from cache_simulator import CacheConfig, Simulator, TraceAccess, run_parallel

sim = Simulator(CacheConfig(cores=2))
events = run_parallel(sim, [
    [TraceAccess(0, "R", 0x1000), TraceAccess(0, "W", 0x1000)],
    [TraceAccess(1, "R", 0x1000)],
], record_events=True)
assert [event.core for event in events] == [0, 1, 0]
```

`parse_trace(stream, cores=2, source="core0.trace", core_id=0)` parses a
two-column per-core trace lazily. Each iterable passed to `run_parallel`
is read exclusively by its assigned worker and must contain only that
core's accesses. Without `record_events=True`, the return value is an empty
list and no history is retained. The API does not close caller-owned streams.

The API accepts `R` and `W` in uppercase and raises `ValueError` for invalid
requests before mutating simulation state. `state()` observes a block
without changing its LRU position. `access()`, `state()`, and `report()` are
synchronized. Treat the exposed statistics and configuration as read-only;
use `report()` for a consistent snapshot during execution. Direct concurrent
`access()` calls are safe but their ordering depends on host scheduling.
Use `run_parallel()` without external callers for reproducible round-robin
ordering.

## Validation

```powershell
python -m unittest discover -v
```

The standard-library tests cover MESI ownership/sharing/invalidation,
silent E-to-M and M hits, dirty interventions and evictions, LRU and set
mapping, write allocation, exact cycle/traffic accounting, trace errors,
and CLI output. Seeded multi-core traces also check coherence and capacity
invariants after every access across 1-, 2-, 4-, and 8-way caches.
Threaded tests verify distinct core workers, repeatable round-robin ordering,
equivalence to serial replay, unequal and empty trace lengths, synchronized
direct access, and worker/read/startup error cleanup.
