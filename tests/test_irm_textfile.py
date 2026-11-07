from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from tests.helpers import load_program


irm = load_program(
    "grayhaven_irm_alert_groups_textfile",
    "roles/observability/files/grayhaven-irm-alert-groups-textfile",
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


class IrmClientTests(unittest.TestCase):
    def test_request_discovery_and_pagination(self) -> None:
        client = irm.GrafanaIrmClient("https://grafana.example.invalid/", "token")
        with mock.patch.object(
            irm.urllib.request, "urlopen", return_value=Response('{"ok":true}')
        ):
            self.assertEqual(client._request("https://example.invalid"), {"ok": True})
        with mock.patch.object(
            irm.urllib.request, "urlopen", return_value=Response("{}")
        ) as urlopen:
            self.assertEqual(
                client._request(
                    "https://example.invalid", headers={"X-Test": "merged"}
                ),
                {},
            )
        request_headers = dict(urlopen.call_args.args[0].header_items())
        self.assertEqual(request_headers["X-test"], "merged")
        self.assertEqual(request_headers["Authorization"], "Bearer token")
        with mock.patch.object(irm.urllib.request, "urlopen", return_value=Response("", 204)):
            self.assertIsNone(client._request("https://example.invalid", expected=(204,)))
        error = urllib.error.HTTPError("url", 403, "bad", {}, io.BytesIO(b"denied"))
        with (
            mock.patch.object(irm.urllib.request, "urlopen", side_effect=error),
            self.assertRaisesRegex(irm.GrafanaIrmError, "HTTP 403"),
        ):
            client._request("https://example.invalid")
        retry = urllib.error.HTTPError("url", 503, "failed", {}, io.BytesIO(b""))
        with (
            mock.patch.object(irm.urllib.request, "urlopen", side_effect=retry),
            mock.patch.object(irm.time, "sleep"),
            self.assertRaisesRegex(irm.GrafanaIrmError, "retry budget"),
        ):
            client._request("https://example.invalid")
        with (
            mock.patch.object(
                irm.urllib.request, "urlopen", return_value=Response("{}", 201)
            ),
            self.assertRaisesRegex(irm.GrafanaIrmError, "unexpected HTTP status"),
        ):
            client._request("https://example.invalid")
        with (
            mock.patch.object(
                irm.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("failed"),
            ),
            mock.patch.object(irm.time, "sleep"),
            self.assertRaisesRegex(irm.GrafanaIrmError, "failed"),
        ):
            client._request("https://example.invalid")

        with mock.patch.object(
            client,
            "_request",
            return_value={"jsonData": {"onCallApiUrl": "https://irm.example.invalid/"}},
        ):
            self.assertEqual(client.discover_oncall_api_url(), "https://irm.example.invalid")
            self.assertEqual(client.discover_oncall_api_url(), "https://irm.example.invalid")
        with mock.patch.object(client, "_request", return_value={}):
            client.oncall_api_url = None
            with self.assertRaisesRegex(irm.GrafanaIrmError, "onCallApiUrl"):
                client.discover_oncall_api_url()
        with mock.patch.object(client, "_request", return_value=[]):
            client.oncall_api_url = None
            with self.assertRaisesRegex(irm.GrafanaIrmError, "not an object"):
                client.discover_oncall_api_url()

        client.oncall_api_url = "https://irm.example.invalid"
        with mock.patch.object(
            client,
            "_request",
            side_effect=[
                {"results": [{"id": "one"}], "next": "next"},
                {"results": [{"id": "two"}], "next": None},
            ],
        ):
            self.assertEqual(len(client.alert_groups("new")), 2)
        with mock.patch.object(client, "_request", return_value={"results": [{"id": "u"}]}):
            self.assertEqual(client.users(), [{"id": "u"}])
        with mock.patch.object(client, "_request", return_value=[{"login": "user"}, "bad"]):
            self.assertEqual(client.org_users(), [{"login": "user"}])
        with mock.patch.object(client, "_request", return_value={"id": "detail"}):
            self.assertEqual(client.alert_group_detail("a/b"), {"id": "detail"})

    def test_invalid_client_responses(self) -> None:
        client = irm.GrafanaIrmClient("https://grafana.example.invalid", "token")
        with mock.patch.object(client, "_request", return_value=[]):
            with self.assertRaises(irm.GrafanaIrmError):
                client.alert_group_detail("id")
        with mock.patch.object(client, "_request", return_value={}):
            with self.assertRaises(irm.GrafanaIrmError):
                client.org_users()
            with self.assertRaises(irm.GrafanaIrmError):
                client._paged_irm_results("url", "items")
        with mock.patch.object(client, "_request", return_value={"results": "bad"}):
            with self.assertRaises(irm.GrafanaIrmError):
                client._paged_irm_results("url", "items")
        with mock.patch.object(client, "_request", return_value=[]):
            with self.assertRaisesRegex(irm.GrafanaIrmError, "not an object"):
                client._paged_irm_results("url", "items")


class IrmRenderingTests(unittest.TestCase):
    def test_identity_and_rendering_helpers(self) -> None:
        identities = irm.IdentityResolver(
            [{"id": "1", "username": "operator"}],
            [{"login": "operator", "name": "Example Operator"}],
        )
        self.assertEqual(identities.username("1"), "operator")
        self.assertEqual(identities.display_name("1"), "Example Operator")
        self.assertEqual(identities.display_name("missing"), "")
        self.assertEqual(irm.label_value('a"b\nc'), 'a\\"b\\nc')
        self.assertEqual(irm.labels({"b": 2, "a": 1}), '{a="1",b="2"}')
        self.assertEqual(irm.metric_line("metric", {"a": "b"}, 1), 'metric{a="b"} 1')
        self.assertEqual(irm.safe_string(None), "")
        self.assertTrue(irm.safe_string("x" * 200).endswith("..."))
        self.assertEqual(irm.alert_group_url({}), "")
        self.assertEqual(irm.local_timestamp("invalid"), "")
        for stamp, zone, expected in (
            ("2026-07-26T20:00:00Z", "America/Chicago", "2026-07-26 3:00 PM CDT"),
            ("2026-01-26T20:00:00Z", "America/Chicago", "2026-01-26 2:00 PM CST"),
            ("2026-01-26T20:00:00Z", "UTC", "2026-01-26 8:00 PM UTC"),
            ("2026-01-26T20:00:00Z", "Invalid/Zone", ""),
        ):
            self.assertEqual(irm.local_timestamp(stamp, zone), expected)
        self.assertEqual(irm.state_sort_key("unknown"), "9")
        self.assertEqual(irm.state_display_name("new"), "Firing")
        self.assertEqual(irm.detail_user_display("bad", identities), "")
        self.assertEqual(
            irm.detail_user_display({"username": "operator"}, identities),
            "Example Operator",
        )
        self.assertEqual(
            irm.detail_user_display({"pk": "1"}, identities), "Example Operator"
        )
        self.assertEqual(irm.detail_user_display({}, identities), "")

        groups = {
            "new": [
                {
                    "id": "1",
                    "state": "new",
                    "title": "Alert",
                    "created_at": "2026-07-26T20:00:00Z",
                    "permalinks": {"web": "https://example.invalid/1"},
                },
                {"title": "Missing ID"},
            ],
            "acknowledged": [
                {"id": "2", "state": "acknowledged", "acknowledged_by": "1"}
            ],
            "silenced": [{"id": "3", "state": "silenced"}],
        }
        details = {
            "2": {"acknowledged_by_user": {"username": "operator"}},
            "3": {
                "silenced_by_user": {"pk": "1"},
                "silenced_until": "2026-07-27T20:00:00Z",
            },
        }
        rendered = irm.render(
            {"labels": {"client": "fixture", "environment": "test"}},
            groups,
            identities,
            details,
        )
        self.assertIn('user_display="Example Operator"', rendered)
        self.assertNotIn("Missing ID", rendered)

    def test_collects_details_only_for_groups_with_ids(self) -> None:
        groups = {
            state: (
                [{"id": "with-detail"}, {"title": "without-id"}]
                if state == "new"
                else []
            )
            for state in irm.DEFAULT_STATES
        }
        client = mock.Mock()
        client.alert_groups.side_effect = [groups[state] for state in irm.DEFAULT_STATES]
        client.users.return_value = []
        client.org_users.return_value = []
        client.alert_group_detail.return_value = {"id": "with-detail"}
        with mock.patch.object(irm, "GrafanaIrmClient", return_value=client):
            collected, identities, details = irm.collect(
                {
                    "grafana": {
                        "stack_url": "https://grafana.example.invalid",
                        "api_token": "token",
                    }
                }
            )

        self.assertEqual(collected, groups)
        self.assertIsInstance(identities, irm.IdentityResolver)
        self.assertEqual(details, {"with-detail": {"id": "with-detail"}})
        client.alert_group_detail.assert_called_once_with("with-detail")

    def test_atomic_collect_and_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / "metrics" / "irm.prom"
            irm.atomic_write(output, "content\n")
            self.assertEqual(output.read_text(encoding="utf-8"), "content\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            with mock.patch.object(
                irm.os, "replace", side_effect=OSError("expected")
            ):
                with self.assertRaises(OSError):
                    irm.atomic_write(output, "replacement\n")
            self.assertEqual(list(output.parent.glob(".irm.prom.*")), [])

            config = {
                "grafana": {
                    "stack_url": "https://grafana.example.invalid",
                    "api_token": "token",
                },
                "labels": {"client": "fixture", "environment": "test"},
                "output_path": str(output),
            }
            config_path = pathlib.Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            groups = {state: [] for state in irm.DEFAULT_STATES}
            identities = irm.IdentityResolver([], [])
            with (
                mock.patch("sys.argv", ["collector", "--config", str(config_path)]),
                mock.patch.object(
                    irm, "collect", return_value=(groups, identities, {})
                ),
            ):
                self.assertEqual(irm.main(), 0)

        fake = mock.Mock()
        fake.alert_groups.side_effect = lambda state: [{"id": state}]
        fake.users.return_value = []
        fake.org_users.return_value = []
        fake.alert_group_detail.side_effect = lambda item: {"id": item}
        with mock.patch.object(irm, "GrafanaIrmClient", return_value=fake):
            groups, _, details = irm.collect(
                {
                    "grafana": {
                        "stack_url": "https://grafana.example.invalid",
                        "api_token": "token",
                    }
                }
            )
        self.assertEqual(set(groups), set(irm.DEFAULT_STATES))
        self.assertEqual(set(details), set(irm.DEFAULT_STATES))
