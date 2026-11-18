import random
import unittest

from cache_simulator import CacheConfig, Simulator, State


class CoherenceTests(unittest.TestCase):
    def test_mesi_sequence_and_exact_statistics(self) -> None:
        sim = Simulator()
        operations = [
            (0, "R", 0x1000), (0, "R", 0x1008), (0, "W", 0x1000),
            (1, "R", 0x1000), (1, "W", 0x1000), (2, "W", 0x1000),
            (3, "R", 0x1000),
        ]
        results = [sim.access(*operation) for operation in operations]
        self.assertEqual(
            [r.state_after for r in results],
            [State.EXCLUSIVE, State.EXCLUSIVE, State.MODIFIED, State.SHARED,
             State.MODIFIED, State.MODIFIED, State.SHARED],
        )
        self.assertEqual([r.cycles for r in results], [103, 1, 1, 113, 3, 113, 113])
        self.assertEqual(
            [r.bus_transaction for r in results],
            ["BusRd", None, None, "BusRd", "BusUpgr", "BusRdX", "BusRd"],
        )
        self.assertEqual(results[3].supplier, 0)
        self.assertEqual(results[4].invalidated_cores, (0,))
        self.assertEqual(results[5].invalidated_cores, (1,))
        self.assertEqual(
            [sim.state(core, 0x1000) for core in range(4)],
            [State.INVALID, State.INVALID, State.SHARED, State.SHARED],
        )
        report = sim.report()
        summary = report["summary"]
        self.assertEqual(summary["accesses"], 7)
        self.assertEqual(summary["hits"], 3)
        self.assertEqual(summary["misses"], 4)
        self.assertEqual(summary["cycles"], 447)
        self.assertAlmostEqual(summary["miss_rate"], 4 / 7)
        self.assertEqual(summary["resident_lines"], {"S": 2, "E": 0, "M": 0})
        self.assertEqual(report["bus"], {
            "bus_rd": 3,
            "bus_rdx": 1,
            "bus_upgr": 1,
            "bus_writeback": 0,
            "snoop_probes": 15,
            "invalidations": 2,
            "memory_reads": 1,
            "memory_writes": 3,
            "cache_to_cache_transfers": 3,
            "transactions": 5,
            "writebacks": 3,
            "memory_traffic_bytes": 256,
            "data_traffic_bytes": 448,
        })
        self.assertEqual([c["cycles"] for c in report["cores"]], [105, 116, 113, 113])
        self.assertEqual([r.writebacks for r in results], [0, 0, 0, 1, 0, 1, 1])
        self.assertEqual([r.start_cycle for r in results], [0, 103, 104, 105, 218, 221, 334])
        self.assertEqual(results[-1].end_cycle, 447)

    def test_clean_sharing_then_upgrade_invalidates_all_sharers(self) -> None:
        sim = Simulator()
        for core in range(4):
            sim.access(core, "R", 0)
        for core in range(4):
            self.assertEqual(sim.state(core, 0), State.SHARED)
        self.assertEqual(sim.bus.memory_reads, 1)
        self.assertEqual(sim.bus.memory_writes, 0)
        self.assertEqual(sim.bus.cache_to_cache_transfers, 3)
        read = sim.access(0, "R", 4)
        self.assertTrue(read.hit)
        self.assertEqual(read.cycles, 1)
        write = sim.access(0, "W", 8)
        self.assertTrue(write.hit)
        self.assertEqual(write.invalidated_cores, (1, 2, 3))
        self.assertEqual(write.cycles, 3)
        self.assertEqual(sim.bus.memory_writes, 0)

    def test_write_miss_takes_exclusive_clean_copy(self) -> None:
        sim = Simulator()
        sim.access(0, "R", 0)
        write = sim.access(1, "W", 0)
        self.assertEqual(write.bus_transaction, "BusRdX")
        self.assertEqual(write.supplier, 0)
        self.assertEqual(write.cycles, 13)
        self.assertEqual(write.writebacks, 0)
        self.assertEqual(sim.state(0, 0), State.INVALID)
        self.assertEqual(sim.state(1, 0), State.MODIFIED)

    def test_modified_hits_and_dirty_data_stay_cached_at_end(self) -> None:
        sim = Simulator()
        sim.access(0, "W", 0)
        read = sim.access(0, "R", 1)
        write = sim.access(0, "W", 63)
        self.assertEqual(read.state_after, State.MODIFIED)
        self.assertTrue(write.hit)
        self.assertIsNone(write.bus_transaction)
        self.assertEqual(sim.bus.memory_writes, 0)
        self.assertEqual(sim.report()["summary"]["resident_lines"]["M"], 1)

    def test_sole_remaining_shared_copy_still_upgrades(self) -> None:
        sim = Simulator(CacheConfig(sets=1, ways=1))
        sim.access(0, "R", 0)
        sim.access(1, "R", 0)
        sim.access(1, "R", 64)
        self.assertEqual(sim.state(0, 0), State.SHARED)
        result = sim.access(0, "W", 0)
        self.assertEqual(result.bus_transaction, "BusUpgr")
        self.assertEqual(result.invalidated_cores, ())

    def test_dirty_victim_and_dirty_supplier_are_both_charged(self) -> None:
        sim = Simulator(CacheConfig(sets=1, ways=1))
        sim.access(0, "W", 0)
        sim.access(1, "W", 64)
        result = sim.access(0, "W", 64)
        self.assertEqual(result.evicted_block, 0)
        self.assertEqual(result.writebacks, 2)
        self.assertEqual(result.cycles, 215)
        self.assertEqual(sim.bus.bus_writeback, 1)
        self.assertEqual(sim.bus.memory_writes, 2)
        self.assertEqual(sim.core_stats[0].dirty_evictions, 1)
        self.assertEqual(sim.core_stats[1].intervention_writebacks, 1)


