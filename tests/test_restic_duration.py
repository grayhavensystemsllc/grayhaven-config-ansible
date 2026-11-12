"""Unit tests for duration calculation and the managed Restic shell functions."""

from __future__ import annotations

import contextlib
from decimal import Decimal
import io
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.helpers import REPO_ROOT, load_program


duration = load_program(
    "grayhaven_restic_duration", "roles/restic_backup/files/grayhaven-restic-duration"
)
HELPER = REPO_ROOT / "roles/restic_backup/files/grayhaven-restic-duration"


class ResticDurationTests(unittest.TestCase):
    def test_elapsed_seconds_preserves_fractional_precision(self):
        for start in (0, 51_000_000_000, 9_000_000_000_000_000_000):
            for elapsed, expected in (
                (0, "0.000000000"),
                (1, "0.000000001"),
                (739_243_000, "0.739243000"),
                (1_000_000_000, "1.000000000"),
                (237_812_345_678, "237.812345678"),
            ):
                with (
                    self.subTest(start=start, elapsed=elapsed),
                    mock.patch.object(
                        duration.time, "monotonic_ns", return_value=start + elapsed
                    ),
                    mock.patch.object(
                        duration.time, "time_ns", side_effect=AssertionError
                    ),
                ):
                    self.assertEqual(duration.elapsed_seconds(start), expected)

    def test_invalid_clock_boundaries_are_rejected(self):
        for start, now in ((-1, 10), (11, 10)):
            with (
                self.subTest(start=start, now=now),
                mock.patch.object(duration.time, "monotonic_ns", return_value=now),
                self.assertRaises(ValueError),
            ):
                duration.elapsed_seconds(start)

    def test_cli_captures_start_or_reports_elapsed(self):
        for args, expected in (([], "1739243000"), (["1000000000"], "0.739243000")):
            output = io.StringIO()
            with (
                mock.patch.object(
                    duration.time, "monotonic_ns", return_value=1_739_243_000
                ),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(duration.main(args), 0)
            self.assertEqual(output.getvalue().strip(), expected)

    def test_cli_invalid_input_has_no_measurement_or_traceback(self):
        for args in (["-1"], ["11"], ["nope"], ["1.2"], ["1", "2"]):
            output, error = io.StringIO(), io.StringIO()
            with (
                self.subTest(args=args),
                mock.patch.object(duration.time, "monotonic_ns", return_value=10),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
                self.assertRaises(SystemExit) as result,
            ):
                duration.main(args)
            self.assertEqual(result.exception.code, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn("Traceback", error.getvalue())
            self.assertTrue(error.getvalue().strip())


class ResticScriptDurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.programs = {
            name: (
                REPO_ROOT / "roles/restic_backup/templates" / (name + ".j2")
            ).read_text()
            for name in ("grayhaven-restic-backup", "grayhaven-restic-check")
        }

    def run_function(self, kind, label="local", failure=""):
        # Exercise only the shell functions with isolated commands and paths;
        # no playbook, inventory, repository, credential, or service is used.
        program = self.programs[
            "grayhaven-restic-backup" if kind == "backup" else "grayhaven-restic-check"
        ]
        functions = [
            re.search(
                rf"^{name}\(\) \{{\n.*?^\}}", program, re.MULTILINE | re.DOTALL
            ).group(0)
            for name in (
                ["restic_stats_restore_size_bytes", "backup_repository"]
                if kind == "backup"
                else ["check_repository"]
            )
        ]
        source = "\n".join(functions).replace(
            "/usr/local/sbin/grayhaven-restic-duration", "duration_helper"
        )
        with tempfile.TemporaryDirectory() as folder:
            calls = Path(folder) / "calls"
            script = r"""
set -Eeuo pipefail
successful_repositories=()
declare -A repository_backup_duration_seconds repository_check_duration_seconds
declare -A repository_restore_size_bytes repository_snapshot_count
PASSWORD_FILE=/fixture/password
INCLUDE_FILE=/fixture/include
EXCLUDE_FILE=/fixture/exclude
KEEP_DAILY=3
hostname() { printf 'unit.example.invalid\n'; }
duration_helper() {
  local phase=start
  [[ $# == 0 ]] || phase=end
  printf '%s\n' "clock-$phase" >> "$CALLS"
  [[ "$FAILURE" != "clock-$phase" ]] || return 9
  python3 "$HELPER" "$@"
}
restic() {
  local action
  for action in "$@"; do
    case "$action" in
      backup|forget|check|stats|snapshots)
        printf '%s\n' "$action" >> "$CALLS"
        [[ "$FAILURE" != "$action" ]] || return 7
        case "$action" in
          stats) printf '{"total_size": 83}\n';;
          snapshots) printf '[{}, {}]\n';;
        esac
        return 0
        ;;
    esac
  done
  return 8
}
"""
            script += (
                source
                + f"\n{kind}_repository /fixture/repository {shlex.quote(label)}\n"
            )
            script += (
                'printf "%s|%s|%s|%s\\n" '
                f'"${{repository_{kind}_duration_seconds[{label}]}}" '
                '"${successful_repositories[*]}" '
                f'"${{repository_restore_size_bytes[{label}]:-}}" '
                f'"${{repository_snapshot_count[{label}]:-}}"\n'
            )
            result = subprocess.run(
                ["bash", "-c", script],
                env={
                    "PATH": os.defpath,
                    "HOME": folder,
                    "CALLS": str(calls),
                    "HELPER": str(HELPER),
                    "FAILURE": failure,
                },
                capture_output=True,
                text=True,
                timeout=20,
            )
            return result, calls.read_text().splitlines()

    def test_success_retains_fraction_and_existing_measurement_boundaries(self):
        for kind in ("backup", "check"):
            for label in ("local", "remote"):
                with self.subTest(kind=kind, label=label):
                    result, calls = self.run_function(kind, label)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    measured, successful, size, count = result.stdout.strip().split("|")
                    self.assertGreater(Decimal(measured), 0)
                    self.assertNotEqual(Decimal(measured) % 1, 0)
                    self.assertEqual(successful, label)
                    if kind == "backup":
                        expected = ["clock-start", "backup", "forget", "clock-end"]
                        if label == "local":
                            expected.append("stats")
                        expected.append("snapshots")
                        self.assertEqual(size, "83" if label == "local" else "")
                        self.assertEqual(count, "2")
                    else:
                        expected = ["clock-start", "check", "clock-end"]
                        self.assertEqual((size, count), ("", ""))
                    self.assertEqual(calls, expected)

    def test_failed_operations_stop_without_recording_success(self):
        for kind, failures in (
            ("backup", ("backup", "forget", "stats", "snapshots")),
            ("check", ("check",)),
        ):
            for failure in failures:
                with self.subTest(kind=kind, failure=failure):
                    result, calls = self.run_function(kind, failure=failure)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(calls[-1], failure)

    def test_failed_clock_stops_without_recording_success(self):
        for kind in ("backup", "check"):
            for failure in ("clock-start", "clock-end"):
                with self.subTest(kind=kind, failure=failure):
                    result, calls = self.run_function(kind, failure=failure)
                    self.assertEqual(result.returncode, 9, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(calls[-1], failure)

if __name__ == "__main__":
    unittest.main()
