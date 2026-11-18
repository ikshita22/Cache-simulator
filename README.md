# MESI cache simulator

A dependency-free Python simulator for private, set-associative caches in a
multi-core processor. It implements snooping MESI coherence, per-core LRU
replacement, and write-back/write-allocate caching. Four cores are simulated by
default.

## Requirements and running

Python 3.11 or newer is required. There is no compilation or dependency
installation step. Run these commands from the repository root:

```powershell
python -m cache_simulator examples\mesi.trace
python -m cache_simulator examples\mesi.trace --json --events
python -m cache_simulator examples\lru.trace --sets 1 --ways 2 --block-size 16
python -m cache_simulator examples\private.trace --cores 4 --ways 8
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

This is a deterministic functional simulation of multiple cores, **not
host-thread parallel execution**. The combined trace defines a total order
of atomic accesses. All caches observe that same order through one serialized
snooping bus. Requests, cache lookups, transfers, and memory work do not
overlap; there is no attempt to predict out-of-order core performance or
parallel speedup.

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
trace is streamed without retaining access history.

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

The API accepts `R` and `W` in uppercase and raises `ValueError` for invalid
requests before mutating simulation state. `state()` observes a block
without changing its LRU position. A simulator instance is mutable and
not thread-safe.

## Validation

```powershell
python -m unittest discover -v
```

The standard-library tests cover MESI ownership/sharing/invalidation,
silent E-to-M and M hits, dirty interventions and evictions, LRU and set
mapping, write allocation, exact cycle/traffic accounting, trace errors,
and CLI output. Seeded multi-core traces also check coherence and capacity
invariants after every access across 1-, 2-, 4-, and 8-way caches.
