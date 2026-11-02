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
            "folder": "Grayhaven Systems LLC",
            "prometheus_datasource_name": "Prometheus",
            "stack_url": "https://grafana.example.invalid",
        }
    )
    return config


class AlertRuleTests(unittest.TestCase):
    def test_host_role_selects_cpu_window_without_changing_threshold_or_pending(self) -> None:
        checks = (
            "metrics_data", "cpu_utilization", "memory_utilization",
            "filesystem_space", "filesystem_inode", "swap_utilization",
            "restic_stale_backup",
        )
        for role, minutes in (("mcp", 15), ("web", 5), ("bastion", 5)):
            for threshold in (37.5, 91):
                with self.subTest(role=role, threshold=threshold):
                    host = {
                        "role": role, "fqdn": f"{role}.example.invalid",
                        "short_hostname": role, "project": "example",
                    }
                    config = {
                        "hosts": [host], "sites": [],
                        "labels": {"client": "example", "environment": "test"},
                        "grafana": {
                            "evaluation_group": "example", "contact_point": "example",
                        },
                        "thresholds": {
                            "cpu_percent": threshold, "memory_percent": 75,
                            "filesystem_percent": 85, "filesystem_inode_percent": 85,
                            "swap_percent": 75, "restic_stale_seconds": 3_600,
                        },
                        "uid_registry": {
                            alert_sync.host_rule_identity(host, check):
                            f"00000000-0000-4000-8000-{index:012d}"
                            for index, check in enumerate(checks, 1)
                        },
                    }
                    rules = alert_sync.host_metric_rules(config, "folder", "prom")
                    cpu = next(rule for rule in rules
                               if rule["labels"]["check"] == "cpu_utilization")
                    self.assertIn(f"[{minutes}m]", cpu["data"][0]["model"]["expr"])
                    self.assertIn(f"{minutes}-minute", cpu["annotations"]["description"])
                    self.assertEqual(cpu["data"][1]["model"]["conditions"][0]["evaluator"],
                                     {"type": "gte", "params": [threshold]})
                    self.assertEqual(cpu["for"], "5m")
                    self.assertEqual(cpu["uid"], config["uid_registry"][
                        alert_sync.host_rule_identity(host, "cpu_utilization")
                    ])
                    self.assertEqual(cpu["labels"]["role"], role)

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

    def test_all_managed_rules_have_fault_tolerant_pending_periods(self) -> None:
        rules = alert_sync.desired_rules(complete_config(), "folder", "prom")
        self.assertTrue(rules)
        for uid, rule in rules.items():
            with self.subTest(uid=uid):
                self.assertGreaterEqual(alert_sync.duration_seconds(rule["for"]), 300)
                self.assertEqual(rule["execErrState"], "Error")

    def test_helpers_and_rule_generation(self) -> None:
        self.assertTrue(alert_sync.valid_rule_uid("gh-f06f6e7870b532624240919a"))
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
            labels={"client": "grayhaven"},
            annotations={"summary": "Example"},
            contact_point="IRM",
        )
        self.assertEqual(
            rule["labels"][alert_sync.MANAGED_LABEL], alert_sync.MANAGED_VALUE
        )
        self.assertEqual(rule["uid"], "123e4567-e89b-42d3-a456-426614174000")

        config = complete_config()
        desired = alert_sync.desired_rules(config, "folder", "prom")
        titles = {item["title"] for item in desired.values()}
        self.assertIn("grayhaven-core-prod-web-01 Time Tracker service", titles)
        self.assertIn("Google Monitoring series >= 80% usage", titles)
        self.assertEqual(len(desired), len(set(desired)))
        self.assertTrue(
            alert_sync.is_managed(next(iter(desired.values())), "grayhaven")
        )
        self.assertFalse(alert_sync.is_managed({}, "grayhaven"))
        self.assertEqual(
            alert_sync.comparable({**rule, "unmanaged": True}),
            alert_sync.comparable(rule),
        )
        self.assertEqual(
            alert_sync.timestamp(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            "2026-01-01T00:00:00.000Z",
        )

    def test_usage_thresholds_are_strict_integer_boundaries(self) -> None:
        for value, expected in ((0, -1), (1, 0), (4_000, 3_999)):
            self.assertEqual(alert_sync.integer_at_least_threshold(value), expected)
        for value in (True, False, 1.5, -1, "4000", "4.0", "invalid", None, object()):
            with self.subTest(value=value), self.assertRaisesRegex(
                alert_sync.GrafanaError, "non-negative integer"
            ):
                alert_sync.integer_at_least_threshold(value)

    def test_usage_rules_use_canonical_raw_metrics_and_ownership(self) -> None:
        config = complete_config()
        rules = {
            rule["labels"]["check"]: rule
            for rule in alert_sync.external_service_rules(config, "folder", "prom")
        }
        contracts = {
            "gcs_class_a_monthly_operations": (
                "grayhaven_gcs_restic_billing_month_operations_total",
                "grayhaven_gcs_restic_monthly_operations_total",
                4_000,
                "gh-ebe005a9676dd3b8bc285f57",
            ),
            "gcs_class_b_monthly_operations": (
                "grayhaven_gcs_restic_billing_month_operations_total",
                "grayhaven_gcs_restic_monthly_operations_total",
                40_000,
                "gh-1b623645313da0fc789b1e43",
            ),
            "google_monitoring_monthly_billed_series": (
                "grayhaven_google_monitoring_billing_month_series_total",
                "grayhaven_google_monitoring_monthly_billed_series_total",
                800_000,
                "gh-4114f9f64c8c20c1465e15c1",
            ),
        }
        for check, (metric, old_metric, minimum, uid) in contracts.items():
            with self.subTest(check=check):
                rule = rules[check]
                query = rule["data"][0]["model"]["expr"]
                evaluator = rule["data"][1]["model"]["conditions"][0]["evaluator"]
                self.assertIn(metric, query)
                self.assertNotIn(old_metric, query)
                self.assertNotRegex(query, r"(?:>=|>|<=|<|==|!=)\s*-?\d")
                self.assertEqual(rule["uid"], uid)
                self.assertEqual(evaluator, {"params": [minimum - 1], "type": "gt"})
                self.assertEqual(rule["data"][1]["model"]["expression"], "A")
                self.assertEqual(rule["noDataState"], "OK")
                for value in (minimum - 1, minimum, minimum + 1):
                    self.assertEqual(value > evaluator["params"][0], value >= minimum)
                self.assertFalse((minimum - 1) > evaluator["params"][0])

        for check in (
            "gcs_service_telemetry_success",
            "gcs_operation_telemetry_stale",
            "google_monitoring_service_telemetry_success",
            "google_monitoring_telemetry_stale",
        ):
            self.assertEqual(rules[check]["noDataState"], "Alerting")

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
                "fqdn": "bastion.example.invalid",
                "role": "bastion",
                "short_hostname": "bastion",
            }
        )
        config["service_units"]["common"] = [
            {
                "check": "common_service",
                "name": "common.service",
                "summary": "Common is down",
            }
        ]
        config["service_units"]["control_node"] = [
            {
                "check": "control_service",
                "name": "control.service",
                "summary": "Control is down",
            }
        ]
        config["sites"][0]["dev_domain"] = "dev.example.invalid"
        config["uid_registry"].update(
            {
                "host:bastion.example.invalid:ansible_convergence": "00000000-0000-4000-8000-000000000001",
                "host:bastion.example.invalid:common_service": "00000000-0000-4000-8000-000000000002",
                "host:bastion.example.invalid:control_service": "00000000-0000-4000-8000-000000000003",
                "host:bastion.example.invalid:cpu_utilization": "00000000-0000-4000-8000-000000000004",
                "host:bastion.example.invalid:filesystem_inode": "00000000-0000-4000-8000-000000000005",
                "host:bastion.example.invalid:filesystem_space": "00000000-0000-4000-8000-000000000006",
                "host:bastion.example.invalid:memory_utilization": "00000000-0000-4000-8000-000000000007",
                "host:bastion.example.invalid:metrics_data": "00000000-0000-4000-8000-000000000008",
                "host:bastion.example.invalid:restic_stale_backup": "00000000-0000-4000-8000-000000000009",
                "host:bastion.example.invalid:swap_utilization": "00000000-0000-4000-8000-000000000010",
                "host:grayhaven-core-prod-web-01.grayhavensystems.com:common_service": "00000000-0000-4000-8000-000000000011",
                "domain:dev.example.invalid:dev_basic_auth_401": "00000000-0000-4000-8000-000000000012",
            }
        )
        titles = {
            rule["title"]
            for rule in alert_sync.desired_rules(config, "folder", "prom").values()
        }
        self.assertIn("bastion Control", titles)
        self.assertIn("bastion Ansible convergence", titles)
        self.assertIn("dev.example.invalid basic auth", titles)

        no_sites = complete_config()
        no_sites["sites"] = []
        self.assertEqual(alert_sync.site_rules(no_sites, "folder", "prom"), [])
        no_web = complete_config()
        no_web["hosts"][0]["role"] = "bastion"
        self.assertEqual(alert_sync.site_rules(no_web, "folder", "prom"), [])

        load_balancer = complete_config()
        load_balancer["tls_mode"] = "load_balancer"
        titles = {
            rule["title"]
            for rule in alert_sync.site_rules(load_balancer, "folder", "prom")
        }
        self.assertNotIn(
            "timetracker.grayhavensystems.com SSL certificate expired", titles
        )

    def test_sync_silence_and_main(self) -> None:
        config = complete_config()
        desired = alert_sync.desired_rules(config, "folder", "prom")
        first_uid, first_rule = next(iter(desired.items()))
        changed_existing = json.loads(json.dumps(first_rule))
        changed_existing["for"] = "99m"
        stale = {
            "uid": "stale",
            "labels": {"configured_by": "ansible", "client": "grayhaven"},
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

        old_usage = json.loads(json.dumps(list(desired.values())))
        old_metrics = {
            "gcs_class_a_monthly_operations": "grayhaven_gcs_restic_monthly_operations_total",
            "gcs_class_b_monthly_operations": "grayhaven_gcs_restic_monthly_operations_total",
            "google_monitoring_monthly_billed_series": "grayhaven_google_monitoring_monthly_billed_series_total",
        }
        for rule in old_usage:
            check = rule["labels"].get("check")
            if check in old_metrics:
                rule["data"][0]["model"]["expr"] = rule["data"][0]["model"]["expr"].replace(
                    {
                        "gcs_class_a_monthly_operations": "grayhaven_gcs_restic_billing_month_operations_total",
                        "gcs_class_b_monthly_operations": "grayhaven_gcs_restic_billing_month_operations_total",
                        "google_monitoring_monthly_billed_series": "grayhaven_google_monitoring_billing_month_series_total",
                    }[check],
                    old_metrics[check],
                )
                rule["data"][1]["model"]["conditions"][0]["evaluator"]["params"] = [0]
        update_only = mock.Mock()
        update_only.datasource_uid.return_value = "prom"
        update_only.folder_uid.return_value = "folder"
        update_only.alert_rules.return_value = old_usage
        update_only.update_rule_group_interval.return_value = False
        with mock.patch.object(alert_sync, "GrafanaClient", return_value=update_only):
            self.assertEqual(alert_sync.sync(config), (0, 3, 0))
        self.assertEqual(update_only.update_rule.call_count, 3)
        update_only.create_rule.assert_not_called()
        update_only.delete_rule.assert_not_called()

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
        self.assertEqual(payload["matchers"][0]["name"], "configured_by")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with (
                mock.patch("sys.argv", ["sync", "--config", str(path)]),
                mock.patch.object(alert_sync, "sync", return_value=(1, 2, 3)),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(alert_sync.main(), 0)
            self.assertIn(
                "changed=true created=1 updated=2 deleted=3", stdout.getvalue()
            )
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
                    alert_sync, "create_initial_silence", return_value="id"
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(alert_sync.main(), 0)
            self.assertIn("silence_id=id", stdout.getvalue())
