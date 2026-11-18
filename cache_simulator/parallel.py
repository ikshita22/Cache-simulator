from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from threading import Condition, Event

from .model import AccessResult, Simulator
from .trace import TraceAccess


def run_parallel(
    simulator: Simulator,
    traces: Sequence[Iterable[TraceAccess]],
    *,
    record_events: bool = False,
) -> list[AccessResult]:
    """Run one worker per core with round-robin access arbitration."""
    cores = simulator.config.cores
    if len(traces) != cores:
        raise ValueError(f"expected {cores} per-core traces, got {len(traces)}")

    condition = Condition()
    start = Event()
    active = set(range(cores))
    turn: int | None = 0
    cancelled = False
    results: list[AccessResult] = []

    def next_core(core: int) -> int | None:
        if not active:
            return None
        return min(
            (candidate for candidate in active if candidate > core),
            default=min(active),
        )

    def worker(core: int, trace: Iterable[TraceAccess]) -> None:
        nonlocal turn, cancelled
        start.wait()
        try:
            with condition:
                if cancelled:
                    return
            for access in trace:
                with condition:
                    condition.wait_for(lambda: cancelled or turn == core)
                    if cancelled:
                        return
                    if access.core != core:
                        raise ValueError(
                            f"trace for core {core} contains an access for core {access.core}"
                        )
                    result = simulator.access(core, access.operation, access.address)
                    if record_events:
                        results.append(result)
                    turn = next_core(core)
                    condition.notify_all()

            with condition:
                active.remove(core)
                if turn == core:
                    turn = next_core(core)
                condition.notify_all()
        except BaseException:
            with condition:
                cancelled = True
                condition.notify_all()
            raise

    with ThreadPoolExecutor(max_workers=cores, thread_name_prefix="mesi-core") as pool:
        try:
            futures = [
                pool.submit(worker, core, trace) for core, trace in enumerate(traces)
            ]
            start.set()
            for future in futures:
                future.result()
        except BaseException:
            with condition:
                cancelled = True
                condition.notify_all()
            raise
        finally:
            start.set()
    return results
