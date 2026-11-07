"""Evaluate generic dependency-state conditions with invented metric samples."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

from tests.helpers import load_program


alerts = load_program(
    "dependency_queries", "roles/observability/files/grayhaven-grafana-alert-sync"
)


class DependencyQueryTests(unittest.TestCase):
    def config(self, count=1):
        return {
            "hosts": [
                {"fqdn": f"control-{index}.example.test", "control_node": True}
                for index in range(count)
            ]
        }

    def test_no_controller_returns_an_unsuppressed_condition(self):
        suppression, reporting, expected = alerts.dependency_state_queries(
            self.config(0),
            "mock-dependency.service",
            freshness_window="2m",
            settle_window="4m",
        )
        self.assertIsNone(suppression)
        self.assertEqual(reporting, "vector(0)")
        self.assertEqual(expected, 1)

    def test_expected_reporting_count_follows_supplied_controllers(self):
        for count in (1, 2, 4):
            _, _, expected = alerts.dependency_state_queries(
                self.config(count),
                "mock-dependency.service",
                freshness_window="3m",
                settle_window="7m",
            )
            self.assertEqual(expected, count)
        for value in ("invalid", "-1m"):
            with self.assertRaises(alerts.GrafanaError):
                alerts.dependency_state_queries(
                    self.config(),
                    "mock-dependency.service",
                    freshness_window=value,
                    settle_window="4m",
                )

    @unittest.skipUnless(shutil.which("promtool"), "promtool is required")
    def test_active_recent_expired_and_missing_dependency_samples(self):
        suppression, _, _ = alerts.dependency_state_queries(
            self.config(),
            "mock-dependency.service",
            freshness_window="2m",
            settle_window="4m",
        )
        expression = "(max(sample_load) > 0) unless on () " + suppression
        labels = 'host="control-0.example.test",name="mock-dependency.service"'
        active = {
            "series": "node_systemd_unit_state{" + labels + ',state="active"}',
            "values": "0x2 1x2 0x10",
        }
        activating = {
            "series": "node_systemd_unit_state{" + labels + ',state="activating"}',
            "values": "0x16",
        }

        def expected_alerts(firing):
            return [{"exp_labels": {}, "exp_annotations": {}}] if firing else []

        tests = [
            {
                "interval": "1m",
                "input_series": [
                    active,
                    activating,
                    {"series": "sample_load", "values": "1x16"},
                ],
                "alert_rule_test": [
                    {
                        "eval_time": time,
                        "alertname": "SampleLoad",
                        "exp_alerts": expected_alerts(firing),
                    }
                    for time, firing in (
                        ("1m", True),
                        ("4m", False),
                        ("8m", False),
                        ("10m", True),
                    )
                ],
            },
            {
                "interval": "1m",
                "input_series": [active, {"series": "sample_load", "values": "1x16"}],
                "alert_rule_test": [
                    {
                        "eval_time": "4m",
                        "alertname": "SampleLoad",
                        "exp_alerts": expected_alerts(True),
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = {
                "groups": [
                    {
                        "name": "sample",
                        "rules": [{"alert": "SampleLoad", "expr": expression}],
                    }
                ]
            }
            (root / "rules.yml").write_text(yaml.safe_dump(rules, explicit_start=True))
            (root / "cases.yml").write_text(
                yaml.safe_dump(
                    {
                        "rule_files": ["rules.yml"],
                        "evaluation_interval": "1m",
                        "tests": tests,
                    },
                    explicit_start=True,
                )
            )
            result = subprocess.run(
                [shutil.which("promtool"), "test", "rules", "cases.yml"],
                cwd=root,
                env={"PATH": os.defpath, "HOME": directory, "LANG": "C.UTF-8"},
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)
