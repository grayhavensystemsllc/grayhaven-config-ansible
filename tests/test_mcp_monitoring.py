"""Alert and probe generation with fictional application inputs."""

from __future__ import annotations

import unittest
from unittest import mock

from tests.helpers import load_program
from tests.test_alert_sync import alert_sync, complete_config


class ApplicationMonitoringTests(unittest.TestCase):
    def test_application_rule_generation_uses_supplied_host_and_site(self):
        config = complete_config()
        host = {
            "client": "fixture",
            "environment": "test",
            "role": "mcp",
            "project": "fixture",
            "control_node": False,
            "fqdn": "worker.example.invalid",
            "short_hostname": "worker",
        }
        site = {
            "domain": "application.example.invalid",
            "root_name": "fixture-app",
            "kind": "application",
            "role": "mcp",
            "display_name": "Fixture Application",
            "health_check": "fixture_health",
            "redirect_http": False,
            "aliases": [],
            "dev_domain": "",
        }
        unit = {
            "check": "fixture_unit",
            "name": "mock-dependency.service",
            "summary": "Fixture service is down",
        }
        config["hosts"] = [host]
        config["sites"] = [site]
        config["service_units"]["mcp"] = [unit]
        config["tls_mode"] = "load_balancer"
        rules = [
            rule
            for function in (alert_sync.service_rules, alert_sync.host_metric_rules, alert_sync.site_rules)
            for rule in function(config, "folder", "prom")
        ]
        checks = {rule["labels"]["check"] for rule in rules}
        self.assertIn(unit["check"], checks)
        self.assertIn(site["health_check"], checks)
        self.assertNotIn("http_https_redirect", checks)
        self.assertTrue(all(rule["labels"]["role"] == host["role"] for rule in rules))
        self.assertTrue(any(rule["labels"].get("host") == host["fqdn"] for rule in rules))
        self.assertTrue(any(rule["labels"].get("domain") == site["domain"] for rule in rules))
        self.assertEqual(len(rules), len({rule["uid"] for rule in rules}))

    def test_validator_cli_selects_requested_role(self):
        validator = load_program("fake_alloy_cli", "scripts/validate-rendered-alloy-config")
        with (
            mock.patch("sys.argv", ["validate-rendered-alloy-config", "--role", "mcp"]),
            mock.patch.object(validator, "render_config") as render,
            mock.patch.object(validator, "validate_https_probe_contract"),
            mock.patch.object(validator.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
        ):
            self.assertEqual(validator.main(), 0)
        self.assertEqual(render.call_args.args[1], "mcp")
        self.assertEqual(run.call_args.args[0][:2], ["alloy", "validate"])
