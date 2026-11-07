"""Test value protection using generated, entirely fictional input values."""

import unittest
import uuid

from tests.helpers import load_program


redaction = load_program("output_redaction", "files/grayhaven-redact-output")


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.value = uuid.uuid4().hex

    def test_structured_sensitive_fields_and_environment_maps_are_concealed(self):
        for field in (
            "password_hash",
            "credentials_json",
            "github_pat",
            "private_key",
            "Authorization",
            "apiToken",
        ):
            data = {field: self.value, "public": "supplied-public-value"}
            result = redaction.Redactor().tree(data)
            self.assertTrue(self.value not in str(result))
            self.assertEqual(result["public"], data["public"])
            self.assertEqual(data[field], self.value)
        result = redaction.Redactor().tree(
            {"ansible_env": {"SAMPLE_OPTION": self.value}, "environment": "sample"}
        )
        self.assertTrue(self.value not in str(result))
        self.assertEqual(result["environment"], "sample")

    def test_known_values_are_removed_from_unlabelled_diagnostics(self):
        redactor = redaction.Redactor([self.value, ""])
        self.assertTrue(self.value not in redactor.text("failure: " + self.value))
        self.assertEqual(redactor.text("ordinary detail"), "ordinary detail")
        self.assertEqual(redactor.text(""), "")

    def test_text_assignment_header_flag_and_url_forms_are_concealed(self):
        values = [
            f'"password_hash": "{self.value}"',
            f"'credentials_json': '{self.value}'",
            f"github_pat={self.value}",
            f"Authorization: Bearer {self.value}",
            f"Authorization: Basic {self.value}",
            f"--password {self.value}",
            f'--api-key="{self.value}"',
            f"SAMPLE_OPTION='{self.value}'",
            f"https://sample-user:{self.value}@example.test/path",
            f"https://discord.com/api/webhooks/123/{self.value}",
        ]
        for data in values:
            with self.subTest():
                self.assertTrue(self.value not in redaction.Redactor().text(data))

    def test_private_key_blocks_are_omitted_until_the_end_marker(self):
        for kind in ("OPENSSH PRIVATE KEY", "PGP PRIVATE KEY BLOCK"):
            text = f"before\n-----BEGIN {kind}-----\n{self.value}\n-----END {kind}-----\nafter\n"
            result = redaction.Redactor().text(text)
            self.assertTrue(self.value not in result)
            self.assertTrue(result.startswith("before\n"))
            self.assertTrue(result.endswith("after\n"))
        redactor = redaction.Redactor()
        redactor.line("-----BEGIN PRIVATE KEY-----\n")
        self.assertEqual(redactor.line(self.value), "")

    def test_argument_lists_mask_the_value_after_a_sensitive_flag(self):
        arguments = ["program", "--password", self.value, "--mode", "sample"]
        result = redaction.Redactor().tree(arguments)
        self.assertTrue(self.value not in str(result))
        self.assertEqual(result[-2:], arguments[-2:])

    def test_collection_handles_aliases_cycles_and_non_string_values(self):
        shared = {"value": self.value}
        values = {
            "public": shared,
            "credentials": shared,
            "number": 42,
            "enabled": True,
            "absent": None,
        }
        values["cycle"] = values
        collected = redaction.collect_secret_values(values)
        self.assertEqual(collected, {self.value})
        self.assertEqual(redaction.collect_secret_values([1, False, None]), set())
        result = redaction.Redactor().tree([1, False, None, object()])
        self.assertEqual(result[:3], [1, False, None])
        self.assertIsInstance(result[-1], str)

    def test_display_proxy_preserves_options_and_filters_arguments(self):
        class Display:
            width = 100

            def display(self, message, **options):
                self.message = message
                self.options = options

        display = Display()
        proxy = redaction.SafeDisplay(display, redaction.Redactor([self.value]))
        proxy.display(self.value, color="sample-color", stderr=True)
        self.assertTrue(self.value not in display.message)
        self.assertEqual(display.options, {"color": "sample-color", "stderr": True})
        self.assertEqual(proxy.width, display.width)
