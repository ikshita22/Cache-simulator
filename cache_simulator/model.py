from collections import OrderedDict
from dataclasses import asdict, dataclass
from enum import Enum


class State(str, Enum):
    INVALID = "I"
    SHARED = "S"
    EXCLUSIVE = "E"
    MODIFIED = "M"


def _positive_integer(name: str, value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class CacheConfig:
    sets: int = 64
    ways: int = 4
    block_size: int = 64
    cores: int = 4
    hit_latency: int = 1
    bus_latency: int = 2
    memory_latency: int = 100
    transfer_latency: int = 10

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            _positive_integer(name, value)
        for name in ("sets", "block_size"):
            value = getattr(self, name)
            if value & (value - 1):
                raise ValueError(f"{name} must be a power of two")
        if self.sets * self.block_size > 1 << 64:
            raise ValueError("sets * block_size must fit in the 64-bit address space")


@dataclass
class CoreStats:
    reads: int = 0
    writes: int = 0
    read_hits: int = 0
    write_hits: int = 0
    read_misses: int = 0
    write_misses: int = 0
    upgrades: int = 0
    evictions: int = 0
    dirty_evictions: int = 0
    invalidations: int = 0
    intervention_writebacks: int = 0
    cycles: int = 0

    def report(self) -> dict[str, int | float]:
        accesses = self.reads + self.writes
        misses = self.read_misses + self.write_misses
        return {
            **asdict(self),
            "accesses": accesses,
            "hits": self.read_hits + self.write_hits,
            "misses": misses,
            "miss_rate": misses / accesses if accesses else 0.0,
            "read_miss_rate": self.read_misses / self.reads if self.reads else 0.0,
            "write_miss_rate": self.write_misses / self.writes if self.writes else 0.0,
        }


@dataclass
class BusStats:
    bus_rd: int = 0
    bus_rdx: int = 0
    bus_upgr: int = 0
    bus_writeback: int = 0
    snoop_probes: int = 0
    invalidations: int = 0
    memory_reads: int = 0
    memory_writes: int = 0
    cache_to_cache_transfers: int = 0


@dataclass(frozen=True)
class AccessResult:
    core: int
    operation: str
    address: int
    block_address: int
    hit: bool
    state_before: State
    state_after: State
    bus_transaction: str | None
    supplier: int | None
    invalidated_cores: tuple[int, ...]
    evicted_block: int | None
    writebacks: int
    cycles: int
    start_cycle: int
    end_cycle: int


class Simulator:
    """Atomic trace accesses on a serialized snooping bus."""

    def __init__(self, config: CacheConfig | None = None) -> None:
        self.config = config if config is not None else CacheConfig()
        self.core_stats = [CoreStats() for _ in range(self.config.cores)]
        self.bus = BusStats()
        self.cycles = 0
        self._caches: list[dict[int, OrderedDict[int, State]]] = [
            {} for _ in range(self.config.cores)
        ]

    def _validate_access(self, core: int, address: int) -> None:
        if type(core) is not int or not 0 <= core < self.config.cores:
            raise ValueError(f"core must be between 0 and {self.config.cores - 1}")
        if type(address) is not int or not 0 <= address < 1 << 64:
            raise ValueError("address must be an unsigned 64-bit integer")

    def _location(self, address: int) -> tuple[int, int]:
        block = address // self.config.block_size
        return block, block % self.config.sets

    def state(self, core: int, address: int) -> State:
        self._validate_access(core, address)
        block, index = self._location(address)
        return self._caches[core].get(index, {}).get(block, State.INVALID)

    def access(self, core: int, operation: str, address: int) -> AccessResult:
        self._validate_access(core, address)
        if operation not in ("R", "W"):
            raise ValueError("operation must be R or W")

        config = self.config
        stats = self.core_stats[core]
        block, index = self._location(address)
        lines = self._caches[core].setdefault(index, OrderedDict())
        before = lines.get(block, State.INVALID)
        hit = before != State.INVALID
        cycles = config.hit_latency
        start = self.cycles
        writes_before = self.bus.memory_writes
        transaction = None
        supplier = None
        invalidated: list[int] = []
        evicted = None

        if operation == "R":
            stats.reads += 1
            if hit:
                stats.read_hits += 1
            else:
                stats.read_misses += 1
        else:
            stats.writes += 1
            if hit:
                stats.write_hits += 1
            else:
                stats.write_misses += 1

        if not hit:
            if len(lines) == config.ways:
                victim, victim_state = lines.popitem(last=False)
                evicted = victim * config.block_size
                stats.evictions += 1
                if victim_state == State.MODIFIED:
                    stats.dirty_evictions += 1
                    self.bus.bus_writeback += 1
                    self.bus.memory_writes += 1
                    cycles += config.bus_latency + config.memory_latency

            transaction = "BusRd" if operation == "R" else "BusRdX"
            if operation == "R":
                self.bus.bus_rd += 1
            else:
                self.bus.bus_rdx += 1
            cycles += config.bus_latency
            peers = self._peers(core, block, index)
            if peers:
                supplier = peers[0][0]
                self.bus.cache_to_cache_transfers += 1
                cycles += config.transfer_latency
            else:
                self.bus.memory_reads += 1
                cycles += config.memory_latency

            for peer, peer_lines, peer_state in peers:
                if peer_state == State.MODIFIED:
                    self.core_stats[peer].intervention_writebacks += 1
                    self.bus.memory_writes += 1
                    cycles += config.memory_latency
                if operation == "R":
                    peer_lines[block] = State.SHARED
                else:
                    del peer_lines[block]
                    self.core_stats[peer].invalidations += 1
                    self.bus.invalidations += 1
                    invalidated.append(peer)

            lines[block] = (
                State.MODIFIED
                if operation == "W"
                else State.SHARED if peers else State.EXCLUSIVE
            )
        elif operation == "W":
            if before == State.SHARED:
                transaction = "BusUpgr"
                stats.upgrades += 1
                self.bus.bus_upgr += 1
                cycles += config.bus_latency
                for peer, peer_lines, _ in self._peers(core, block, index):
                    del peer_lines[block]
                    self.core_stats[peer].invalidations += 1
                    self.bus.invalidations += 1
                    invalidated.append(peer)
            lines[block] = State.MODIFIED

        lines.move_to_end(block)
        self.cycles += cycles
        stats.cycles += cycles
        return AccessResult(
            core=core,
            operation=operation,
            address=address,
            block_address=block * config.block_size,
            hit=hit,
            state_before=before,
            state_after=lines[block],
            bus_transaction=transaction,
            supplier=supplier,
            invalidated_cores=tuple(invalidated),
            evicted_block=evicted,
            writebacks=self.bus.memory_writes - writes_before,
            cycles=cycles,
            start_cycle=start,
            end_cycle=self.cycles,
        )

    def _peers(
        self, core: int, block: int, index: int
    ) -> list[tuple[int, OrderedDict[int, State], State]]:
        peers = []
        for peer, cache in enumerate(self._caches):
            if peer == core:
                continue
            self.bus.snoop_probes += 1
            lines = cache.get(index)
            if lines is not None and block in lines:
                peers.append((peer, lines, lines[block]))
        return peers

    def report(self) -> dict[str, object]:
        cores = [stats.report() for stats in self.core_stats]
        accesses = sum(stats.reads + stats.writes for stats in self.core_stats)
        misses = sum(stats.read_misses + stats.write_misses for stats in self.core_stats)
        memory_blocks = self.bus.memory_reads + self.bus.memory_writes
        transactions = (
            self.bus.bus_rd + self.bus.bus_rdx
            + self.bus.bus_upgr + self.bus.bus_writeback
        )
        resident = {state.value: 0 for state in State if state != State.INVALID}
        for cache in self._caches:
            for lines in cache.values():
                for state in lines.values():
                    resident[state.value] += 1
        return {
            "config": {
                **asdict(self.config),
                "bytes_per_core": (
                    self.config.sets * self.config.ways * self.config.block_size
                ),
            },
            "summary": {
                "accesses": accesses,
                "hits": accesses - misses,
                "misses": misses,
                "miss_rate": misses / accesses if accesses else 0.0,
                "cycles": self.cycles,
                "average_access_cycles": self.cycles / accesses if accesses else 0.0,
                "resident_lines": resident,
            },
            "bus": {
                **asdict(self.bus),
                "transactions": transactions,
                "writebacks": self.bus.memory_writes,
                "memory_traffic_bytes": memory_blocks * self.config.block_size,
                "data_traffic_bytes": (
                    memory_blocks + self.bus.cache_to_cache_transfers
                ) * self.config.block_size,
            },
            "cores": cores,
        }
