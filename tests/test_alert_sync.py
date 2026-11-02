from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from tests.helpers import load_program


alert_sync = load_program(
    "grayhaven_grafana_alert_sync",
    "roles/observability/files/grayhaven-grafana-alert-sync",
)
validator = load_program(
    "alert_sync_fixture", "scripts/validate-generated-grafana-alerts"
)


class Response:
    def __init__(self, body: str = "{}", status: int = 200) -> None:
        self.body = body.encode()
        self.status = status

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.body


def complete_config() -> dict[str, object]:
    config = validator.fixture_config()
    config["grafana"].update(
        {
            "api_token": "token",
            "evaluation_interval": "1m",
            "folder": "Fixture Folder",
            "prometheus_datasource_name": "Prometheus",
            "stack_url": "https://grafana.example.invalid",
        }
    )
    return config


class AlertRuleTests(unittest.TestCase):
    def test_rule_propagates_supplied_values(self) -> None:
        values = {
            "identity": "example:sample",
            "uid_registry": {"example:sample": "123e4567-e89b-42d3-a456-426614174000"},
            "title": "Fixture alert",
            "folder_uid": "fixture-folder",
            "rule_group": "fixture-group",
            "datasource_uid": "fixture-source",
            "expression": "fixture_metric > 4",
            "evaluator_type": "gt",
            "threshold": 42,
            "labels": {"example": "fixture-label"},
            "annotations": {"summary": "fixture-summary"},
            "contact_point": "fixture-contact",
            "duration": "7m",
            "no_data_state": "Alerting",
        }
        rule = alert_sync.alert_rule(**values)
        self.assertEqual(rule["_identity"], values["identity"])
        self.assertEqual(rule["uid"], values["uid_registry"][values["identity"]])
        self.assertEqual(rule["title"], values["title"])
        self.assertEqual(rule["folderUID"], values["folder_uid"])
        self.assertEqual(rule["ruleGroup"], values["rule_group"])
        self.assertEqual(rule["data"][0]["datasourceUid"], values["datasource_uid"])
        self.assertEqual(rule["data"][0]["model"]["expr"], values["expression"])
        evaluator = rule["data"][1]["model"]["conditions"][0]["evaluator"]
        self.assertEqual(evaluator, {"type": values["evaluator_type"], "params": [values["threshold"]]})
        self.assertEqual(rule["labels"]["example"], values["labels"]["example"])
        self.assertEqual(rule["annotations"], values["annotations"])
        self.assertEqual(rule["notification_settings"]["receiver"], values["contact_point"])
        self.assertEqual(rule["for"], values["duration"])
        self.assertEqual(rule["noDataState"], values["no_data_state"])

    def test_pending_period_floor_preserves_longer_periods(self) -> None:
        arguments = {
            "identity": "test:example",
            "uid_registry": {"test:example": "123e4567-e89b-42d3-a456-426614174000"},
            "title": "Example", "folder_uid": "folder", "rule_group": "group",
            "datasource_uid": "prom", "expression": "up", "evaluator_type": "lt",
            "threshold": 1, "labels": {}, "annotations": {}, "contact_point": "IRM",
        }
        baseline = alert_sync.alert_rule(**arguments)
        self.assertEqual(baseline["for"], "5m")
        for duration, expected in (
            ("0s", "5m"), ("1m", "5m"), ("2m", "5m"), ("299s", "5m"),
            ("300s", "300s"), ("5m", "5m"), ("10m", "10m"), ("1h", "1h"),
        ):
            with self.subTest(duration=duration):
                rule = alert_sync.alert_rule(**arguments, duration=duration)
                self.assertEqual(rule, {**baseline, "for": expected})

    def test_helpers_and_rule_generation(self) -> None:
        self.assertTrue(alert_sync.valid_rule_uid("gh-" + "a" * 24))
        self.assertTrue(
            alert_sync.valid_rule_uid("123e4567-e89b-42d3-a456-426614174000")
        )
        self.assertFalse(alert_sync.valid_rule_uid("not-a-uid"))
        with self.assertRaisesRegex(alert_sync.GrafanaError, "not registered"):
            alert_sync.rule_uid("missing", {})
        self.assertEqual(alert_sync.promql_string('a"b\\c'), 'a\\"b\\\\c')
        self.assertIn(
            r"example\\.invalid", alert_sync.domain_regex(["example.invalid"])
        )
        self.assertEqual(alert_sync.duration_seconds(5), 5)
        self.assertEqual(alert_sync.duration_seconds("2h"), 7200)
        with self.assertRaises(alert_sync.GrafanaError):
            alert_sync.duration_seconds("invalid")

        query = alert_sync.query_data("A", "prom", "up")
        threshold = alert_sync.threshold_data("lt", 1)
        self.assertEqual(query["model"]["expr"], "up")
        self.assertEqual(threshold["model"]["conditions"][0]["evaluator"]["type"], "lt")
        rule = alert_sync.alert_rule(
            identity="test:example",
            uid_registry={"test:example": "123e4567-e89b-42d3-a456-426614174000"},
            title="Example",
            folder_uid="folder",
            rule_group="group",
            datasource_uid="prom",
            expression="up",
            evaluator_type="lt",
            threshold=1,
            labels={"client": "fixture"},
            annotations={"summary": "Example"},
            contact_point="IRM",
        )
        self.assertEqual(
            rule["labels"][alert_sync.MANAGED_LABEL], alert_sync.MANAGED_VALUE
        )
        self.assertEqual(rule["uid"], "123e4567-e89b-42d3-a456-426614174000")

        config = complete_config()
        desired = alert_sync.desired_rules(config, "folder", "prom")
        self.assertTrue(any(
            item["labels"].get("host") == config["hosts"][0]["fqdn"]
            for item in desired.values()
        ))
        self.assertTrue(any(
            item["labels"].get("domain") == config["sites"][0]["domain"]
            for item in desired.values()
        ))
        self.assertEqual(len(desired), len(set(desired)))
        self.assertTrue(
            alert_sync.is_managed(next(iter(desired.values())), "fixture")
        )
        self.assertFalse(alert_sync.is_managed({}, "fixture"))
        self.assertEqual(
            alert_sync.comparable({**rule, "unmanaged": True}),
            alert_sync.comparable(rule),
        )
        self.assertEqual(
            alert_sync.timestamp(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            "2026-01-01T00:00:00.000Z",
        )

    def test_usage_thresholds_are_strict_integer_boundaries(self) -> None:
        for value, expected in ((0, -1), (1, 0), (43, 42)):
            self.assertEqual(alert_sync.integer_at_least_threshold(value), expected)
        for value in (True, False, 1.5, -1, "43", "4.0", "invalid", None, object()):
            with self.subTest(value=value), self.assertRaisesRegex(
                alert_sync.GrafanaError, "non-negative integer"
            ):
                alert_sync.integer_at_least_threshold(value)

    def test_uid_registry_rejects_invalid_shapes_and_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "uids.json"
            for value, message in (
                ([], "non-empty object"),
                ({}, "non-empty object"),
                ({"identity": "invalid"}, "invalid identity or UID"),
                (
                    {"": "123e4567-e89b-42d3-a456-426614174000"},
                    "invalid identity or UID",
                ),
                (
                    {
                        "one": "123e4567-e89b-42d3-a456-426614174000",
                        "two": "123e4567-e89b-42d3-a456-426614174000",
                    },
                    "duplicate UIDs",
                ),
            ):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(alert_sync.GrafanaError, message):
                    alert_sync.load_uid_registry(str(path))

            expected = {"identity": "123e4567-e89b-42d3-a456-426614174000"}
            path.write_text(json.dumps(expected), encoding="utf-8")
            self.assertEqual(alert_sync.load_uid_registry(str(path)), expected)

    def test_desired_rules_rejects_duplicate_identities_and_uids(self) -> None:
        config = complete_config()
        first = {"_identity": "duplicate", "uid": "one"}
        second = {"_identity": "duplicate", "uid": "two"}
        with (
            mock.patch.object(
                alert_sync, "host_metric_rules", return_value=[first, second]
            ),
            mock.patch.object(alert_sync, "service_rules", return_value=[]),
            mock.patch.object(alert_sync, "site_rules", return_value=[]),
            mock.patch.object(alert_sync, "external_service_rules", return_value=[]),
            self.assertRaisesRegex(
                alert_sync.GrafanaError, "identities are not unique"
            ),
        ):
            alert_sync.desired_rules(config, "folder", "prom")

        first = {"_identity": "one", "uid": "duplicate"}
        second = {"_identity": "two", "uid": "duplicate"}
        with (
            mock.patch.object(
                alert_sync, "host_metric_rules", return_value=[first, second]
            ),
            mock.patch.object(alert_sync, "service_rules", return_value=[]),
            mock.patch.object(alert_sync, "site_rules", return_value=[]),
            mock.patch.object(alert_sync, "external_service_rules", return_value=[]),
            self.assertRaisesRegex(alert_sync.GrafanaError, "UIDs are not unique"),
        ):
            alert_sync.desired_rules(config, "folder", "prom")

    def test_grafana_client_request_and_helpers(self) -> None:
        client = alert_sync.GrafanaClient("https://grafana.example.invalid/", "token")
        with mock.patch.object(
            alert_sync.urllib.request,
            "urlopen",
            return_value=Response('{"uid":"prom"}'),
        ):
            self.assertEqual(client.request("GET", "/test"), {"uid": "prom"})
        with mock.patch.object(
            alert_sync.urllib.request, "urlopen", return_value=Response("", 204)
        ):
            self.assertIsNone(client.request("DELETE", "/test", expected=(204,)))
        with mock.patch.object(
            alert_sync.urllib.request, "urlopen", return_value=Response("{}")
        ) as urlopen:
            self.assertEqual(client.request("PUT", "/test", {"value": 1}), {})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.data, b'{"value": 1}')
        self.assertEqual(request.headers["Content-type"], "application/json")

        error = urllib.error.HTTPError(
            "url", 400, "bad", {}, io.BytesIO(b"bad request")
        )
        with (
            mock.patch.object(alert_sync.urllib.request, "urlopen", side_effect=error),
            self.assertRaisesRegex(alert_sync.GrafanaError, "HTTP 400"),
        ):
            client.request("GET", "/test")
        retry = urllib.error.HTTPError("url", 503, "failed", {}, io.BytesIO(b""))
        with (
            mock.patch.object(alert_sync.urllib.request, "urlopen", side_effect=retry),
            mock.patch.object(alert_sync.time, "sleep"),
            self.assertRaisesRegex(alert_sync.GrafanaError, "retry budget"),
        ):
            client.request("GET", "/test")
        retry = urllib.error.HTTPError(
            "url", 429, "limited", {"Retry-After": "0"}, io.BytesIO(b"")
        )
        with (
            mock.patch.object(
                alert_sync.urllib.request,
                "urlopen",
                side_effect=[retry, Response("{}")],
            ),
            mock.patch.object(alert_sync.time, "sleep"),
        ):
            self.assertEqual(client.request("GET", "/test"), {})
        with (
            mock.patch.object(
                alert_sync.urllib.request, "urlopen", return_value=Response("{}", 201)
            ),
            self.assertRaisesRegex(alert_sync.GrafanaError, "unexpected HTTP status"),
        ):
            client.request("GET", "/test")

        with mock.patch.object(client, "request") as request:
            request.side_effect = [
                {"uid": "prom"},
                [{"title": "Other", "uid": "one"}, {"title": "Folder", "uid": "two"}],
                [{"uid": "rule"}],
                {"interval": 60},
                {"interval": 30},
                None,
                None,
                None,
                None,
                {"silenceID": "silence"},
            ]
            self.assertEqual(client.datasource_uid("Prometheus Main"), "prom")
            self.assertEqual(client.folder_uid("Folder"), "two")
            self.assertEqual(client.alert_rules(), [{"uid": "rule"}])
            self.assertFalse(client.update_rule_group_interval("folder", "group", 60))
            self.assertTrue(client.update_rule_group_interval("folder", "group", 60))
            client.create_rule({})
            client.update_rule("uid", {})
            client.delete_rule("uid")
            self.assertEqual(client.create_silence({}), "silence")
        with (
            mock.patch.object(client, "request", return_value=[]),
            self.assertRaisesRegex(alert_sync.GrafanaError, "was not found"),
        ):
            client.folder_uid("Missing")
        with (
            mock.patch.object(client, "request", return_value={}),
            self.assertRaisesRegex(alert_sync.GrafanaError, "silence ID"),
        ):
            client.create_silence({})
        with (
            mock.patch.object(client, "request", return_value=[]),
            self.assertRaisesRegex(alert_sync.GrafanaError, "unexpected response"),
        ):
            client.create_silence({})

    def test_rule_generation_boundaries(self) -> None:
        config = complete_config()
        config["hosts"].append(
            {
                **config["hosts"][0],
                "control_node": True,
                "fqdn": "control.example.invalid",
                "role": "bastion",
                "short_hostname": "control",
            }
        )
        config["service_units"]["common"] = [
            {"check": "common_service", "name": "mock-common.service", "summary": "Common is down"}
        ]
        config["service_units"]["control_node"] = [
            {"check": "control_service", "name": "mock-control.service", "summary": "Control is down"}
        ]
        services = alert_sync.service_rules(config, "folder", "prom")
        checks = {rule["labels"]["check"] for rule in services}
        expected = {
            unit["check"]
            for group in ("common", "control_node")
            for unit in config["service_units"][group]
        }
        self.assertTrue(expected <= checks)

        with_control = alert_sync.host_metric_rules(config, "folder", "prom")
        config["hosts"][-1]["control_node"] = False
        without_control = alert_sync.host_metric_rules(config, "folder", "prom")
        self.assertEqual(len(with_control), len(without_control) + 1)

        no_sites = complete_config()
        no_sites["sites"] = []
        self.assertEqual(alert_sync.site_rules(no_sites, "folder", "prom"), [])
        no_web = complete_config()
        no_web["hosts"][0]["role"] = "bastion"
        self.assertEqual(alert_sync.site_rules(no_web, "folder", "prom"), [])

        config = complete_config()
        local_tls = alert_sync.site_rules(config, "folder", "prom")
        config["tls_mode"] = "load_balancer"
        self.assertLess(len(alert_sync.site_rules(config, "folder", "prom")), len(local_tls))

        config = complete_config()
        config["sites"][0]["redirect_http"] = False
        baseline = alert_sync.site_rules(config, "folder", "prom")
        dev_domain = "sandbox.example.invalid"
        config["sites"][0]["dev_domain"] = dev_domain
        with_development = alert_sync.site_rules(config, "folder", "prom")
        self.assertEqual(len(with_development), len(baseline) + 1)
        self.assertTrue(any(rule["labels"].get("domain") == dev_domain for rule in with_development))
        config["sites"][0]["redirect_http"] = True
        with_redirect = alert_sync.site_rules(config, "folder", "prom")
        self.assertEqual(len(with_redirect), len(with_development) + 1)

    def test_sync_silence_and_main(self) -> None:
        config = complete_config()
        desired = alert_sync.desired_rules(config, "folder", "prom")
        first_uid, first_rule = next(iter(desired.items()))
        changed_existing = json.loads(json.dumps(first_rule))
        changed_existing["for"] = "99m"
        stale = {
            "uid": "stale",
            "labels": {"configured_by": "ansible", "client": "fixture"},
        }

        fake = mock.Mock()
        fake.datasource_uid.return_value = "prom"
        fake.folder_uid.return_value = "folder"
        fake.alert_rules.return_value = [changed_existing, stale, {"uid": "foreign"}]
        fake.update_rule_group_interval.return_value = True
        with mock.patch.object(alert_sync, "GrafanaClient", return_value=fake):
            created, updated, deleted = alert_sync.sync(config)
        self.assertEqual(created, len(desired) - 1)
        self.assertEqual(updated, 2)
        self.assertEqual(deleted, 1)
        fake.update_rule.assert_called_once_with(first_uid, first_rule)
        fake.delete_rule.assert_called_once_with("stale")

        unchanged = mock.Mock()
        unchanged.datasource_uid.return_value = "prom"
        unchanged.folder_uid.return_value = "folder"
        unchanged.alert_rules.return_value = list(desired.values())
        unchanged.update_rule_group_interval.return_value = False
        with mock.patch.object(alert_sync, "GrafanaClient", return_value=unchanged):
            self.assertEqual(alert_sync.sync(config), (0, 0, 0))
        unchanged.update_rule.assert_not_called()

        empty = mock.Mock()
        empty.datasource_uid.return_value = "prom"
        empty.folder_uid.return_value = "folder"
        empty.alert_rules.return_value = []
        with (
            mock.patch.object(alert_sync, "GrafanaClient", return_value=empty),
            mock.patch.object(alert_sync, "desired_rules", return_value={}),
        ):
            self.assertEqual(alert_sync.sync(config), (0, 0, 0))
        empty.update_rule_group_interval.assert_not_called()

        with self.assertRaises(alert_sync.GrafanaError):
            alert_sync.create_initial_silence(config, 0)
        fake = mock.Mock()
        fake.create_silence.return_value = "silence"
        with mock.patch.object(alert_sync, "GrafanaClient", return_value=fake):
            self.assertEqual(alert_sync.create_initial_silence(config, 15), "silence")
        payload = fake.create_silence.call_args.args[0]
        self.assertTrue(any(item["value"] == config["labels"]["client"] for item in payload["matchers"]))

        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "config.json"
            registry = pathlib.Path(temp_dir) / "registry.json"
            registry.write_text(json.dumps(config["uid_registry"]), encoding="utf-8")
            config["uid_registry_file"] = str(registry)
            path.write_text(json.dumps(config), encoding="utf-8")
            with (
                mock.patch("sys.argv", ["sync", "--config", str(path)]),
                mock.patch.object(alert_sync, "sync", return_value=(1, 2, 3)) as sync,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(alert_sync.main(), 0)
            self.assertEqual(sync.call_args.args[0]["labels"], config["labels"])
            with (
                mock.patch(
                    "sys.argv",
                    [
                        "sync",
                        "--config",
                        str(path),
                        "--create-initial-silence",
                        "--silence-minutes",
                        "10",
                    ],
                ),
                mock.patch.object(
                    alert_sync, "create_initial_silence", return_value="fake-silence"
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(alert_sync.main(), 0)
            self.assertIn("fake-silence", stdout.getvalue())
