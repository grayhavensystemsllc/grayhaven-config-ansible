"""Selected public-status telemetry and rules use synthetic provider inputs."""

import json
import unittest
from unittest import mock

from tests.helpers import load_program
from tests.test_alert_sync import validator

collector = load_program(
    "public_status_collector",
    "roles/observability/files/grayhaven-observability-textfile",
)
alerts = load_program(
    "public_status_alerts", "roles/observability/files/grayhaven-grafana-alert-sync"
)


def providers():
    return {
        "example": {
            "name": "Example",
            "url": "https://status.example.invalid/summary.json",
            "components": [
                {"id": "alpha", "name": "First"},
                {"id": "beta", "name": "Second"},
            ],
        }
    }


def config():
    return {
        "host": {"control_node": True, "client": "fixture", "environment": "test"},
        "public_status": {"enabled": True, "providers": providers()},
    }


class PublicStatusTests(unittest.TestCase):
    def test_component_response_validation_and_identifier_selection(self):
        body = {
            "components": [
                {"id": "alpha", "status": "operational"},
                {},
                None,
                "invalid",
            ]
        }
        with mock.patch.object(collector, "request_json", return_value=body):
            self.assertEqual(
                collector.public_status_components("https://example.invalid"),
                {"alpha": body["components"][0]},
            )
        for body in (None, [], {}, {"components": {}}, {"components": None}):
            with (
                self.subTest(body=body),
                mock.patch.object(collector, "request_json", return_value=body),
            ):
                with self.assertRaises(ValueError):
                    collector.public_status_components("https://example.invalid")

    def test_rendering_uses_selected_components_and_operational_status(self):
        for status, expected in (
            ("operational", 1),
            ("degraded_performance", 0),
            ("major_outage", 0),
            ("", 0),
            (None, 0),
        ):
            payload = {
                "alpha": {"status": status},
                "unselected": {"status": "major_outage"},
            }
            with (
                self.subTest(status=status),
                mock.patch.object(
                    collector, "public_status_components", return_value=payload
                ),
            ):
                lines = collector.render_public_status_metrics(config())
                metrics = [line for line in lines if not line.startswith("#")]
                self.assertEqual(len(metrics), 3)
                first = next(line for line in metrics if 'component="alpha"' in line)
                second = next(line for line in metrics if 'component="beta"' in line)
                self.assertTrue(first.endswith(" " + str(expected)))
                self.assertTrue(second.endswith(" 0"))
                self.assertIn('status="Unknown"', second)
                self.assertNotIn("unselected", "\n".join(metrics))
                collector.public_status_components.assert_called_once_with(
                    providers()["example"]["url"]
                )

    def test_disabled_noncontrol_and_empty_provider_scopes_do_not_fetch(self):
        for value in (
            {"host": {"control_node": True}},
            {"host": {"control_node": False}, "public_status": {"enabled": True}},
            {"host": {"control_node": True}, "public_status": {"enabled": False}},
            {"host": {"control_node": True}, "public_status": {"enabled": True}},
        ):
            with mock.patch.object(collector, "public_status_components") as fetch:
                self.assertEqual(collector.render_public_status_metrics(value), [])
                fetch.assert_not_called()

    def test_provider_failure_emits_failed_telemetry_without_fabricating_status(self):
        for error in (
            OSError("offline"),
            ValueError("malformed"),
            json.JSONDecodeError("bad", "", 0),
        ):
            with (
                self.subTest(error=type(error)),
                mock.patch.object(
                    collector, "public_status_components", side_effect=error
                ),
            ):
                metrics = [
                    line
                    for line in collector.render_public_status_metrics(config())
                    if not line.startswith("#")
                ]
                self.assertEqual(len(metrics), 1)
                self.assertTrue(
                    metrics[0].startswith("grayhaven_example_telemetry_success{")
                )
                self.assertTrue(metrics[0].endswith(" 0"))

    def test_rules_filter_component_identity_and_alert_on_collection_failure(self):
        settings = {
            "public_status": providers(),
            "labels": {"client": "fixture", "environment": "test"},
            "grafana": {
                "evaluation_group": "fixture-group",
                "contact_point": "fixture-contact",
            },
            "uid_registry": validator.FixtureRegistry(),
        }
        rules = alerts.public_status_rules(settings, "fixture-folder", "fixture-source")
        self.assertEqual(len(rules), 1 + len(providers()["example"]["components"]))
        self.assertEqual(
            {rule["uid"] for rule in rules}, set(settings["uid_registry"].values())
        )
        telemetry = next(
            rule
            for rule in rules
            if rule["labels"]["check"].endswith("telemetry_success")
        )
        self.assertEqual(telemetry["noDataState"], "Alerting")
        for component, rule in zip(providers()["example"]["components"], rules[1:]):
            self.assertIn(
                'component="' + component["id"] + '"', rule["data"][0]["model"]["expr"]
            )
            self.assertEqual(
                rule["data"][1]["model"]["conditions"][0]["evaluator"],
                {"type": "lt", "params": [1]},
            )
            self.assertEqual(
                rule["notification_settings"]["receiver"],
                settings["grafana"]["contact_point"],
            )
        del settings["public_status"]
        self.assertEqual(alerts.public_status_rules(settings, "folder", "source"), [])