class ReplacementTests(unittest.TestCase):
    def test_lru_writeback_and_write_allocate(self) -> None:
        sim = Simulator(CacheConfig(sets=1, ways=2, block_size=16))
        results = [
            sim.access(0, operation, address)
            for operation, address in [
                ("W", 0), ("R", 16), ("R", 4),
                ("R", 32), ("R", 48), ("R", 0),
            ]
        ]
        self.assertTrue(results[2].hit)
        self.assertEqual([r.evicted_block for r in results], [None, None, None, 16, 0, 32])
        self.assertEqual(sim.core_stats[0].evictions, 3)
        self.assertEqual(sim.core_stats[0].dirty_evictions, 1)
        self.assertEqual(sim.bus.memory_reads, 5)
        self.assertEqual(sim.bus.memory_writes, 1)
        self.assertEqual(sim.bus.bus_writeback, 1)
        self.assertEqual(sim.cycles, 618)

    def test_snooping_does_not_refresh_local_lru(self) -> None:
        sim = Simulator(CacheConfig(sets=1, ways=2))
        sim.access(0, "R", 0)
        sim.access(0, "R", 64)
        sim.access(1, "R", 0)
        result = sim.access(0, "R", 128)
        self.assertEqual(result.evicted_block, 0)
        self.assertEqual(sim.state(0, 64), State.EXCLUSIVE)

    def test_invalidation_frees_a_way_without_eviction(self) -> None:
        sim = Simulator(CacheConfig(sets=1, ways=2))
        sim.access(0, "R", 0)
        sim.access(0, "R", 64)
        sim.access(1, "W", 0)
        result = sim.access(0, "R", 128)
        self.assertIsNone(result.evicted_block)
        self.assertEqual(sim.core_stats[0].evictions, 0)

    def test_set_mapping_and_block_offsets(self) -> None:
        sim = Simulator(CacheConfig(sets=2, ways=1, block_size=16))
        sim.access(0, "R", 0)
        sim.access(0, "R", 16)
        self.assertTrue(sim.access(0, "R", 15).hit)
        result = sim.access(0, "R", 32)
        self.assertEqual(result.evicted_block, 0)
        self.assertEqual(sim.state(0, 16), State.EXCLUSIVE)
        self.assertEqual(sim.state(0, 31), State.EXCLUSIVE)
        self.assertEqual(sim.state(0, 0), State.INVALID)


