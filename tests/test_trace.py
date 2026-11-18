import unittest

from cache_simulator import TraceAccess, TraceError, parse_trace


class TraceTests(unittest.TestCase):
    def test_decimal_hex_comments_and_case(self) -> None:
        accesses = list(parse_trace([
            "# heading\n", "\n", "0 r 0xFF # load\n",
            "  3 W 0016\t\n", "1 R 0Xffffffffffffffff\n",
        ]))
        self.assertEqual(accesses, [
            TraceAccess(0, "R", 255), TraceAccess(3, "W", 16),
            TraceAccess(1, "R", (1 << 64) - 1),
        ])

    def test_malformed_records_include_source_and_line(self) -> None:
        for record in (
            "0 R", "0 R 0 extra", "0 X 0", "4 R 0", "-1 R 0", "x R 0",
            "0 R -1", "0 R 0x", "0 R ff", "0 R 0b10", "0 R 1_000",
            "0 R +1", "0 R 18446744073709551616",
        ):
            with self.subTest(record=record):
                with self.assertRaisesRegex(TraceError, "sample.trace:3:"):
                    list(parse_trace(["# comment", "", record], source="sample.trace"))

    def test_configured_core_count(self) -> None:
        self.assertEqual(list(parse_trace(["7 W 0"], cores=8)), [TraceAccess(7, "W", 0)])
        with self.assertRaises(TraceError):
            list(parse_trace(["1 W 0"], cores=1))
        with self.assertRaises(ValueError):
            list(parse_trace([], cores=0))


if __name__ == "__main__":
    unittest.main()
