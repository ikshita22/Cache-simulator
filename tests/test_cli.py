import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cache_simulator", *args],
        input=stdin, text=True, capture_output=True, cwd=ROOT, check=False, timeout=15,
    )


class CliTests(unittest.TestCase):
    def test_documented_sample_json_and_events(self) -> None:
        result = run_cli(str(ROOT / "examples" / "mesi.trace"), "--json", "--events")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"]["cycles"], 447)
        self.assertEqual(report["summary"]["misses"], 4)
        self.assertEqual(len(report["events"]), 7)
        self.assertEqual(report["events"][4]["invalidated_cores"], [0])
        self.assertEqual(report["events"][0]["state_after"], "E")

    def test_text_output_and_custom_configuration(self) -> None:
        result = run_cli(
            str(ROOT / "examples" / "lru.trace"),
            "--sets", "1", "--ways", "2", "--block-size", "16", "--events",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Cycles: 618", result.stdout)
        self.assertIn("dirty_evictions=1", result.stdout)
        self.assertIn("miss I->M BusRdX", result.stdout)

    def test_stdin_and_custom_timing(self) -> None:
        result = run_cli(
            "-", "--json", "--cores", "1", "--hit-latency", "2",
            "--bus-latency", "3", "--memory-latency", "7",
            stdin="0 R 0\n0 W 1\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"]["cycles"], 14)
        self.assertNotIn("events", report)

    def test_empty_input_is_valid(self) -> None:
        result = run_cli("-", "--json", stdin="# no accesses\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["summary"]["accesses"], 0)

    def test_invalid_inputs_exit_nonzero_without_partial_report(self) -> None:
        for args, stdin, message in [
            (["-", "--sets", "3"], "", "sets must be a power of two"),
            (["-", "--ways", "0"], "", "ways must be a positive integer"),
            (["-"], "0 R 0\n4 W 0\n", "<stdin>:2:"),
            (["-"], "0 W nope\n", "<stdin>:1:"),
            ([str(ROOT / "does-not-exist.trace")], None, "error:"),
        ]:
            with self.subTest(args=args, stdin=stdin):
                result = run_cli(*args, stdin=stdin)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn(message, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_utf8_has_a_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.trace"
            path.write_bytes(b"\xff")
            result = run_cli(str(path))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("decode", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_threaded_examples_match_round_robin_trace(self) -> None:
        paths = [str(ROOT / "examples" / f"core{core}.trace") for core in range(4)]
        result = run_cli("--core-traces", *paths, "--json", "--events")
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        serial = run_cli("-", "--json", "--events", stdin=(
            "0 R 0x1000\n1 R 0x1000\n2 R 0x2000\n3 W 0x3000\n"
            "0 W 0x1000\n1 W 0x1000\n2 R 0x2008\n3 R 0x1000\n"
        ))
        self.assertEqual(serial.returncode, 0, serial.stderr)
        expected = json.loads(serial.stdout)
        self.assertEqual(actual.pop("execution"), {
            "mode": "threaded", "workers": 4, "arbitration": "round-robin",
        })
        expected.pop("execution")
        self.assertEqual(actual, expected)
        text = run_cli("--core-traces", *paths)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("threaded cores, round-robin arbitration", text.stdout)
        self.assertIn("Cycles: 552", text.stdout)

    def test_threaded_invalid_arguments_and_worker_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good.trace"
            bad = Path(directory) / "bad.trace"
            invalid_utf8 = Path(directory) / "invalid.trace"
            good.write_text("R 0\nW 0\n" * 50, encoding="utf-8")
            bad.write_text("# comment\nR invalid\n", encoding="utf-8")
            invalid_utf8.write_bytes(b"\xff")
            for args, message in [
                ([], "provide either"),
                (["-", "--core-traces", str(good)], "provide either"),
                (["--core-traces", str(good)], "expected 4 per-core traces"),
                (["--core-traces", "-", "-", "-", "-"], "requires files"),
                (["--core-traces", str(good), str(bad), str(good), str(good)],
                 f"{bad}:2:"),
                (["--core-traces", str(good), str(invalid_utf8), str(good), str(good)],
                 "decode"),
                (["--core-traces", str(good), str(Path(directory) / "missing"),
                  str(good), str(good)], "error:"),
            ]:
                with self.subTest(args=args):
                    result = run_cli(*args)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(message, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_threaded_single_core_and_empty_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "core.trace"
            path.write_text("# empty\n", encoding="utf-8")
            result = run_cli("--cores", "1", "--core-traces", str(path), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["summary"]["cycles"], 0)
            self.assertNotIn("events", report)
            path.write_text("W 0\nR 0\n", encoding="utf-8")
            result = run_cli("--cores", "1", "--core-traces", str(path), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["summary"]["cycles"], 104)


if __name__ == "__main__":
    unittest.main()
