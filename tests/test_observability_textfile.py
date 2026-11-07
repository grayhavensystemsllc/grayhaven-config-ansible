from __future__ import annotations

from datetime import datetime, timezone
import json
import pathlib
import runpy
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock
from zoneinfo import ZoneInfoNotFoundError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.helpers import load_program


collector = load_program(
    "grayhaven_observability_textfile",
    "roles/observability/files/grayhaven-observability-textfile",
)


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def window_at(timestamp: int, month: str = "2026-07") -> tuple[datetime, datetime, str]:
    end = datetime.fromtimestamp(timestamp, timezone.utc)
    return BILLING_START, end, month


BILLING_START = utc("2026-07-01T07:00:00Z")
BILLING_END = utc("2026-07-27T12:00:00Z")
BILLING_WINDOW = (BILLING_START, BILLING_END, "2026-07")


def metric_point(
    value: int | float,
    *,
    start: str = "2026-07-01T07:00:00Z",
    end: str = "2026-07-27T12:00:00Z",
) -> dict[str, object]:
    value_key = "int64Value" if isinstance(value, int) else "doubleValue"
    value_body: object = str(value) if value_key == "int64Value" else value
    return {
        "interval": {"startTime": start, "endTime": end},
        "value": {value_key: value_body},
    }


class Response:
    def __init__(self, body: object) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def host(*, control_node: bool = True) -> dict[str, object]:
    return {
        "client": "fixture",
        "control_node": control_node,
        "environment": "test",
        "fqdn": "control.example.invalid",
        "project": "security",
        "role": "bastion",
        "short_hostname": "bastion",
    }


def full_config() -> dict[str, object]:
    return {
        "host": host(),
        "hosts": [
            host(),
            {
                **host(control_node=False),
                "short_hostname": "web",
                "fqdn": "worker.example.invalid",
            },
        ],
        "restic": {"repositories": ["local", "remote"]},
        "gcs": {
            "enabled": True,
            "storage_status_product": {"title": "Fixture Storage", "id": "fixture-storage"},
            "monitoring_status_product": {"title": "Fixture Monitoring", "id": "fixture-monitoring"},
            "credentials_file": "/credentials.json",
            "location": "EXAMPLE-EAST4",
            "monitoring_usage_cache_file": "/cache/monitoring.json",
            "monitoring_usage_refresh_seconds": 3600,
            "monitoring_usage_stale_seconds": 10800,
            "operation_cache_file": "/cache/gcs.json",
            "operation_refresh_seconds": 3600,
            "operation_stale_seconds": 10800,
            "project_id": "fixture",
        },
        "proton": {
            "enabled": True,
            "services": [{"name": "Example", "component_name": "Mock Component"}],
        },
        "fail2ban": {
            "jails": [
                {
                    "name": "sshd",
                    "display_name": "SSH",
                    "domain": "",
                    "category": "access",
                }
            ]
        },
    }


