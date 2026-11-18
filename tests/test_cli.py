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
        input=stdin, text=True, capture_output=True, cwd=ROOT, check=False,
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


if __name__ == "__main__":
    unittest.main()
