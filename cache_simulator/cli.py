import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import TextIO

from .model import AccessResult, CacheConfig, Simulator
from .trace import parse_trace


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate private write-back caches with snooping MESI coherence."
    )
    parser.add_argument("trace", help="trace file, or - for standard input")
    defaults = CacheConfig()
    for name in (
        "cores", "sets", "ways", "block_size", "hit_latency",
        "bus_latency", "memory_latency", "transfer_latency",
    ):
        parser.add_argument(
            f"--{name.replace('_', '-')}",
            type=int,
            default=getattr(defaults, name),
            help=f"{name.replace('_', ' ')} (default: %(default)s)",
        )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument(
        "--events", action="store_true",
        help="include each access and its coherence result",
    )
    return parser


def _run(
    stream: TextIO, simulator: Simulator, source: str, events: bool
) -> list[AccessResult]:
    results = []
    for access in parse_trace(stream, simulator.config.cores, source):
        result = simulator.access(access.core, access.operation, access.address)
        if events:
            results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = CacheConfig(**{
            name: getattr(args, name) for name in CacheConfig.__dataclass_fields__
        })
        simulator = Simulator(config)
        if args.trace == "-":
            results = _run(sys.stdin, simulator, "<stdin>", args.events)
        else:
            with Path(args.trace).open(encoding="utf-8") as stream:
                results = _run(stream, simulator, args.trace, args.events)
    except (ValueError, OSError) as error:
        parser.error(str(error))

    report = simulator.report()
    if args.json:
        if args.events:
            report["events"] = [asdict(result) for result in results]
        print(json.dumps(report, indent=2))
    else:
        if args.events:
            for result in results:
                outcome = "hit" if result.hit else "miss"
                print(
                    f"core {result.core} {result.operation} {result.address:#x}: "
                    f"{outcome} {result.state_before.value}->{result.state_after.value} "
                    f"{result.bus_transaction or '-'} "
                    f"cycles={result.cycles} end={result.end_cycle}"
                )
        print("MESI cache simulator (serialized trace order)")
        print(
            f"{config.cores} cores | {config.sets} sets | {config.ways}-way | "
            f"{config.block_size} B blocks | "
            f"{config.sets * config.ways * config.block_size} B/core"
        )
        accesses = sum(s.reads + s.writes for s in simulator.core_stats)
        misses = sum(s.read_misses + s.write_misses for s in simulator.core_stats)
        print(f"Accesses: {accesses} | Hits: {accesses - misses} | Misses: {misses}")
        print(f"Miss rate: {misses / accesses if accesses else 0:.2%}")
        print(
            f"Cycles: {simulator.cycles} | Average access cycles: "
            f"{simulator.cycles / accesses if accesses else 0:.2f}"
        )
        for core, stats in enumerate(simulator.core_stats):
            local = stats.report()
            print(
                f"Core {core}: accesses={local['accesses']} "
                f"misses={local['misses']} miss_rate={local['miss_rate']:.2%} "
                f"cycles={stats.cycles} evictions={stats.evictions} "
                f"dirty_evictions={stats.dirty_evictions} "
                f"invalidations={stats.invalidations}"
            )
        bus = simulator.bus
        print(
            f"Bus: BusRd={bus.bus_rd} BusRdX={bus.bus_rdx} "
            f"BusUpgr={bus.bus_upgr} Writeback={bus.bus_writeback} "
            f"snoop_probes={bus.snoop_probes} invalidations={bus.invalidations}"
        )
        print(
            f"Memory: reads={bus.memory_reads} writes={bus.memory_writes} | "
            f"Cache-to-cache transfers: {bus.cache_to_cache_transfers}"
        )
        print(
            f"Traffic: memory={(bus.memory_reads + bus.memory_writes) * config.block_size} B "
            f"data={(bus.memory_reads + bus.memory_writes + bus.cache_to_cache_transfers) * config.block_size} B"
        )
    return 0
