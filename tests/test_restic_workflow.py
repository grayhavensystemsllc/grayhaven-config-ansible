"""Unit tests for backup selection and repository metric preservation."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from tests.helpers import load_program

metrics = load_program("fixture_metrics", "roles/restic_backup/files/grayhaven-restic-workflow")


class ResticWorkflowTests(unittest.TestCase):
    def test_each_selection_starts_with_one_snapshot_and_runs_selected_repositories(self):
        for selection, remote, expected in (
            ("local", True, ["snapshot", "local"]),
            ("local", False, ["snapshot", "local"]),
            ("remote", True, ["snapshot", "remote"]),
            ("all", True, ["snapshot", "local", "remote"]),
            ("all", False, ["snapshot", "local"]),
        ):
            with self.subTest(selection=selection, remote=remote):
                self.assertEqual(metrics.select(selection, remote), expected)
        for selection, remote in (("remote", False), ("unknown", True)):
            with self.subTest(selection=selection, remote=remote), self.assertRaises(ValueError):
                metrics.select(selection, remote)

    def test_selection_command_rejects_unavailable_or_invalid_repositories(self):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(metrics.main(["select", "local", "true"]), 0)
        self.assertEqual(output.getvalue().splitlines(), ["snapshot", "local"])
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit):
            metrics.main(["select", "remote", "false"])
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit):
            metrics.main(["select", "invalid", "true"])

class ResticMetricsTests(unittest.TestCase):
    def test_updates_one_repository_and_preserves_other_series_in_their_family(self):
        host = "node.example.invalid"
        def sample(name, repo, value, node=host):
            return f'{name}{{host="{node}",repository="{repo}"}} {value}\n'
        previous = ("# HELP sample_count example\n# TYPE sample_count gauge\n"
                    + sample("sample_count", "alpha", 1) + sample("sample_count", "beta", 2)
                    + sample("sample_count", "beta", 99, "other.example.invalid")
                    + "# HELP sample_duration example\n# TYPE sample_duration gauge\n"
                    + sample("sample_duration", "beta", 4))
        current = ("# HELP sample_count example\n# TYPE sample_count gauge\n"
                   + sample("sample_count", "alpha", 3)
                   + "# HELP sample_duration example\n# TYPE sample_duration gauge\n"
                   + sample("sample_duration", "alpha", 5))
        value = metrics.merge(previous, current, host, "alpha")
        self.assertIn(sample("sample_count", "alpha", 3), value)
        self.assertIn(sample("sample_count", "beta", 2), value)
        self.assertIn(sample("sample_duration", "beta", 4), value)
        self.assertNotIn(sample("sample_count", "alpha", 1), value)
        self.assertNotIn(sample("sample_count", "beta", 99, "other.example.invalid"), value)
        self.assertLess(value.index(sample("sample_count", "beta", 2)), value.index("# HELP sample_duration"))

    def test_duplicate_or_unrelated_prior_samples_are_not_added(self):
        host = "node.example.invalid"
        existing = 'sample{host="node.example.invalid",repository="beta"} 2\n'
        current = "# HELP sample x\n# TYPE sample gauge\n" + existing
        previous = existing + existing + 'other{host="node.example.invalid",repository="beta"} 8\n'
        self.assertEqual(metrics.merge(previous, current, host, "alpha"), current)
        self.assertEqual(metrics.merge(previous, "no families\n", host, "alpha"), "no families\n")

    def test_only_well_formed_repository_samples_have_an_identity(self):
        valid = 'test_metric{host="n.example.invalid",repository="example"} 7'
        self.assertEqual(metrics.sample_identity(valid), ("test_metric", "n.example.invalid", "example"))
        for invalid in ("nonsense", "metric 4", 'metric{host="n"} 1',
                        'metric{repository="r"} 1'):
            with self.subTest(invalid=invalid):
                self.assertIsNone(metrics.sample_identity(invalid))

    def test_command_reads_current_data_when_previous_file_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current"
            example = "# HELP sample x\n# TYPE sample gauge\nsample 4\n"
            current.write_text(example)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(metrics.main(["merge", str(root / "missing"), str(current),
                                               "n.example.invalid", "alpha"]), 0)
            self.assertEqual(output.getvalue(), example)
            previous = root / "previous"
            previous.write_text("irrelevant\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(metrics.main(["merge", str(previous), str(current),
                                               "n.example.invalid", "alpha"]), 0)
            with self.assertRaises(FileNotFoundError):
                metrics.main(["merge", str(previous), str(root / "missing"), "n.example.invalid", "alpha"])


if __name__ == "__main__":
    unittest.main()
