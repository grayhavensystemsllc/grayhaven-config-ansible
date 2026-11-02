from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from tests.helpers import load_program


alerts = load_program(
    "validate_generated_grafana_alerts", "scripts/validate-generated-grafana-alerts"
)
alloy = load_program(
    "validate_rendered_alloy_config", "scripts/validate-rendered-alloy-config"
)
timetracker = load_program(
    "validate_rendered_timetracker_config",
    "scripts/validate-rendered-timetracker-config",
)


def fake_alloy_content(site: dict[str, object]) -> str:
    site_id = alloy.regex_replace(site["root_name"], r"[^A-Za-z0-9_]", "_")
    statuses = "".join(f"        - {status}\n" for status in range(100, 600))
    return (
        f'{site_id}_health:\nHost: "{site["domain"]}"\n'
        f'address = "{site["health_url"]}"\nmodule  = "{site_id}_health"\n'
        f'check       = "{site["health_check"]}"\n'
        f"  https_any_status_skip_tls_verify:\n{statuses}"
        f"  https_any_status_trusted:\n{statuses}`\n"
        '  target {\n    address = "https://business.example.invalid/"\n'
        '    module  = "https_any_status_skip_tls_verify"\n'
        '      check = "https_200",\n      domain      = "business.example.invalid",\n  }\n'
        '  target {\n    address = "https://other.example.invalid/"\n'
        '    module  = "https_any_status_trusted"\n'
        '      check = "ssl_certificate_trusted",\n      domain = "other.example.invalid",\n  }\n'
        '  target {\n    address = "https://spare.example.invalid/"\n'
        '    module  = "https_any_status_skip_tls_verify"\n'
        '      check = "ssl_certificate",\n      domain = "spare.example.invalid",\n  }\n'
    )


def fake_application_config(hostname: str) -> dict[str, str]:
    context = timetracker.fixture_context(hostname)
    image = context["timetracker_image_repository"]
    digest = context["timetracker"]["image_digest"]
    host_port = context["timetracker_host_port"]
    container_port = context["timetracker_container_port"]
    quadlet = "\n".join((
        f"Environment=WEBAUTHN_RP_ID={hostname}",
        f"Environment=WEBAUTHN_ORIGIN=https://{hostname}",
        f"Image={image}@{digest}",
        f"PublishPort=127.0.0.1:{host_port}:{container_port}",
        "Environment=GUNICORN_CMD_ARGS=--no-control-socket",
        f"headers={{'Host': '{hostname}'}}",
        "NoNewPrivileges=true", "PidsLimit=128", "ReadOnly=true", "Notify=healthy",
    ))
    indexing = 'add_header X-Robots-Tag "noindex, nofollow, noarchive" always;'
    nginx = "\n".join((
        f"server_name {hostname};", indexing, indexing,
        f"proxy_pass http://127.0.0.1:{host_port};",
        "proxy_set_header X-Forwarded-Proto https;",
        "location = /health", "/shared/reports/[redacted]",
    ))
    return {"grayhaven-timetracker.container": quadlet, "grayhaven-timetracker.conf": nginx}


