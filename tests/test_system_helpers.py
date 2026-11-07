from __future__ import annotations

import io
import json
import pathlib
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock

from tests.helpers import load_program


gcs = load_program("grayhaven_gcs_cleanup", "files/grayhaven-gcs-restic-bucket-cleanup")
reboot = load_program("grayhaven_reboot_notify", "files/grayhaven-reboot-notify")
motd = load_program("grayhaven_refresh_motd", "files/grayhaven-refresh-motd")


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


class GcsCleanupTests(unittest.TestCase):
    def test_credentials_token(self) -> None:
        credentials = mock.Mock(token="token")
        with (
            mock.patch.object(
                gcs.service_account.Credentials,
                "from_service_account_file",
                return_value=credentials,
            ) as factory,
            mock.patch.object(gcs, "Request", return_value="request"),
        ):
            self.assertEqual(gcs.credentials_token("credentials.json"), "token")
        factory.assert_called_once_with("credentials.json", scopes=[gcs.GCS_SCOPE])
        credentials.refresh.assert_called_once_with("request")

        credentials.token = None
        with (
            mock.patch.object(
                gcs.service_account.Credentials,
                "from_service_account_file",
                return_value=credentials,
            ),
            mock.patch.object(gcs, "Request"),
            self.assertRaisesRegex(gcs.CleanupError, "access token"),
        ):
            gcs.credentials_token("credentials.json")

    def test_request_success_and_failures(self) -> None:
        with mock.patch.object(gcs.urllib.request, "urlopen", return_value=Response('{"ok": true}')):
            self.assertEqual(gcs.request("POST", "/test", "token", {"a": 1}), {"ok": True})
        with mock.patch.object(gcs.urllib.request, "urlopen", return_value=Response("", 204)):
            self.assertIsNone(gcs.request("DELETE", "/test", "token", expected=(204,)))
        with (
            mock.patch.object(gcs.urllib.request, "urlopen", return_value=Response("{}", 201)),
            self.assertRaisesRegex(gcs.CleanupError, "unexpected HTTP status"),
        ):
            gcs.request("GET", "/test", "token")

        error = urllib.error.HTTPError("url", 403, "forbidden", {}, io.BytesIO(b"denied"))
        with (
            mock.patch.object(gcs.urllib.request, "urlopen", side_effect=error),
            self.assertRaisesRegex(gcs.CleanupError, "HTTP 403") as raised,
        ):
            gcs.request("GET", "/test", "token")
        self.assertNotIn("denied", str(raised.exception))

        retry = urllib.error.HTTPError(
            "url", 429, "rate limited", {"Retry-After": "0"}, io.BytesIO(b"")
        )
        with (
            mock.patch.object(gcs.urllib.request, "urlopen", side_effect=[retry, Response("{}")]),
            mock.patch.object(gcs.time, "sleep") as sleep,
        ):
            self.assertEqual(gcs.request("GET", "/test", "token"), {})
        sleep.assert_called_once_with(0)

        retry = urllib.error.HTTPError("url", 500, "failed", {}, io.BytesIO(b""))
        with (
            mock.patch.object(gcs.urllib.request, "urlopen", side_effect=retry),
            mock.patch.object(gcs.time, "sleep"),
            self.assertRaisesRegex(gcs.CleanupError, "retry budget"),
        ):
            gcs.request("GET", "/test", "token")

    def test_bucket_helpers_and_main(self) -> None:
        with mock.patch.object(
            gcs,
            "request",
            side_effect=[
                {"items": [{"name": "one"}], "nextPageToken": "next"},
                {"items": [{"name": "two"}]},
            ],
        ):
            self.assertEqual([item["name"] for item in gcs.list_buckets("project", "token")], ["one", "two"])

        with mock.patch.object(
            gcs,
            "request",
            side_effect=[
                {"items": [{"name": "one", "generation": 1}], "nextPageToken": "next"},
                {"items": [{"name": "two", "generation": 2}]},
            ],
        ):
            self.assertEqual(len(gcs.list_objects("bucket/name", "token")), 2)

        with (
            mock.patch.object(gcs, "list_objects", return_value=[{"name": "a/b", "generation": 2}]),
            mock.patch.object(gcs, "request") as request,
        ):
            gcs.delete_bucket("bucket/name", "token")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(
            gcs.bucket_is_managed(
                {"labels": {"managed_by": "ansible", "client": "fixture"}},
                {"managed_by": "ansible"},
            )
        )
        self.assertFalse(gcs.bucket_is_managed({}, {"managed_by": "ansible"}))

        with tempfile.TemporaryDirectory() as temp_dir:
            inventory = pathlib.Path(temp_dir) / "inventory.json"
            inventory.write_text(
                json.dumps(
                    {
                        "_meta": {
                            "hostvars": {
                                "localhost": {},
                                "web.example.invalid": {},
                                "bastion.example.invalid": {},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                gcs.expected_buckets_from_inventory(str(inventory)),
                {"web-restic", "bastion-restic"},
            )

            expected = pathlib.Path(temp_dir) / "expected.json"
            expected.write_text(json.dumps(["expected-restic"]), encoding="utf-8")
            args = [
                "cleanup",
                "--credentials-file",
                "credentials.json",
                "--project-id",
                "project",
                "--client",
                "fixture",
                "--environment",
                "test",
                "--expected-buckets-file",
                str(expected),
            ]
            buckets = [
                {
                    "name": "stale-restic",
                    "labels": {
                        "managed_by": "ansible",
                        "client": "fixture",
                        "env": "test",
                        "purpose": "restic",
                    },
                },
                {"name": "ignored", "labels": {}},
            ]
            with (
                mock.patch("sys.argv", args),
                mock.patch.object(gcs, "credentials_token", return_value="token"),
                mock.patch.object(gcs, "list_buckets", return_value=buckets),
                mock.patch.object(gcs, "delete_bucket") as delete,
                mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            ):
                self.assertEqual(gcs.main(), 0)
            self.assertIn(buckets[0]["name"], stdout.getvalue())
            delete.assert_not_called()

            args.extend(["--execute"])
            with (
                mock.patch("sys.argv", args),
                mock.patch.object(gcs, "credentials_token", return_value="token"),
                mock.patch.object(gcs, "list_buckets", return_value=buckets),
                mock.patch.object(gcs, "delete_bucket") as delete,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(gcs.main(), 0)
            delete.assert_called_once_with(buckets[0]["name"], "token")

            inventory_args = [
                "cleanup",
                "--credentials-file",
                "credentials.json",
                "--project-id",
                "project",
                "--client",
                "fixture",
                "--environment",
                "test",
                "--inventory-file",
                str(inventory),
            ]
            with (
                mock.patch("sys.argv", inventory_args),
                mock.patch.object(gcs, "credentials_token", return_value="token"),
                mock.patch.object(gcs, "list_buckets", return_value=[]),
                mock.patch.object(gcs, "delete_bucket") as delete,
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(gcs.main(), 0)
            delete.assert_not_called()


class RebootNotifyTests(unittest.TestCase):
    def test_json_boot_and_state_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            valid = root / "valid.json"
            valid.write_text('{"hostname":"host"}', encoding="utf-8")
            self.assertEqual(reboot.load_json(valid, "facts"), {"hostname": "host"})
            with self.assertRaises(SystemExit):
                reboot.load_json(root / "missing", "facts")
            invalid = root / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            with self.assertRaises(SystemExit):
                reboot.load_json(invalid, "facts")

            boot = root / "boot-id"
            state = root / "state"
            last = state / "last-boot-id"
            with (
                mock.patch.object(reboot, "BOOT_ID_PATH", boot),
                mock.patch.object(reboot, "STATE_DIR", state),
                mock.patch.object(reboot, "LAST_BOOT_ID_PATH", last),
            ):
                with self.assertRaises(SystemExit):
                    reboot.current_boot_id()
                boot.write_text("boot-one\n", encoding="utf-8")
                self.assertEqual(reboot.current_boot_id(), "boot-one")
                self.assertFalse(reboot.already_notified("boot-one"))
                reboot.record_boot_id("boot-one")
                self.assertTrue(reboot.already_notified("boot-one"))
                self.assertEqual(last.stat().st_mode & 0o777, 0o600)

                with mock.patch.object(
                    reboot.os, "replace", side_effect=OSError("expected")
                ):
                    with self.assertRaises(OSError):
                        reboot.record_boot_id("boot-two")
                self.assertEqual(list(state.glob(".last-boot-id.*")), [])

    def test_payload_notification_and_main(self) -> None:
        facts = {"hostname": "worker.example.invalid", "environment": "test"}
        payload = reboot.build_payload(facts, "Invalid/Timezone")
        self.assertIn(facts["hostname"], payload["embeds"][0]["description"])
        with self.assertRaises(SystemExit):
            reboot.send_discord_notification({}, payload)

        with mock.patch.object(reboot.urllib.request, "urlopen", return_value=Response("", 204)):
            reboot.send_discord_notification(
                {"webhook_id": "id", "webhook_token": "token"}, payload
            )
        with (
            mock.patch.object(
                reboot.urllib.request, "urlopen", return_value=Response("", 201)
            ),
            self.assertRaises(SystemExit),
        ):
            reboot.send_discord_notification(
                {"webhook_id": "id", "webhook_token": "token"}, payload
            )
        with (
            mock.patch.object(
                reboot.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("expected"),
            ),
            self.assertRaises(SystemExit),
        ):
            reboot.send_discord_notification(
                {"webhook_id": "id", "webhook_token": "token"}, payload
            )

        with (
            mock.patch.object(reboot, "current_boot_id", return_value="boot"),
            mock.patch.object(reboot, "already_notified", return_value=True),
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch.object(reboot, "send_discord_notification") as send,
        ):
            self.assertEqual(reboot.main(), 0)
        send.assert_not_called()

        with (
            mock.patch.object(reboot, "current_boot_id", return_value="boot"),
            mock.patch.object(reboot, "already_notified", return_value=False),
            mock.patch.object(
                reboot,
                "load_json",
                side_effect=[
                    {"hostname": "host", "environment": "test"},
                    {"webhook_id": "id", "webhook_token": "token"},
                ],
            ),
            mock.patch.object(reboot, "send_discord_notification") as send,
            mock.patch.object(reboot, "record_boot_id") as record,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(reboot.main(), 0)
        send.assert_called_once()
        record.assert_called_once_with("boot")


class RefreshMotdTests(unittest.TestCase):
    def test_render_and_reboot_state(self) -> None:
        self.assertEqual(motd.fit_text("short"), "short")
        self.assertTrue(motd.fit_text("x" * 100).endswith("..."))
        self.assertEqual(len(motd.motd_line("test")), motd.WIDTH)
        self.assertIn(motd.GREEN, motd.motd_line("test", motd.GREEN))
        facts = {"hostname": "worker.example.invalid", "environment": "test"}
        needs_reboot = motd.render_motd(facts, True)
        normal = motd.render_motd(facts, False)
        self.assertIn(facts["hostname"], needs_reboot)
        self.assertIn(facts["hostname"], normal)
        self.assertNotEqual(needs_reboot, normal)

        with mock.patch.object(motd.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit):
                motd.reboot_required()
        for returncode, expected in ((0, False), (1, True)):
            with (
                mock.patch.object(motd.shutil, "which", return_value="/bin/needs-restarting"),
                mock.patch.object(
                    motd.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], returncode),
                ),
            ):
                self.assertEqual(motd.reboot_required(), expected)
        with (
            mock.patch.object(motd.shutil, "which", return_value="command"),
            mock.patch.object(
                motd.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 2),
            ),
            self.assertRaises(SystemExit),
        ):
            motd.reboot_required()

    def test_facts_atomic_write_and_main(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            facts = root / "facts.json"
            target = root / "motd"
            with (
                mock.patch.object(motd, "FACT_PATH", facts),
                mock.patch.object(motd, "MOTD_PATH", target),
            ):
                with self.assertRaises(SystemExit):
                    motd.load_facts()
                facts.write_text("{", encoding="utf-8")
                with self.assertRaises(SystemExit):
                    motd.load_facts()
                facts.write_text('{"hostname":"host"}', encoding="utf-8")
                self.assertEqual(motd.load_facts(), {"hostname": "host"})
                self.assertTrue(motd.write_if_changed("content"))
                self.assertFalse(motd.write_if_changed("content"))
                self.assertEqual(target.stat().st_mode & 0o777, 0o644)
                with mock.patch.object(
                    motd.os, "replace", side_effect=OSError("expected")
                ):
                    with self.assertRaises(OSError):
                        motd.write_if_changed("replacement")
                self.assertEqual(list(root.glob(".motd.*")), [])

        with (
            mock.patch.object(motd, "load_facts", return_value={}),
            mock.patch.object(motd, "reboot_required", return_value=False),
            mock.patch.object(motd, "write_if_changed", side_effect=[True, False]) as write,
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(motd.main(), 0)
            self.assertEqual(motd.main(), 0)
        self.assertEqual(write.call_count, 2)
