"""Exercise caller-provided definitions rather than deployed rule mappings."""

import base64
import copy
import json
import unittest

import yaml

from tests.alert_fixtures import complete_config
from tests.helpers import load_program


alerts = load_program(
    "generic_alert_templates", "roles/observability/files/grayhaven-grafana-alert-sync"
)


class AlertTemplateTests(unittest.TestCase):
    def test_supplied_checks_propagate_to_generated_rules(self):
        config = complete_config()
        for check in config["rule_definitions"]["parameters"]["checks"]:
            rules = alerts.desired_rules(config, "supplied-folder", "supplied-source")
            rule = rules[config["uid_registry"][check["identity"]]]
            self.assertEqual(rule["title"], check["title"])
            self.assertEqual(rule["folderUID"], "supplied-folder")
            self.assertEqual(rule["data"][0]["model"]["expr"], check["expression"])
            evaluator = rule["data"][1]["model"]["conditions"][0]["evaluator"]
            self.assertEqual(evaluator["type"], check["comparison"])
            self.assertEqual(evaluator["params"], [check["threshold"]])

    def test_invalid_definitions_and_unavailable_context_fail_safely(self):
        config = complete_config()
        cases = [
            None,
            [],
            {},
            {"templates": None},
            {"templates": [], "parameters": []},
            {"templates": [None]},
        ]
        for template in (
            "not json",
            "42",
            "{}",
            "[null]",
            "{{ missing }}",
            "{{ config.grafana.api_token }}",
            "{{ ().__class__ }}",
        ):
            cases.append({"templates": [{"template": template}]})
        for definition in cases:
            with self.subTest(), self.assertRaises(alerts.GrafanaError):
                alerts.render_rule_templates(config, "folder", "source", definition)

    def test_templates_can_return_one_rule_or_an_empty_list(self):
        config = complete_config()
        rule = alerts.render_rule_templates(
            config, "folder", "source", config["rule_definitions"]
        )[0]
        for template, count in ((json.dumps(rule), 1), ("[]", 0)):
            result = alerts.render_rule_templates(
                config, "folder", "source", {"templates": [{"template": template}]}
            )
            self.assertEqual(len(result), count)

    def test_encoded_definitions_decode_or_refuse_invalid_inputs(self):
        expected = complete_config()["rule_definitions"]
        encoded = base64.b64encode(yaml.safe_dump(expected).encode()).decode()
        self.assertEqual(alerts.decode_rule_definitions(encoded), expected)
        for value in (
            None,
            "not base64!",
            base64.b64encode(b"[broken").decode(),
            base64.b64encode(b"[]").decode(),
        ):
            with self.subTest(), self.assertRaises(alerts.GrafanaError):
                alerts.decode_rule_definitions(value)

    def test_matching_uses_supplied_fields_and_defaults(self):
        items = [{"kind": "one"}, {"kind": "two", "mode": "explicit"}]
        before = copy.deepcopy(items)
        self.assertEqual(
            alerts.first_matching(items, {"mode": "fallback"}, {"mode": "fallback"}),
            items[0],
        )
        self.assertEqual(
            alerts.first_matching(items, {"kind": "two", "mode": "explicit"}), items[1]
        )
        self.assertIsNone(alerts.first_matching(items, {"kind": "absent"}))
        self.assertEqual(items, before)