class HttpAndGoogleTests(unittest.TestCase):
    def test_google_billing_windows_are_deterministic_and_pacific(self) -> None:
        cases = (
            (
                "2026-08-01T06:59:59Z",
                "2026-07-01T07:00:00Z",
                "2026-07",
            ),
            (
                "2026-08-01T07:00:00Z",
                "2026-08-01T07:00:00Z",
                "2026-08",
            ),
            (
                "2026-05-15T12:34:56Z",
                "2026-05-01T07:00:00Z",
                "2026-05",
            ),
            (
                "2026-01-15T12:34:56Z",
                "2026-01-01T08:00:00Z",
                "2026-01",
            ),
            (
                "2028-02-29T12:34:56Z",
                "2028-02-01T08:00:00Z",
                "2028-02",
            ),
            (
                "2026-12-31T07:59:59Z",
                "2026-12-01T08:00:00Z",
                "2026-12",
            ),
            (
                "2027-01-01T08:00:00Z",
                "2027-01-01T08:00:00Z",
                "2027-01",
            ),
            (
                "2027-01-01T08:00:01Z",
                "2027-01-01T08:00:00Z",
                "2027-01",
            ),
        )
        for end_value, start_value, month in cases:
            start, end, actual_month = collector.google_billing_window(utc(end_value))
            self.assertEqual(
                (start, end, actual_month), (utc(start_value), utc(end_value), month)
            )

        with mock.patch.object(collector, "datetime") as clock:
            clock.now.return_value = utc("2026-08-01T06:59:59Z")
            self.assertEqual(
                collector.google_billing_window(),
                (
                    utc("2026-07-01T07:00:00Z"),
                    utc("2026-08-01T06:59:59Z"),
                    "2026-07",
                ),
            )
            clock.now.assert_called_once_with(collector.timezone.utc)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            collector.google_billing_window(datetime(2026, 8, 1, 7, 0, 0))
        with (
            mock.patch(
                "zoneinfo.ZoneInfo",
                side_effect=ZoneInfoNotFoundError("expected invalid timezone"),
            ),
            self.assertRaises(ZoneInfoNotFoundError),
        ):
            runpy.run_path(
                str(
                    pathlib.Path(__file__).parents[1]
                    / "roles/observability/files/grayhaven-observability-textfile"
                )
            )

    def test_http_token_and_values(self) -> None:
        with mock.patch.object(
            collector.urllib.request, "urlopen", return_value=Response({"ok": True})
        ):
            self.assertEqual(
                collector.request_json("https://example.invalid"), {"ok": True}
            )
            self.assertEqual(
                collector.post_form_json("https://example.invalid", {"a": "b"}),
                {"ok": True},
            )
        self.assertEqual(collector.base64url(b"test"), "dGVzdA")
        self.assertEqual(collector.point_value({"value": {"int64Value": "2"}}), 2.0)
        self.assertEqual(collector.point_value({"value": {"doubleValue": 2.5}}), 2.5)
        self.assertEqual(collector.point_value({}), 0.0)
        self.assertTrue(
            collector.point_starts_at_or_after(metric_point(1), BILLING_START)
        )
        self.assertTrue(
            collector.point_starts_at_or_after(
                metric_point(1, start="2026-07-27T11:00:00Z"), BILLING_START
            )
        )
        for invalid_point in (
            [],
            metric_point(1, start="2026-06-30T07:00:00Z"),
            {"interval": {"startTime": "2026-07-01T07:00:00Z"}},
            {"interval": {"startTime": 1, "endTime": "2026-07-27T12:00:00Z"}},
            {
                "interval": {
                    "startTime": "not-a-time",
                    "endTime": "2026-07-27T12:00:00Z",
                }
            },
            {
                "interval": {
                    "startTime": "2026-07-27T12:00:00Z",
                    "endTime": "2026-07-01T07:00:00Z",
                }
            },
            {"interval": []},
            {},
        ):
            self.assertFalse(
                collector.point_starts_at_or_after(invalid_point, BILLING_START)
            )

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "credentials.json"
            path.write_text(
                json.dumps(
                    {
                        "client_email": "service@example.invalid",
                        "private_key": pem,
                        "token_uri": "https://oauth.example.invalid/token",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(collector.time, "time", return_value=1000),
                mock.patch.object(
                    collector,
                    "post_form_json",
                    return_value={"access_token": "token"},
                ) as post,
            ):
                self.assertEqual(collector.monitoring_token(str(path)), "token")
            self.assertIn("assertion", post.call_args.args[1])
            with (
                mock.patch.object(
                    collector.serialization,
                    "load_pem_private_key",
                    return_value=object(),
                ),
                self.assertRaisesRegex(RuntimeError, "not an RSA"),
            ):
                collector.google_token(str(path), "scope")
            with (
                mock.patch.object(collector, "post_form_json", return_value={}),
                self.assertRaisesRegex(RuntimeError, "access token"),
            ):
                collector.google_token(str(path), "scope")

    def test_monitoring_queries_and_operation_classes(self) -> None:
        with mock.patch.object(
            collector, "request_json", return_value={"ok": True}
        ) as request:
            self.assertEqual(
                collector.monitoring_request(
                    "project/name", "timeSeries", {"a": "b"}, "token"
                ),
                {"ok": True},
            )
        self.assertIn("projects/project%2Fname/timeSeries", request.call_args.args[0])
        self.assertEqual(
            collector.gcs_operation_class("storage.objects.insert"), "Class A"
        )
        self.assertEqual(
            collector.gcs_operation_class("storage.objects.get"), "Class B"
        )
        self.assertEqual(collector.gcs_operation_class("WriteObject"), "Class A")
        self.assertEqual(collector.gcs_operation_class("CloneObject.From"), "Class A")
        self.assertEqual(
            collector.gcs_operation_class("UpdateBucketMetadata"), "Class A"
        )
        self.assertEqual(collector.gcs_operation_class("ReadObject"), "Class B")
        self.assertEqual(collector.gcs_operation_class("GetObjectMetadata"), "Class B")
        self.assertEqual(collector.gcs_operation_class("GetBucketMetadata"), "Class B")
        self.assertIsNone(collector.gcs_operation_class("DeleteObject"))
        self.assertIsNone(collector.gcs_operation_class("storage.buckets.delete"))
        self.assertIsNone(collector.gcs_operation_class("unknown"))
        self.assertIsNone(collector.gcs_operation_class("storage.objects.getter"))
        self.assertEqual(
            collector.gcs_operation_counts(
                "project", "token", set(), BILLING_START, BILLING_END
            ),
            {"Class A": 0.0, "Class B": 0.0},
        )

        pages = [
            {
                "timeSeries": [
                    {
                        "resource": {"labels": {"bucket_name": "bucket"}},
                        "metric": {"labels": {"method": "storage.objects.insert"}},
                        "points": [
                            metric_point(
                                100,
                                start="2026-06-30T07:00:00Z",
                                end="2026-07-01T07:00:00Z",
                            ),
                            metric_point(2),
                        ],
                    },
                    {
                        "resource": {"labels": {"bucket_name": "other"}},
                        "metric": {"labels": {"method": "storage.objects.get"}},
                        "points": [metric_point(99)],
                    },
                ],
                "nextPageToken": "next",
            },
            {
                "timeSeries": [
                    {
                        "resource": {"labels": {"bucket_name": "bucket"}},
                        "metric": {"labels": {"method": "storage.objects.get"}},
                        "points": [metric_point(3.5)],
                    },
                    {
                        "resource": {"labels": {"bucket_name": "bucket"}},
                        "metric": {"labels": {"method": "DeleteObject"}},
                        "points": [metric_point(4)],
                    },
                    {
                        "resource": {"labels": {"bucket_name": "bucket"}},
                        "metric": {"labels": {"method": "future.method"}},
                        "points": [metric_point(5)],
                    },
                    {
                        "resource": {"labels": {"bucket_name": "bucket"}},
                        "metric": {"labels": {"method": "storage.objects.insert"}},
                        "points": [{"value": {"int64Value": "7"}}],
                    },
                ]
            },
        ]
        with mock.patch.object(
            collector, "monitoring_request", side_effect=pages
        ) as request:
            self.assertEqual(
                collector.gcs_operation_counts(
                    "project", "token", {"bucket"}, BILLING_START, BILLING_END
                ),
                {"Class A": 2.0, "Class B": 3.5},
            )
        self.assertEqual(request.call_count, 2)
        query = request.call_args_list[0].args[2]
        self.assertIn('resource.labels.bucket_name = "bucket"', query["filter"])
        self.assertEqual(query["interval.startTime"], "2026-07-01T07:00:00Z")
        self.assertEqual(query["interval.endTime"], "2026-07-27T12:00:00Z")
        self.assertEqual(query["aggregation.alignmentPeriod"], "2264400s")
        with mock.patch.object(
            collector, "monitoring_request", return_value={"timeSeries": []}
        ):
            self.assertEqual(
                collector.gcs_operation_counts(
                    "project", "token", {"bucket"}, BILLING_START, BILLING_END
                ),
                {"Class A": 0.0, "Class B": 0.0},
            )

        with mock.patch.object(
            collector,
            "monitoring_request",
            return_value={
                "timeSeries": [
                    {
                        "points": [
                            metric_point(
                                100,
                                start="2026-06-30T07:00:00Z",
                                end="2026-07-01T07:00:00Z",
                            ),
                            metric_point(12),
                            {"value": {"int64Value": "9"}},
                            {
                                "interval": {
                                    "startTime": 1,
                                    "endTime": "2026-07-27T12:00:00Z",
                                },
                                "value": {"int64Value": "8"},
                            },
                        ]
                    }
                ]
            },
        ) as request:
            self.assertEqual(
                collector.monitoring_billed_series_total(
                    "project", "token", BILLING_START, BILLING_END
                ),
                12,
            )
        query = request.call_args.args[2]
        self.assertEqual(query["interval.startTime"], "2026-07-01T07:00:00Z")
        self.assertEqual(query["interval.endTime"], "2026-07-27T12:00:00Z")
        with mock.patch.object(
            collector, "monitoring_request", return_value={"timeSeries": []}
        ):
            self.assertEqual(
                collector.monitoring_billed_series_total(
                    "project", "token", BILLING_START, BILLING_END
                ),
                0,
            )
        with (
            mock.patch.object(
                collector,
                "monitoring_request",
                return_value={"timeSeries": [{}, {}]},
            ),
            self.assertRaisesRegex(RuntimeError, "cardinality"),
        ):
            collector.monitoring_billed_series_total(
                "project", "token", BILLING_START, BILLING_END
            )


class CacheTests(unittest.TestCase):
    def test_cache_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "cache.json"
            self.assertIsNone(
                collector.load_gcs_operation_cache(
                    path, "project", {"bucket"}, "2026-07"
                )
            )
            valid = {
                "version": collector.GCS_OPERATION_CACHE_VERSION,
                "project_id": "project",
                "month": "2026-07",
                "bucket_names": ["bucket"],
                "counts": {"Class A": 1, "Class B": 2},
                "refreshed_at": 100,
            }
            path.write_text(json.dumps(valid), encoding="utf-8")
            self.assertEqual(
                collector.load_gcs_operation_cache(
                    path, "project", {"bucket"}, "2026-07"
                ),
                valid,
            )
            path.write_text("[]", encoding="utf-8")
            self.assertIsNone(
                collector.load_gcs_operation_cache(
                    path, "project", {"bucket"}, "2026-07"
                )
            )
            for key, value in (
                ("version", collector.GCS_OPERATION_CACHE_VERSION - 1),
                ("project_id", "other"),
                ("month", "2026-06"),
                ("bucket_names", []),
                ("counts", []),
                ("refreshed_at", "bad"),
                ("bucket_names", "bucket"),
                ("refreshed_at", True),
            ):
                invalid = dict(valid)
                invalid[key] = value
                path.write_text(json.dumps(invalid), encoding="utf-8")
                self.assertIsNone(
                    collector.load_gcs_operation_cache(
                        path, "project", {"bucket"}, "2026-07"
                    )
                )
            invalid = dict(valid)
            invalid["counts"] = {
                "Class A": True,
                "Class B": 2,
            }
            path.write_text(json.dumps(invalid), encoding="utf-8")
            self.assertIsNone(
                collector.load_gcs_operation_cache(
                    path, "project", {"bucket"}, "2026-07"
                )
            )
            invalid = dict(valid)
            invalid["counts"] = {"Class A": 1, "Class B": 2, "Free": 3}
            path.write_text(json.dumps(invalid), encoding="utf-8")
            self.assertIsNone(
                collector.load_gcs_operation_cache(
                    path, "project", {"bucket"}, "2026-07"
                )
            )
            missing = dict(valid)
            del missing["counts"]
            path.write_text(json.dumps(missing), encoding="utf-8")
            self.assertIsNone(
                collector.load_gcs_operation_cache(
                    path, "project", {"bucket"}, "2026-07"
                )
            )

            usage = {
                "version": collector.GOOGLE_MONITORING_USAGE_CACHE_VERSION,
                "project_id": "project",
                "month": "2026-07",
                "billed_series": 10,
                "refreshed_at": 100,
            }
            path.write_text(json.dumps(usage), encoding="utf-8")
            self.assertEqual(
                collector.load_google_monitoring_usage_cache(
                    path, "project", "2026-07"
                ),
                usage,
            )
            for key, value in (
                ("version", collector.GOOGLE_MONITORING_USAGE_CACHE_VERSION - 1),
                ("project_id", "other"),
                ("month", "2026-06"),
                ("billed_series", "bad"),
                ("refreshed_at", "bad"),
                ("billed_series", True),
                ("refreshed_at", True),
            ):
                invalid_usage = dict(usage)
                invalid_usage[key] = value
                path.write_text(json.dumps(invalid_usage), encoding="utf-8")
                self.assertIsNone(
                    collector.load_google_monitoring_usage_cache(
                        path, "project", "2026-07"
                    )
                )
            path.write_text("[]", encoding="utf-8")
            self.assertIsNone(
                collector.load_google_monitoring_usage_cache(path, "project", "2026-07")
            )
            missing_usage = dict(usage)
            del missing_usage["billed_series"]
            path.write_text(json.dumps(missing_usage), encoding="utf-8")
            self.assertIsNone(
                collector.load_google_monitoring_usage_cache(path, "project", "2026-07")
            )

    def test_cached_refresh_and_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            gcs_path = pathlib.Path(temp_dir) / "gcs.json"
            usage_path = pathlib.Path(temp_dir) / "usage.json"
            config = {
                "credentials_file": "credentials",
                "operation_cache_file": str(gcs_path),
                "operation_refresh_seconds": 60,
                "operation_stale_seconds": 120,
                "monitoring_usage_cache_file": str(usage_path),
                "monitoring_usage_refresh_seconds": 60,
                "monitoring_usage_stale_seconds": 120,
                "project_id": "project",
            }
            with (
                mock.patch.object(
                    collector, "google_billing_window", return_value=window_at(1000)
                ),
                mock.patch.object(collector, "monitoring_token", return_value="token"),
                mock.patch.object(
                    collector,
                    "gcs_operation_counts",
                    return_value={"Class A": 1, "Class B": 2},
                ),
            ):
                self.assertEqual(
                    collector.cached_gcs_operation_counts(config, {"bucket"}),
                    (
                        {"Class A": 1.0, "Class B": 2.0},
                        1000,
                        True,
                        "2026-07",
                    ),
                )
            self.assertEqual(gcs_path.stat().st_mode & 0o777, 0o600)
            with (
                mock.patch.object(
                    collector, "google_billing_window", return_value=window_at(1100)
                ),
                mock.patch.object(
                    collector, "monitoring_token", side_effect=OSError("expected")
                ),
            ):
                self.assertEqual(
                    collector.cached_gcs_operation_counts(config, {"bucket"})[0],
                    {"Class A": 1.0, "Class B": 2.0},
                )
            with (
                mock.patch.object(
                    collector, "google_billing_window", return_value=window_at(1201)
                ),
                mock.patch.object(
                    collector, "monitoring_token", side_effect=OSError("expected")
                ),
            ):
                self.assertFalse(
                    collector.cached_gcs_operation_counts(config, {"bucket"})[2]
                )
            gcs_path.unlink()
            with (
                mock.patch.object(
                    collector, "google_billing_window", return_value=window_at(1200)
                ),
                mock.patch.object(
                    collector, "monitoring_token", side_effect=OSError("expected")
                ),
                self.assertRaises(OSError),
            ):
                collector.cached_gcs_operation_counts(config, {"bucket"})

            with (
                mock.patch.object(
                    collector, "google_billing_window", return_value=window_at(2000)
                ),
                mock.patch.object(collector, "monitoring_token", return_value="token"),
                mock.patch.object(
                    collector, "monitoring_billed_series_total", return_value=123
                ),
            ):
                self.assertEqual(
                    collector.cached_google_monitoring_usage(config),
                    (123.0, 2000, True, "2026-07"),
                )
            self.assertEqual(usage_path.stat().st_mode & 0o777, 0o600)
            with (
                mock.patch.object(
                    collector, "google_billing_window", return_value=window_at(2050)
                ),
                mock.patch.object(collector, "monitoring_token") as token,
                mock.patch.object(collector, "monitoring_billed_series_total") as query,
            ):
                self.assertEqual(
                    collector.cached_google_monitoring_usage(config),
                    (123.0, 2000, True, "2026-07"),
                )
            token.assert_not_called()
            query.assert_not_called()
            with (
                mock.patch.object(
                    collector, "google_billing_window", return_value=window_at(2100)
                ),
                mock.patch.object(
                    collector, "monitoring_token", side_effect=RuntimeError("expected")
                ),
            ):
                self.assertEqual(
                    collector.cached_google_monitoring_usage(config)[:2],
                    (123.0, 2000),
                )

            with (
                mock.patch.object(
                    collector, "google_billing_window", return_value=window_at(2201)
                ),
                mock.patch.object(
                    collector, "monitoring_token", side_effect=RuntimeError("expected")
                ),
            ):
                self.assertEqual(
                    collector.cached_google_monitoring_usage(config),
                    (123.0, 2000, False, "2026-07"),
                )
            usage_path.unlink()
            with (
                mock.patch.object(
                    collector, "google_billing_window", return_value=window_at(2200)
                ),
                mock.patch.object(
                    collector, "monitoring_token", side_effect=RuntimeError("expected")
                ),
                self.assertRaises(RuntimeError),
            ):
                collector.cached_google_monitoring_usage(config)

    def test_refresh_cadence_before_at_and_after_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "gcs.json"
            config = {
                "credentials_file": "credentials",
                "operation_cache_file": str(path),
                "operation_refresh_seconds": 60,
                "operation_stale_seconds": 120,
                "project_id": "project",
            }
            query = mock.Mock(
                side_effect=[
                    {"Class A": 1, "Class B": 0},
                    {"Class A": 2, "Class B": 0},
                    {"Class A": 3, "Class B": 0},
                ]
            )
            windows = [
                window_at(1000),
                window_at(1059),
                window_at(1060),
                window_at(1061, "2026-08"),
            ]
            with (
                mock.patch.object(
                    collector, "google_billing_window", side_effect=windows
                ),
                mock.patch.object(collector, "monitoring_token", return_value="token"),
                mock.patch.object(collector, "gcs_operation_counts", query),
            ):
                self.assertEqual(
                    collector.cached_gcs_operation_counts(config, {"bucket"})[0][
                        "Class A"
                    ],
                    1.0,
                )
                self.assertEqual(
                    collector.cached_gcs_operation_counts(config, {"bucket"})[0][
                        "Class A"
                    ],
                    1.0,
                )
                self.assertEqual(
                    collector.cached_gcs_operation_counts(config, {"bucket"})[0][
                        "Class A"
                    ],
                    2.0,
                )
                self.assertEqual(
                    collector.cached_gcs_operation_counts(config, {"bucket"})[3],
                    "2026-08",
                )
            self.assertEqual(query.call_count, 3)


class PublicStatusAndFail2banTests(unittest.TestCase):
    def test_google_and_proton_status(self) -> None:
        self.assertTrue(collector.location_matches("EXAMPLE-EAST4", []))
        self.assertTrue(collector.location_matches("US", [{"title": "United States"}]))
        self.assertTrue(collector.location_matches("EXAMPLE-EAST4", [{"id": "global"}]))
        self.assertTrue(collector.location_matches("EXAMPLE-EAST4", [{"id": "example-east4"}]))
        self.assertTrue(
            collector.location_matches("EXAMPLE-EAST4", [{"title": "region example-east4 zone"}])
        )
        self.assertFalse(collector.location_matches("EXAMPLE-EAST4", [{"id": "europe"}]))
        with mock.patch.object(
            collector,
            "request_json",
            return_value={"products": [{"title": "Mock Product", "id": "found"}]},
        ):
            self.assertEqual(
                collector.google_cloud_product_id("Mock Product", "fallback"),
                "found",
            )
        with mock.patch.object(
            collector, "request_json", side_effect=urllib.error.URLError("expected")
        ):
            self.assertEqual(
                collector.google_cloud_product_id("Mock Product", "fallback"),
                "fallback",
            )
        with mock.patch.object(
            collector,
            "request_json",
            return_value={"products": [{"title": "Other", "id": "other"}]},
        ):
            self.assertEqual(
                collector.google_cloud_product_id("Mock Product", "fallback"),
                "fallback",
            )

        incidents = [
            {
                "affected_products": [{"id": "product"}],
                "currently_affected_locations": [{"id": "global"}],
                "status_impact": "SERVICE_DISRUPTION",
            }
        ]
        with (
            mock.patch.object(
                collector, "google_cloud_product_id", return_value="product"
            ),
            mock.patch.object(collector, "request_json", return_value=incidents),
        ):
            self.assertEqual(
                collector.google_cloud_status("Product", "fallback", "US"),
                ("Service Disruption", 0, 1),
            )
        with (
            mock.patch.object(
                collector, "google_cloud_product_id", return_value="product"
            ),
            mock.patch.object(collector, "request_json", return_value=[]),
        ):
            self.assertEqual(
                collector.google_cloud_status("Product", "fallback", "US"),
                ("Operational", 1, 1),
            )
        with mock.patch.object(
            collector, "request_json", side_effect=OSError("expected")
        ):
            self.assertEqual(
                collector.google_cloud_status("Product", "fallback", "US"),
                ("Unknown", 0, 0),
            )
        with (
            mock.patch.object(
                collector, "google_cloud_product_id", return_value="product"
            ),
            mock.patch.object(
                collector,
                "request_json",
                return_value=[
                    {**incidents[0], "end": "finished"},
                    {**incidents[0], "affected_products": [{"id": "other"}]},
                    {
                        **incidents[0],
                        "currently_affected_locations": [{"id": "europe"}],
                    },
                ],
            ),
        ):
            self.assertEqual(
                collector.google_cloud_status("Product", "fallback", "US"),
                ("Operational", 1, 1),
            )
        with mock.patch.object(
            collector, "google_cloud_status", return_value=("Operational", 1, 1)
        ) as status:
            self.assertEqual(collector.gcs_status("US", {"title": "Fixture Storage", "id": "fixture-storage"}), ("Operational", 1, 1))
            self.assertEqual(
                collector.google_monitoring_status("US", {"title": "Fixture Monitoring", "id": "fixture-monitoring"}), ("Operational", 1, 1)
            )
        self.assertEqual(status.call_count, 2)

        with mock.patch.object(
            collector,
            "request_json",
            return_value={
                "components": [{"name": "Mock Component", "status": "operational"}, {}]
            },
        ):
            self.assertIn("Mock Component", collector.proton_components())
        self.assertEqual(collector.proton_status_value("operational"), 1)
        self.assertEqual(collector.proton_status_value("degraded"), 0)
        self.assertEqual(collector.render_status("partial_outage"), "Partial Outage")
        self.assertEqual(collector.render_status(""), "Unknown")

    def test_fail2ban_parsing(self) -> None:
        self.assertEqual(collector.normalize_ip_token("[192.0.2.1],"), "192.0.2.1")
        self.assertEqual(collector.normalize_ip_token("192.0.2.1:"), "192.0.2.1")
        self.assertEqual(collector.normalize_ip_token("invalid"), "")
        self.assertEqual(
            collector.format_fail2ban_expiry_date("permanent"), "permanent"
        )
        self.assertRegex(
            collector.format_fail2ban_expiry_date("expires=2026-07-27 09:00:00"),
            r"2026-07-27 \d{1,2}:00 (AM|PM)",
        )
        self.assertEqual(
            collector.format_fail2ban_expiry_date("expires=2026-02-30 09:00:00"),
            "expires=2026-02-30 09:00:00",
        )
        success = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "Status for the jail: sshd\n"
                "|- Currently banned: 1\n"
                "|- Total banned: 3\n"
                "`- Banned IP list: 192.0.2.1\n"
            ),
            stderr="",
        )
        with mock.patch.object(collector.subprocess, "run", return_value=success):
            self.assertEqual(
                collector.fail2ban_status("sshd"), (1, 1, 3, ["192.0.2.1"])
            )
        with mock.patch.object(
            collector.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, stdout="192.0.2.1 2026-07-27 09:00:00\n", stderr=""
            ),
        ):
            self.assertIn("192.0.2.1", collector.fail2ban_ban_expiry_dates("sshd"))
        with mock.patch.object(
            collector.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout="ignored-token 192.0.2.2 2026-07-27 09:00:00\n",
                stderr="",
            ),
        ):
            expiry_dates = collector.fail2ban_ban_expiry_dates("sshd")
        self.assertIn("192.0.2.2", expiry_dates)
        with mock.patch.object(
            collector.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout="not-an-ip\n192.0.2.4 2026-07-27 09:00:00\n",
                stderr="",
            ),
        ):
            expiry_dates = collector.fail2ban_ban_expiry_dates("sshd")
        self.assertEqual(set(expiry_dates), {"192.0.2.4"})
        for result in (
            OSError("expected"),
            subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        ):
            kwargs = (
                {"side_effect": result}
                if isinstance(result, OSError)
                else {"return_value": result}
            )
            with mock.patch.object(collector.subprocess, "run", **kwargs):
                self.assertEqual(collector.fail2ban_ban_expiry_dates("sshd"), {})
        with mock.patch.object(
            collector.subprocess, "run", side_effect=OSError("expected")
        ):
            self.assertEqual(collector.fail2ban_status("sshd"), (0, 0, 0, []))
        with mock.patch.object(
            collector.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        ):
            self.assertEqual(collector.fail2ban_status("sshd"), (0, 0, 0, []))
        malformed_counts = subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "|- Currently banned: many\n"
                "|- Total banned: unknown\n"
                "`- Banned IP list: 192.0.2.3\n"
            ),
            stderr="",
        )
        with mock.patch.object(
            collector.subprocess, "run", return_value=malformed_counts
        ):
            self.assertEqual(
                collector.fail2ban_status("sshd"), (1, 0, 0, ["192.0.2.3"])
            )