class ValidatorTests(unittest.TestCase):
    def test_generated_rules_use_fictional_inputs_and_validate_shapes(self) -> None:
        namespace = alerts.runpy.run_path(str(alerts.ALERT_SYNC))
        config = alerts.fixture_config()
        rules = namespace["desired_rules"](config, "fixture-folder", "fixture-source")
        alerts.validate_generated_rules(namespace, rules)
        self.assertEqual(alerts.main([]), 0)

        identity = "host:fixture.example.invalid:example"
        uid = config["uid_registry"][identity]
        self.assertEqual(config["uid_registry"][identity], uid)
        self.assertNotEqual(config["uid_registry"]["host:other.example.invalid:example"], uid)

        first_uid = next(iter(rules))
        for change in ("empty", "uid", "duration", "query"):
            candidate = json.loads(json.dumps(rules))
            if change == "empty":
                candidate = {}
            elif change == "uid":
                candidate[first_uid]["uid"] = "invalid"
            elif change == "duration":
                candidate[first_uid]["for"] = "1m"
            else:
                candidate[first_uid]["data"][0]["model"]["expr"] = ""
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                alerts.validate_generated_rules(namespace, candidate)

    def test_registry_validation_uses_fictional_exports(self) -> None:
        namespace = alerts.runpy.run_path(str(alerts.ALERT_SYNC))
        registry = {
            "host:worker.example.invalid:example": "gh-" + "a" * 24,
            "service:Mock:check": "gh-" + "b" * 24,
        }
        live = [
            {
                "uid": uid,
                "labels": {"configured_by": "ansible", scope: resource, "check": check},
            }
            for identity, uid in registry.items()
            for scope, resource, check in [identity.split(":", 2)]
        ]
        alerts.validate_live_registry(namespace, registry, live)
        for scope in ("host", "domain", "service"):
            labels = {scope: "example", "check": "sample"}
            self.assertEqual(alerts.imported_identity({"labels": labels}), f"{scope}:example:sample")
        self.assertEqual(alerts.imported_identity({"labels": {"check": "sample"}}), "check:sample")

        with self.assertRaises(RuntimeError):
            alerts.validate_live_registry(namespace, registry, live[:-1])
        changed = json.loads(json.dumps(live))
        changed[0]["uid"] = "different"
        with self.assertRaises(RuntimeError):
            alerts.validate_live_registry(namespace, registry, changed)

        invalid_registry = {**registry, next(iter(registry)): "invalid"}
        invalid_live = json.loads(json.dumps(live))
        invalid_live[0]["uid"] = "invalid"
        with self.assertRaises(RuntimeError):
            alerts.validate_live_registry(namespace, invalid_registry, invalid_live)

        duplicate_registry = dict(registry)
        duplicate_registry[list(registry)[1]] = duplicate_registry[list(registry)[0]]
        duplicate_live = json.loads(json.dumps(live))
        duplicate_live[1]["uid"] = duplicate_live[0]["uid"]
        with self.assertRaises(RuntimeError):
            alerts.validate_live_registry(namespace, duplicate_registry, duplicate_live)

        desired = json.loads(json.dumps(live))
        alerts.validate_live_registry(namespace, registry, live, desired)
        desired[0]["title"] = "different"
        with self.assertRaises(RuntimeError):
            alerts.validate_live_registry(namespace, registry, live, desired)

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            export = root / "export.json"
            registry_file = root / "registry.json"
            export.write_text(json.dumps(live), encoding="utf-8")
            registry_file.write_text(json.dumps(registry), encoding="utf-8")
            with mock.patch.object(alerts, "UID_REGISTRY", registry_file):
                self.assertEqual(alerts.main(["--live-export", str(export)]), 0)

    def test_alloy_render_validation_and_main(self) -> None:
        self.assertEqual(alloy.regex_replace("abc", "b", "x"), "axc")
        environment = alloy.build_environment()
        self.assertIn("regex_replace", environment.filters)
        site = next(
            item for item in alloy.fixture_context()["observability_web_sites"]
            if item["kind"] == "application"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            rendered = pathlib.Path(temp_dir) / "config.alloy"
            template = mock.Mock()
            template.render.return_value = "fictional rendered output"
            with mock.patch.object(alloy, "build_environment") as build:
                build.return_value.get_template.return_value = template
                alloy.render_config(rendered, "mcp")
            self.assertEqual(rendered.read_text(encoding="utf-8"), template.render.return_value)
            template.render.assert_called_once_with(alloy.fixture_context("mcp"))
            rendered.write_text(fake_alloy_content(site), encoding="utf-8")
            alloy.validate_application_contract(rendered, site)
            alloy.validate_https_probe_contract(rendered)
            content = rendered.read_text(encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "contract is missing"):
                alloy.validate_application_contract(
                    rendered, {**site, "domain": "different.example.invalid"}
                )

            broken = content.replace("https_any_status_trusted:", "missing:", 1)
            rendered.write_text(broken, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "HTTPS probe module is missing"):
                alloy.validate_https_probe_contract(rendered)

        with (
            mock.patch("sys.argv", ["validate-rendered-alloy-config"]),
            mock.patch.object(alloy, "render_config"),
            mock.patch.object(alloy, "validate_application_contract"),
            mock.patch.object(alloy, "validate_https_probe_contract"),
            mock.patch.object(
                alloy.subprocess, "run", return_value=mock.Mock(returncode=0)
            ),
        ):
            self.assertEqual(alloy.main(), 0)
        with (
            mock.patch("sys.argv", ["validate-rendered-alloy-config"]),
            mock.patch.object(alloy, "render_config"),
            mock.patch.object(alloy, "validate_application_contract"),
            mock.patch.object(alloy, "validate_https_probe_contract"),
            mock.patch.object(alloy.subprocess, "run", side_effect=FileNotFoundError),
        ):
            self.assertEqual(alloy.main(), 127)

    def test_alloy_probe_validator_rejects_probe_contract_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rendered = pathlib.Path(temp_dir) / "config.alloy"
            site = next(
                item for item in alloy.fixture_context()["observability_web_sites"]
                if item["kind"] == "application"
            )
            content = fake_alloy_content(site)

            cases = (
                (
                    content.replace("        - 100\n", "        - 99\n", 1),
                    "does not accept every HTTP status",
                ),
                (
                    content.replace(
                        'module  = "https_any_status_skip_tls_verify"',
                        'module  = "other"',
                    ).replace(
                        'module  = "https_any_status_trusted"',
                        'module  = "other"',
                    ),
                    "do not use shared TLS probes",
                ),
                (content + '\nmodule  = "http_2xx"\n', "status-limited"),
                (
                    content.replace(
                        '      domain      = "business.example.invalid"',
                        '      domain      = "other.example.invalid"',
                        1,
                    ),
                    "requested hostname",
                ),
                (
                    content.replace(
                        '    module  = "https_any_status_skip_tls_verify"',
                        '    module_missing  = "https_any_status_skip_tls_verify"',
                        1,
                    ),
                    "requested hostname",
                ),
                (
                    content.replace(
                        '    module  = "https_any_status_skip_tls_verify"',
                        '    module  = "https_any_status_trusted"',
                        1,
                    ),
                    "wrong TLS probe module",
                ),
            )
            for broken, message in cases:
                rendered.write_text(broken, encoding="utf-8")
                with self.subTest(message=message):
                    with self.assertRaisesRegex(RuntimeError, message):
                        alloy.validate_https_probe_contract(rendered)

    def test_webauthn_values_follow_supplied_hostname(self) -> None:
        hostname = "login.example.invalid"
        values = timetracker.managed_webauthn_values(hostname)
        self.assertEqual(values, {"rp_id": hostname, "origin": "https://" + hostname})
        for invalid in ("https://example.invalid", "example.invalid:1234", "example.invalid/path", ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                timetracker.managed_webauthn_values(invalid)

    def test_template_validator_handles_fictional_origins(self) -> None:
        hostname = "login.example.invalid"
        context = timetracker.fixture_context(hostname)
        rendered = fake_application_config(hostname)
        fake_tasks = mock.Mock()
        fake_tasks.read_text.side_effect = AssertionError("unexpected task read")
        with (
            mock.patch.object(timetracker, "SERVICE_TASKS", fake_tasks),
            mock.patch.object(timetracker, "NGINX_TASKS", fake_tasks),
        ):
            timetracker.validate(rendered, check_tasks=False)
            quadlet_name = next(name for name in rendered if name.endswith(".container"))
            original = rendered[quadlet_name]
            cases = (
                original.replace(f"WEBAUTHN_RP_ID={hostname}", "WEBAUTHN_RP_ID=https://example.invalid"),
                original.replace(f"WEBAUTHN_ORIGIN=https://{hostname}", "WEBAUTHN_ORIGIN=http://other.invalid"),
                original.replace(f"WEBAUTHN_RP_ID={hostname}", "MISSING_RP_ID"),
                original + (
                    f'\nPublishPort=0.0.0.0:{context["timetracker_host_port"]}:'
                    f'{context["timetracker_container_port"]}\n'
                ),
            )
            for candidate in cases:
                with self.subTest(candidate=candidate), self.assertRaises(RuntimeError):
                    timetracker.validate({**rendered, quadlet_name: candidate}, check_tasks=False)
        fake_tasks.read_text.assert_not_called()

    def test_template_renderer_uses_supplied_fictional_hostname(self) -> None:
        hostname = "login.example.invalid"
        template = mock.Mock()
        template.render.side_effect = lambda context: context["timetracker"]["hostname"]
        environment = mock.Mock()
        environment.get_template.return_value = template
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(timetracker.jinja2, "Environment", return_value=environment),
        ):
            rendered = timetracker.render_templates(pathlib.Path(directory), hostname)
            self.assertTrue(rendered)
            self.assertTrue(all(value == hostname for value in rendered.values()))
            for name in rendered:
                self.assertEqual((pathlib.Path(directory) / name).read_text(), hostname)
