"""Behavior tests for stale backup bucket host identification."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from tests.helpers import load_program


stale_buckets = load_program(
    "grayhaven_gcs_stale_buckets",
    "roles/observability/files/grayhaven-gcs-restic-stale-buckets",
)


class BucketHostnameTests(unittest.TestCase):
    def test_bucket_hosts_use_the_supplied_inventory_domain(self):
        for domain in ("example.test", "preview.example.test", "another.invalid"):
            for suffix in ("", "."):
                with self.subTest(domain=domain, suffix=suffix):
                    self.assertEqual(
                        stale_buckets.bucket_host_fqdn(
                            "retired-node-restic", f"controller.{domain}{suffix}"
                        ),
                        f"retired-node.{domain}",
                    )

    def test_invalid_inventory_names_are_rejected(self):
        for value in (
            None,
            "",
            "localhost",
            "192.0.2.1",
            ".example.test",
            "node..test",
            "node.-test",
            "node." + "a" * 64,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                stale_buckets.bucket_host_fqdn("retired-node-restic", value)


class StaleBucketTests(unittest.TestCase):
    def test_http_helpers_encode_requests_and_decode_responses(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"items": []}'
        with mock.patch.object(
            stale_buckets.urllib.request, "urlopen", return_value=response
        ) as request:
            self.assertEqual(
                stale_buckets.request_json(
                    "https://example.test", {"X-Fixture": "sample"}
                ),
                {"items": []},
            )
            self.assertEqual(
                stale_buckets.post_form_json("https://example.test", {"sample": "a b"}),
                {"items": []},
            )
        self.assertEqual(request.call_args.args[0].data, b"sample=a+b")
        with mock.patch.object(
            stale_buckets, "request_json", return_value={}
        ) as request:
            self.assertEqual(stale_buckets.storage_request("/b", "invented"), {})
        self.assertTrue(request.call_args.args[0].endswith("/b"))

    def test_google_assertion_signs_only_requested_scope_and_rejects_bad_credentials(
        self,
    ):
        for private, result in (
            (
                rsa.generate_private_key(public_exponent=65537, key_size=2048),
                {"access_token": "invented"},
            ),
            (ec.generate_private_key(ec.SECP256R1()), {}),
        ):
            pem = private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
            credentials = {"client_email": "service@example.test", "private_key": pem}
            with (
                mock.patch(
                    "builtins.open", return_value=io.StringIO(json.dumps(credentials))
                ),
                mock.patch.object(
                    stale_buckets, "post_form_json", return_value=result
                ) as post,
            ):
                if isinstance(private, rsa.RSAPrivateKey):
                    self.assertEqual(
                        stale_buckets.google_token("fixture.json", "fixture-scope"),
                        result["access_token"],
                    )
                    assertion = post.call_args.args[1]["assertion"]
                    payload = assertion.split(".")[1]
                    claims = json.loads(
                        base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
                    )
                    self.assertEqual(claims["scope"], "fixture-scope")
                    self.assertEqual(claims["iss"], credentials["client_email"])
                    self.assertGreater(claims["exp"], claims["iat"])
                else:
                    with self.assertRaises(RuntimeError):
                        stale_buckets.google_token("fixture.json", "fixture-scope")
                    post.assert_not_called()
        pem = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode()
        )
        for invalid in (None, "", 3):
            with (
                mock.patch(
                    "builtins.open",
                    return_value=io.StringIO(
                        json.dumps(
                            {
                                "client_email": "service@example.test",
                                "private_key": pem,
                                "token_uri": "https://example.test/token",
                            }
                        )
                    ),
                ),
                mock.patch.object(
                    stale_buckets,
                    "post_form_json",
                    return_value={"access_token": invalid},
                ),
            ):
                with self.assertRaises(RuntimeError):
                    stale_buckets.google_token("fixture.json", "fixture-scope")

    def test_pagination_and_managed_selection(self):
        with mock.patch.object(
            stale_buckets,
            "storage_request",
            side_effect=[
                {"items": [{"name": "first"}], "nextPageToken": "second page"},
                {"items": [{"name": "second"}]},
            ],
        ) as request:
            self.assertEqual(
                stale_buckets.list_buckets("fixture project", "invented"),
                [{"name": "first"}, {"name": "second"}],
            )
        self.assertIn("pageToken=second+page", request.call_args.args[0])
        self.assertTrue(
            stale_buckets.bucket_is_managed(
                {"labels": {"team": "example", "extra": "value"}}, {"team": "example"}
            )
        )
        self.assertFalse(stale_buckets.bucket_is_managed({}, {"team": "example"}))

    def test_metrics_include_only_stale_managed_buckets(self):
        config = {
            "host": {
                "client": "fixture",
                "environment": "test",
                "fqdn": "current.example.test",
            },
            "hosts": [{"short_hostname": "current"}],
            "gcs": {
                "project_id": "fixture-project",
                "credentials_file": "fixture.json",
            },
        }
        labels = {
            "managed_by": "ansible",
            "client": "fixture",
            "env": "test",
            "purpose": "restic",
        }
        for buckets, expected in (
            ([], 0),
            (
                [
                    {"name": "current-restic", "labels": labels},
                    {"name": "old-restic", "labels": labels},
                    {"name": "foreign-restic", "labels": {}},
                ],
                1,
            ),
        ):
            with (
                mock.patch.object(
                    stale_buckets, "google_token", return_value="invented"
                ),
                mock.patch.object(stale_buckets, "list_buckets", return_value=buckets),
            ):
                metrics = stale_buckets.render_metrics(config)
            count = next(
                row
                for row in metrics
                if row.startswith("grayhaven_gcs_restic_stale_bucket_count{")
            )
            self.assertTrue(count.endswith(" " + str(expected)))
            info = [
                row
                for row in metrics
                if row.startswith("grayhaven_gcs_restic_stale_bucket_info{")
            ]
            self.assertEqual(len(info), expected)
            if info:
                self.assertIn('host="old.example.test"', info[0])
        self.assertEqual(stale_buckets.label_value('a"b\\c\n'), 'a\\"b\\\\c\\n')

    def test_main_uses_atomic_output_and_removes_disabled_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, output = root / "config.json", root / "metrics/result.prom"
            with (
                mock.patch.object(stale_buckets, "CONFIG_PATH", config),
                mock.patch.object(stale_buckets, "TEXTFILE_DIR", output.parent),
                mock.patch.object(stale_buckets, "OUTPUT_PATH", output),
                mock.patch.object(
                    stale_buckets, "render_metrics", return_value=["fixture_metric 1"]
                ),
            ):
                config.write_text(
                    json.dumps(
                        {"host": {"control_node": True}, "gcs": {"enabled": True}}
                    )
                )
                self.assertEqual(stale_buckets.main(), 0)
                self.assertEqual(output.read_text(), "fixture_metric 1\n")
                self.assertEqual(output.stat().st_mode & 0o777, 0o644)
                self.assertEqual(list(output.parent.iterdir()), [output])
                for host, enabled in ((False, True), (True, False)):
                    config.write_text(
                        json.dumps(
                            {
                                "host": {"control_node": host},
                                "gcs": {"enabled": enabled},
                            }
                        )
                    )
                    self.assertEqual(stale_buckets.main(), 0)
                    self.assertFalse(output.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
