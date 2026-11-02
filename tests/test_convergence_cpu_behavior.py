from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from tests.helpers import load_program


alert_sync = load_program(
    "cpu_alert_sync",
    "roles/observability/files/grayhaven-grafana-alert-sync",
)
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "convergence-cpu-alerts.yml"
MISSING_CONTROL_FIXTURE = FIXTURE.parent / "convergence-cpu-missing-control.yml"


def fake_config(role: str) -> dict[str, object]:
    host = {
        "fqdn": f"{role}.example.invalid",
        "short_hostname": role,
        "role": role,
        "project": "example",
    }
    control = {
        "fqdn": "control.example.invalid",
        "short_hostname": "control",
        "role": "bastion",
        "project": "example",
        "control_node": True,
    }
    checks = (
        "metrics_data", "cpu_utilization", "memory_utilization", "filesystem_space",
        "filesystem_inode", "swap_utilization", "restic_stale_backup", "ansible_convergence",
    )
    registry = {
        alert_sync.host_rule_identity(item, check): f"00000000-0000-4000-8000-{index:012d}"
        for index, (item, check) in enumerate(
            ((item, check) for item in (host, control) for check in checks), 1
        )
    }
    registry[alert_sync.service_rule_identity("Ansible", "ansible_runner_state_metric")] = (
        "00000000-0000-4000-8000-000000000099"
    )
    return {
        "hosts": [host, control],
        "sites": [],
        "labels": {"client": "example", "environment": "test"},
        "grafana": {"evaluation_group": "example", "contact_point": "example"},
        "thresholds": {
            "cpu_percent": 80, "memory_percent": 80, "filesystem_percent": 80,
            "filesystem_inode_percent": 80, "swap_percent": 80, "restic_stale_seconds": 3600,
        },
        "uid_registry": registry,
    }


class ConvergenceCPUBehaviorTests(unittest.TestCase):
    def run_cases(self, config: dict[str, object], role: str, fixture_path: pathlib.Path) -> None:
        promtool = os.environ.get("PROMTOOL") or shutil.which("promtool")
        self.assertTrue(promtool, "promtool is required to test generated alert behavior")
        cpu = next(
            rule for rule in alert_sync.host_metric_rules(config, "folder", "prometheus")
            if rule["labels"]["check"] == "cpu_utilization"
            and rule["labels"]["host"] == f"{role}.example.invalid"
        )
        missing = alert_sync.runner_state_metric_rule(config, "folder", "prometheus")
        cpu_eval = cpu["data"][1]["model"]["conditions"][0]["evaluator"]
        missing_eval = missing["data"][1]["model"]["conditions"][0]["evaluator"]
        self.assertEqual(cpu_eval["type"], "gte")
        self.assertEqual(missing_eval["type"], "lt")
        self.assertEqual(cpu["noDataState"], "OK")
        self.assertEqual(missing["noDataState"], "Alerting")
        rules = {
            "groups": [{
                "name": "test",
                "rules": [
                    {
                        "alert": "CPU",
                        "expr": f'({cpu["data"][0]["model"]["expr"]}) >= {cpu_eval["params"][0]}',
                        "for": cpu["for"],
                    },
                    {
                        "alert": "RunnerMetricMissing",
                        "expr": f'({missing["data"][0]["model"]["expr"]}) < {missing_eval["params"][0]}',
                        "for": missing["for"],
                    },
                ],
            }],
        }
        fixture = fixture_path.read_text(encoding="utf-8")
        fixture = fixture.replace("web.example.invalid", f"{role}.example.invalid")
        fixture = fixture.replace('role="web"', f'role="{role}"').replace("role: web", f"role: {role}")
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / fixture_path.name).write_text(fixture, encoding="utf-8")
            (root / "generated-cpu.rules.yml").write_text(json.dumps(rules), encoding="utf-8")
            result = subprocess.run(
                [promtool, "test", "rules", str(root / fixture_path.name)],
                capture_output=True, text=True, timeout=30, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_generated_rules_against_synthetic_metrics(self) -> None:
        for role in ("bastion", "web", "mcp"):
            with self.subTest(role=role):
                self.run_cases(fake_config(role), role, FIXTURE)

    def test_missing_control_hosts_fail_open(self) -> None:
        config = fake_config("web")
        config["hosts"] = config["hosts"][:1]
        self.run_cases(config, "web", MISSING_CONTROL_FIXTURE)

        config = fake_config("web")
        extra_control = {**config["hosts"][1], "fqdn": "control2.example.invalid"}
        config["hosts"].append(extra_control)
        for index, check in enumerate(
            ("metrics_data", "cpu_utilization", "memory_utilization", "filesystem_space",
             "filesystem_inode", "swap_utilization", "restic_stale_backup", "ansible_convergence"),
            100,
        ):
            config["uid_registry"][alert_sync.host_rule_identity(extra_control, check)] = (
                f"00000000-0000-4000-8000-{index:012d}"
            )
        self.run_cases(config, "web", MISSING_CONTROL_FIXTURE)


if __name__ == "__main__":
    unittest.main()