class ValidationTests(unittest.TestCase):
    def test_invalid_configurations(self) -> None:
        for field in CacheConfig.__dataclass_fields__:
            for value in (0, -1, True, 1.5, "4"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        CacheConfig(**{field: value})
        for kwargs in ({"sets": 3}, {"block_size": 24}, {"sets": 1 << 64}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    CacheConfig(**kwargs)

    def test_invalid_access_does_not_mutate_statistics(self) -> None:
        sim = Simulator()
        original = sim.report()
        for core, operation, address in [
            (-1, "R", 0), (4, "W", 0), (True, "R", 0), (0, "X", 0),
            (0, "R", -1), (0, "R", 1 << 64), (0, "R", 1.5), (0, "R", False),
        ]:
            with self.subTest(core=core, operation=operation, address=address):
                with self.assertRaises(ValueError):
                    sim.access(core, operation, address)
                self.assertEqual(sim.report(), original)

    def test_empty_and_single_core_reports(self) -> None:
        sim = Simulator(CacheConfig(cores=1))
        report = sim.report()
        self.assertEqual(report["summary"]["miss_rate"], 0)
        self.assertEqual(report["summary"]["average_access_cycles"], 0)
        self.assertEqual(report["cores"][0]["read_miss_rate"], 0)
        self.assertEqual(report["cores"][0]["write_miss_rate"], 0)
        sim.access(0, "R", (1 << 64) - 1)
        self.assertEqual(sim.bus.snoop_probes, 0)
        self.assertEqual(sim.state(0, (1 << 64) - 1), State.EXCLUSIVE)

    def test_seeded_traces_preserve_coherence_capacity_and_accounting(self) -> None:
        rng = random.Random(731)
        for ways in (1, 2, 4, 8):
            config = CacheConfig(
                sets=2, ways=ways, block_size=16, cores=4,
                hit_latency=2, bus_latency=3, memory_latency=17, transfer_latency=5,
            )
            sim = Simulator(config)
            for _ in range(500):
                sim.access(rng.randrange(4), rng.choice(("R", "W")), rng.randrange(512))
                for block in range(32):
                    states = [sim.state(core, block * 16) for core in range(4)]
                    valid = [state for state in states if state != State.INVALID]
                    if len(valid) > 1:
                        self.assertTrue(all(state == State.SHARED for state in valid))
                for core in range(4):
                    for index in range(2):
                        count = sum(
                            sim.state(core, block * 16) != State.INVALID
                            for block in range(index, 32, 2)
                        )
                        self.assertLessEqual(count, ways)
            report = sim.report()
            bus = report["bus"]
            cores = report["cores"]
            self.assertEqual(report["summary"]["accesses"], 500)
            self.assertEqual(report["summary"]["hits"] + report["summary"]["misses"], 500)
            self.assertEqual(bus["bus_rd"] + bus["bus_rdx"], report["summary"]["misses"])
            self.assertEqual(bus["memory_reads"] + bus["cache_to_cache_transfers"],
                             report["summary"]["misses"])
            self.assertEqual(bus["invalidations"], sum(c["invalidations"] for c in cores))
            self.assertEqual(bus["memory_writes"], sum(
                c["dirty_evictions"] + c["intervention_writebacks"] for c in cores
            ))
            self.assertEqual(sim.cycles, sum(c["cycles"] for c in cores))
            self.assertEqual(
                sim.cycles,
                500 * config.hit_latency + bus["transactions"] * config.bus_latency
                + (bus["memory_reads"] + bus["memory_writes"]) * config.memory_latency
                + bus["cache_to_cache_transfers"] * config.transfer_latency,
            )


if __name__ == "__main__":
    unittest.main()
