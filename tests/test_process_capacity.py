"""Process and cgroup measurements use isolated invented filesystem samples."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.helpers import load_program


collector = load_program(
    "process_capacity_collector",
    "roles/observability/files/grayhaven-observability-textfile",
)


class ProcessCapacityTests(unittest.TestCase):
    def test_task_count_and_capacity_follow_kernel_values(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            kernel = root / "sys/kernel"
            kernel.mkdir(parents=True)
            (kernel / "pid_max").write_text("500")
            (kernel / "threads-max").write_text("250")
            for pid, threads, state in ((1, 3, "S"), (4, 1, "Z"), (8, 2, "R")):
                (root / str(pid)).mkdir()
                (root / str(pid) / "status").write_text(
                    f"Threads: {threads}\nState: {state}\n"
                )
            (root / "9").mkdir()  # An exited process is a normal sampling race.
            self.assertEqual(collector.process_capacity(root), (6, 250, 1))
            for invalid in ("0", "-1", "invalid"):
                (kernel / "pid_max").write_text(invalid)
                with self.assertRaises(ValueError):
                    collector.process_capacity(root)
            (kernel / "pid_max").write_text("500")
            (root / "1/status").write_text("Threads: 0\nState: S\n")
            with self.assertRaises(ValueError):
                collector.process_capacity(root)

    def test_container_measurements_and_invalid_cgroups(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            proc, groups = root / "proc", root / "groups"
            (proc / "24").mkdir(parents=True)
            (groups / "sample").mkdir(parents=True)
            (proc / "24/cgroup").write_text("0::/sample\n")
            (groups / "sample/pids.current").write_text("7")
            for maximum, expected in (("50", 50), ("max", 0)):
                (groups / "sample/pids.max").write_text(maximum)
                with mock.patch.object(
                    collector.subprocess, "run", return_value=mock.Mock(stdout="24")
                ) as run:
                    self.assertEqual(
                        collector.container_capacity("fixture-app", proc, groups),
                        (7, expected),
                    )
                    self.assertEqual(run.call_args.args[0][-1], "fixture-app")
            (groups / "sample/pids.max").write_text("-1")
            with mock.patch.object(
                collector.subprocess, "run", return_value=mock.Mock(stdout="24")
            ):
                with self.assertRaises(ValueError):
                    collector.container_capacity("fixture-app", proc, groups)
                for cgroup in ("0::/../../outside", "1:legacy:/sample"):
                    (proc / "24/cgroup").write_text(cgroup)
                    with self.assertRaises(ValueError):
                        collector.container_capacity("fixture-app", proc, groups)
            for pid in ("0", "invalid"):
                with mock.patch.object(
                    collector.subprocess, "run", return_value=mock.Mock(stdout=pid)
                ):
                    with self.assertRaises(ValueError):
                        collector.container_capacity("fixture-app", proc, groups)

    def test_rendering_covers_enabled_disabled_and_failed_collection(self):
        config = {
            "host": {
                "client": "fixture",
                "environment": "test",
                "fqdn": "node.example.test",
                "project": "sample",
                "role": "worker",
            },
            "process": {"enabled": True, "containers": ["fixture-app"]},
        }
        with (
            mock.patch.object(collector, "process_capacity", return_value=(8, 100, 2)),
            mock.patch.object(collector, "container_capacity", return_value=(3, 20)),
        ):
            rows = collector.render_process_metrics(config)
            values = {row.split("{", 1)[0]: row.rsplit(" ", 1)[1] for row in rows}
            self.assertEqual(
                values,
                {
                    "grayhaven_host_process_tasks": "8",
                    "grayhaven_host_process_capacity": "100",
                    "grayhaven_host_zombies": "2",
                    "grayhaven_container_tasks": "3",
                    "grayhaven_container_task_limit": "20",
                    "grayhaven_process_telemetry_success": "1",
                },
            )
        for error in (
            OSError(),
            ValueError(),
            KeyError(),
            subprocess.TimeoutExpired("fixture", 1),
        ):
            with mock.patch.object(collector, "process_capacity", side_effect=error):
                rows = collector.render_process_metrics(config)
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].endswith(" 0"))
        config["process"]["enabled"] = False
        with mock.patch.object(collector, "process_capacity") as measure:
            self.assertEqual(collector.render_process_metrics(config), [])
            measure.assert_not_called()
