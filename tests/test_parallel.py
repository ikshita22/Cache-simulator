from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
import random
from threading import Barrier, Lock, current_thread, get_ident
import unittest
from unittest.mock import patch

from cache_simulator import CacheConfig, Simulator, State, TraceAccess, run_parallel


class ParallelTests(unittest.TestCase):
    def test_trace_readers_and_accesses_use_distinct_core_workers(self) -> None:
        ready = Barrier(4, timeout=5)
        readers = {}
        writers = {}

        class RecordingSimulator(Simulator):
            def access(self, core, operation, address):
                writers[core] = (get_ident(), current_thread().name)
                return super().access(core, operation, address)

        def trace(core):
            readers[core] = get_ident()
            ready.wait()
            yield TraceAccess(core, "R", core * 64)

        sim = RecordingSimulator()
        events = run_parallel(sim, [trace(core) for core in range(4)], record_events=True)
        self.assertEqual(len(set(readers.values())), 4)
        self.assertNotIn(get_ident(), readers.values())
        self.assertEqual([event.core for event in events], [0, 1, 2, 3])
        for core in range(4):
            self.assertEqual(writers[core][0], readers[core])
            self.assertTrue(writers[core][1].startswith("mesi-core"))

    def test_round_robin_matches_serial_replay_with_unequal_traces(self) -> None:
        rng = random.Random(183)
        for cores in (1, 2, 4, 8):
            for ways in (1, 2, 4):
                with self.subTest(cores=cores, ways=ways):
                    config = CacheConfig(cores=cores, sets=2, ways=ways, block_size=16)
                    traces = [
                        [
                            TraceAccess(core, rng.choice(("R", "W")), rng.randrange(256))
                            for _ in range((core * 13 + 19) % 37)
                        ]
                        for core in range(cores)
                    ]
                    expected = Simulator(config)
                    ordered = [
                        access for row in zip_longest(*traces)
                        for access in row if access is not None
                    ]
                    expected_events = [expected.access(a.core, a.operation, a.address)
                                       for a in ordered]
                    for _ in range(3):
                        actual = Simulator(config)
                        events = run_parallel(
                            actual, [iter(trace) for trace in traces], record_events=True
                        )
                        self.assertEqual(events, expected_events)
                        self.assertEqual(actual.report(), expected.report())
                        for core in range(cores):
                            for address in range(0, 256, 16):
                                self.assertEqual(
                                    actual.state(core, address),
                                    expected.state(core, address),
                                )

    def test_empty_cores_are_skipped_and_history_is_optional(self) -> None:
        sim = Simulator()
        events = run_parallel(sim, [
            [], [TraceAccess(1, "W", 0), TraceAccess(1, "R", 0)],
            [], [TraceAccess(3, "R", 0)],
        ], record_events=True)
        self.assertEqual([event.core for event in events], [1, 3, 1])
        self.assertEqual([event.state_after for event in events],
                         [State.MODIFIED, State.SHARED, State.SHARED])
        empty = Simulator()
        self.assertEqual(run_parallel(empty, [[], [], [], []]), [])
        self.assertEqual(empty.cycles, 0)
        self.assertEqual(run_parallel(sim, [
            [TraceAccess(0, "R", 64)], [], [], [],
        ]), [])
        self.assertEqual(sim.report()["summary"]["accesses"], 4)

    def test_incorrect_trace_count_does_not_change_simulator(self) -> None:
        sim = Simulator()
        original = sim.report()
        with self.assertRaisesRegex(ValueError, "expected 4 per-core traces, got 1"):
            run_parallel(sim, [[]])
        self.assertEqual(sim.report(), original)

    def test_model_exception_cancels_waiters_and_is_propagated(self) -> None:
        sim = Simulator()
        with self.assertRaisesRegex(ValueError, "operation must be R or W"):
            run_parallel(sim, [
                [TraceAccess(0, "R", 0)] * 10,
                [TraceAccess(1, "X", 0)],
                [TraceAccess(2, "W", 0)] * 10,
                [],
            ])
        self.assertEqual(run_parallel(sim, [[], [], [], []]), [])

    def test_wrong_core_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "trace for core 0"):
            run_parallel(Simulator(), [
                [TraceAccess(1, "R", 0)], [], [], [],
            ])

    def test_reader_failure_propagates_after_workers_exit(self) -> None:
        workers = {}
        ready = Barrier(4, timeout=5)

        def trace(core):
            workers[core] = current_thread()
            ready.wait()
            if core == 2:
                raise OSError("trace read failed")
            yield TraceAccess(core, "R", 0)

        with self.assertRaisesRegex(OSError, "trace read failed"):
            run_parallel(Simulator(), [trace(core) for core in range(4)])
        self.assertEqual(len(workers), 4)
        self.assertFalse(any(worker.is_alive() for worker in workers.values()))

    def test_thread_start_failure_releases_already_started_worker(self) -> None:
        original = ThreadPoolExecutor.submit
        submissions = 0

        def submit(pool, *args, **kwargs):
            nonlocal submissions
            submissions += 1
            if submissions == 2:
                raise RuntimeError("thread start failed")
            return original(pool, *args, **kwargs)

        sim = Simulator()
        with patch.object(ThreadPoolExecutor, "submit", submit):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                run_parallel(sim, [
                    [TraceAccess(core, "W", 0)] * 2 for core in range(4)
                ])
        self.assertEqual(sim.cycles, 0)

    def test_simulator_supports_direct_concurrent_access(self) -> None:
        sim = Simulator()
        ready = Barrier(4, timeout=5)
        results = []
        result_lock = Lock()

        def worker(core):
            ready.wait()
            local = [sim.access(core, "W", 0) for _ in range(100)]
            with result_lock:
                results.extend(local)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))
        ordered = sorted(results, key=lambda event: event.start_cycle)
        self.assertEqual(len(ordered), 400)
        self.assertEqual(ordered[0].start_cycle, 0)
        self.assertEqual([event.end_cycle for event in ordered[:-1]],
                         [event.start_cycle for event in ordered[1:]])
        self.assertEqual(ordered[-1].end_cycle, sim.cycles)
        self.assertEqual(sim.report()["summary"]["accesses"], 400)
        self.assertEqual(sum(sim.state(core, 0) == State.MODIFIED for core in range(4)), 1)


if __name__ == "__main__":
    unittest.main()