class RenderingTests(unittest.TestCase):
    def test_host_inventory_only_emitted_on_control_node(self) -> None:
        fake_host = host()
        inventory = {"host": fake_host, "hosts": [fake_host]}
        self.assertIn(fake_host["fqdn"], collector.render(inventory))
        inventory["host"] = host(control_node=False)
        self.assertNotIn(fake_host["fqdn"], collector.render(inventory))

    def test_render_sections_and_failures(self) -> None:
        config = full_config()
        operation_counts = {"Class A": 17, "Class B": 29}
        monitoring_usage = 2345
        month = "2037-04"
        disabled = full_config()
        disabled["gcs"]["enabled"] = False
        self.assertEqual(collector.render_gcs_metrics(disabled), [])
        non_control = full_config()
        non_control["host"] = host(control_node=False)
        self.assertEqual(collector.render_gcs_metrics(non_control), [])
        self.assertEqual(collector.render_google_monitoring_metrics(disabled), [])
        self.assertEqual(collector.render_google_monitoring_metrics(non_control), [])
        self.assertEqual(
            collector.render_expected_restic_repositories(
                {"host": host(control_node=False)}
            ),
            [],
        )
        with (
            mock.patch.object(
                collector, "gcs_status", return_value=("Operational", 1, 1)
            ),
            mock.patch.object(
                collector,
                "cached_gcs_operation_counts",
                return_value=(
                    operation_counts,
                    1000,
                    True,
                    month,
                ),
            ),
            mock.patch.object(
                collector,
                "google_monitoring_status",
                return_value=("Operational", 1, 1),
            ),
            mock.patch.object(
                collector,
                "cached_google_monitoring_usage",
                return_value=(monitoring_usage, 1000, True, month),
            ),
            mock.patch.object(
                collector,
                "proton_components",
                return_value={"Mock Component": {"status": "operational"}},
            ),
            mock.patch.object(
                collector, "fail2ban_status", return_value=(1, 1, 2, ["192.0.2.1"])
            ),
            mock.patch.object(
                collector,
                "fail2ban_ban_expiry_dates",
                return_value={"192.0.2.1": "date"},
            ),
        ):
            rendered = collector.render(config)
        samples = [line for line in rendered.splitlines() if not line.startswith("#")]
        shared_labels = (
            f'client="{config["host"]["client"]}"',
            f'project_id="{config["gcs"]["project_id"]}"',
            f'month="{month}"',
        )
        for operation, value in operation_counts.items():
            self.assertTrue(any(
                all(label in line for label in shared_labels)
                and f'operation_class="{operation}"' in line
                and line.endswith(f" {value}")
                for line in samples
            ))
        self.assertTrue(any(
            all(label in line for label in shared_labels)
            and line.endswith(f" {monitoring_usage}")
            for line in samples
        ))

        with (
            mock.patch.object(collector, "gcs_status", return_value=("Unknown", 0, 0)),
            mock.patch.object(
                collector,
                "cached_gcs_operation_counts",
                side_effect=RuntimeError("expected"),
            ),
        ):
            self.assertTrue(any(line.endswith(" 0") for line in collector.render_gcs_metrics(config)))
        with (
            mock.patch.object(
                collector, "google_monitoring_status", return_value=("Unknown", 0, 0)
            ),
            mock.patch.object(
                collector,
                "cached_google_monitoring_usage",
                side_effect=RuntimeError("expected"),
            ),
        ):
            self.assertTrue(any(line.endswith(" 0") for line in collector.render_google_monitoring_metrics(config)))
        with mock.patch.object(
            collector, "proton_components", side_effect=OSError("expected")
        ):
            self.assertTrue(any(line.endswith(" 0") for line in collector.render_proton_metrics(config)))
        self.assertEqual(
            collector.render_proton_metrics(
                {"host": host(), "proton": {"enabled": True}}
            ),
            [],
        )
        self.assertEqual(collector.render_fail2ban_metrics({"host": host()}), [])

    def test_atomic_write_and_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            output = root / "metrics.prom"
            config_path = root / "config.json"
            fake_host = host(control_node=True)
            config_path.write_text(json.dumps({"host": fake_host, "hosts": [fake_host]}), encoding="utf-8")
            collector.atomic_write(output, "content\n", mode=0o600)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with mock.patch.object(
                collector.os, "replace", side_effect=OSError("expected")
            ):
                with self.assertRaises(OSError):
                    collector.atomic_write(output, "replacement\n")
            self.assertEqual(list(root.glob(".metrics.prom.*")), [])
            with (
                mock.patch.object(collector, "CONFIG_PATH", config_path),
                mock.patch.object(collector, "OUTPUT_PATH", output),
            ):
                self.assertEqual(collector.main(), 0)
            self.assertIn(fake_host["fqdn"], output.read_text(encoding="utf-8"))
