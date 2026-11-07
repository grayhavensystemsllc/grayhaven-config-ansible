"""Isolated behavior and coverage tests for managed shell programs."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shlex
import stat
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import yaml

from tests import shell_coverage


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def executable(path: pathlib.Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class ShellProgramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shell_coverage.reset()

    @classmethod
    def tearDownClass(cls) -> None:
        shell_coverage.write_report()

    def run_bash(
        self, source: str, *, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace = pathlib.Path(temp_dir) / "trace.bash"
            trace.write_text(shell_coverage.TRACE_SCRIPT, encoding="utf-8")
            run_env = {"PATH": os.defpath, "HOME": temp_dir, "LANG": "C.UTF-8"}
            run_env.update(shell_coverage.coverage_environment(trace))
            if env:
                run_env.update(env)
            return subprocess.run(
                ["bash", "-c", source],
                cwd=REPO_ROOT,
                env=run_env,
                check=False,
                capture_output=True,
                text=True,
            )

    def test_coverage_trace_distinguishes_executed_and_skipped_commands(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            script = root / "gtmux"
            executable(
                script,
                "#!/usr/bin/env bash\nvalue=42\nif false; then\n"
                "  printf 'skipped'\nfi\nprintf '%s' \"$value\"\n",
            )
            with (
                mock.patch.object(shell_coverage, "REPO_ROOT", root),
                mock.patch.object(shell_coverage, "TRACE_PATH", root / "trace.lines"),
                mock.patch.object(shell_coverage, "XML_PATH", root / "coverage.xml"),
                mock.patch.object(shell_coverage, "PROGRAMS", (pathlib.Path("gtmux"),)),
            ):
                result = self.run_bash(f"bash {shlex.quote(str(script))}")
                self.assertEqual(result.returncode, 0, result.stderr)
                shell_coverage.write_report()
                report = ET.parse(shell_coverage.XML_PATH)
                hits = {
                    int(line.get("number")): int(line.get("hits"))
                    for line in report.iter("line")
                }
            self.assertEqual(hits[2], 1)
            self.assertEqual(hits[4], 0)
            self.assertEqual(hits[6], 1)

    def test_convergence_includes_optional_variables_only_when_present(
        self,
    ) -> None:
        for include_optional in (False, True):
            with (
                self.subTest(include_optional=include_optional),
                tempfile.TemporaryDirectory() as folder,
            ):
                root = pathlib.Path(folder)
                if include_optional:
                    (root / "mock-dependency.yml").write_text("fixture: true")
                result = self.run_bash(
                    """
                    export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
                    source files/grayhaven-ansible-runner
                    VAULT_CHECKOUT_DIR="$FIXTURE_ROOT"
                    CHECKOUT_DIR="$FIXTURE_CHECKOUT"
                    RUNNER_VARS="$FIXTURE_ROOT/vars"
                    RUNNER_PLAYBOOK_LOG="$FIXTURE_ROOT/playbook.log"
                    VAULT_PASSWORD_FILE="$FIXTURE_ROOT/password"
                    ANSIBLE_PLAYBOOK_BIN=fixture-ansible
                    prepare_inventory_known_hosts() { :; }
                    log() { :; }
                    write_ordered_runner_log() { cat; }
                    run_as_ansible() { printf '%s\\n' "$@"; }
                    run_convergence "$FIXTURE_ROOT/mock-dependency.yml"
                    """,
                    env={"FIXTURE_ROOT": folder, "FIXTURE_CHECKOUT": str(REPO_ROOT)},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    "@" + folder + "/mock-dependency.yml" in result.stdout,
                    include_optional,
                )


    def test_secret_environment_uses_grouped_configuration(self) -> None:
        for valid_selector in (True, False):
            with self.subTest(valid_selector=valid_selector), tempfile.TemporaryDirectory() as folder:
                root = pathlib.Path(folder)
                (root / "config.yml").write_text(json.dumps({
                    "certificate": {"environment": "staging", "email": "operator@example.invalid"},
                }))
                (root / "server_configuration.yml").write_text(json.dumps({
                    "network": {"vpc_cidr": "172.28.0.0/16"},
                }))
                (root / "ansible.yml").write_text(json.dumps({
                    "ansible": {"notifications": {"environment": "delivery-alpha"}},
                }))
                secret = {
                    "discord_webhooks": {
                        "delivery-alpha" if valid_selector else "delivery-beta":
                            "https://hooks.example.invalid/fictional/credential",
                    },
                    "digitalocean_inventory_api_token": "invented-inventory-credential",
                }
                provider = root / "provider"
                executable(provider, "#!/usr/bin/python3\nimport json\nprint(json.dumps("
                           + repr(secret) + "))\n")
                result = self.run_bash(
                    """
                    export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
                    source files/grayhaven-ansible-runner
                    VAULT_CHECKOUT_DIR="$FIXTURE_ROOT"
                    RUNNER_SECRET_ENV="$FIXTURE_ROOT/generated-env"
                    VAULT_PASSWORD_FILE="$FIXTURE_ROOT/opaque-password"
                    export ANSIBLE_VAULT_BIN="$FIXTURE_ROOT/provider"
                    render_secret_environment
                    python3 - <<'PY'
import json
import os
print(json.dumps({
    "token": os.environ["DO_API_TOKEN"] == "invented-inventory-credential",
    "webhook": os.environ["DISCORD_WEBHOOK_URL"] == "https://hooks.example.invalid/fictional/credential",
    "network": os.environ["GRAYHAVEN_NETWORK_VPC_CIDR"] == "172.28.0.0/16",
    "certificate": os.environ["GRAYHAVEN_CERTIFICATE_EMAIL"] == "operator@example.invalid",
}))
PY
                    """,
                    env={"FIXTURE_ROOT": folder},
                )
                self.assertEqual(result.returncode == 0, valid_selector)
                self.assertEqual(stat.S_IMODE((root / "generated-env").stat().st_mode), 0o600)
                if valid_selector:
                    self.assertTrue(all(json.loads(result.stdout).values()))

    def test_rendered_runtime_values_follow_supplied_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "policy").mkdir()
            infrastructure = {"identity": {"client_name": "fictional", "client_domain": "example.invalid"}}
            (root / "policy/infrastructure.yml").write_text(json.dumps(infrastructure))
            variables = {
                "REPO_REF": "feature/fixture",
                "GRAYHAVEN_ENVIRONMENT": "fixture",
                "GRAYHAVEN_TLS_MODE": "host",
                "DISCORD_WEBHOOK_ID": "invented-notice",
                "DISCORD_WEBHOOK_TOKEN": "invented-notice-credential",
            }
            result = self.run_bash(
                """
                export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
                source files/grayhaven-ansible-runner
                export VAULT_CHECKOUT_DIR="$FIXTURE_ROOT"
                RUNNER_VARS="$FIXTURE_ROOT/runtime-values"
                export REPO_REF="$FIXTURE_REF"
                render_runner_vars
                """,
                env={**variables, "FIXTURE_ROOT": folder, "FIXTURE_REF": variables["REPO_REF"]},
            )
            self.assertEqual(result.returncode, 0)
            # Parsing the generated private file stays inside the test process.
            output = yaml.safe_load((root / "runtime-values").read_text())
            self.assertEqual(output, {
                "infrastructure": infrastructure,
                "grayhaven_config_repo_ref": variables["REPO_REF"],
                "grayhaven_environment": variables["GRAYHAVEN_ENVIRONMENT"],
                "grayhaven_tls_mode": variables["GRAYHAVEN_TLS_MODE"],
                "automation_notifications_discord_webhook_id": variables["DISCORD_WEBHOOK_ID"],
                "automation_notifications_discord_webhook_token": variables["DISCORD_WEBHOOK_TOKEN"],
            })
            self.assertEqual(stat.S_IMODE((root / "runtime-values").stat().st_mode), 0o600)

    def test_runner_arguments_and_dispatch(self) -> None:
        for arguments, expected in (
            ("--converge", "converge|false|"),
            (
                "--cleanup-gcs-restic-buckets --execute",
                "cleanup-gcs-restic-buckets|true|",
            ),
            (
                "--rotate-vault-password --extra-vars-file /tmp/vars",
                "rotate-vault-password|false|/tmp/vars",
            ),
            ("--rotate-vault-deploy-key", "rotate-vault-deploy-key|false|"),
            ("--rotate-ansible-control-key", "rotate-ansible-control-key|false|"),
        ):
            result = self.run_bash(
                f"export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1; source files/grayhaven-ansible-runner; "
                f'parse_args {arguments}; printf \'%s|%s|%s\\n\' "$RUNNER_COMMAND" "$GCS_CLEANUP_EXECUTE" "${{RUNNER_EXTRA_VARS_FILES[*]}}"'
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), expected)
        for arguments, message in (
            ("--unknown", "Unknown runner argument"),
            ("--execute", "only valid"),
            ("--rotate-vault-password", "requires --extra-vars-file"),
            ("--extra-vars-file", "requires a path"),
        ):
            result = self.run_bash(
                f"export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1; source files/grayhaven-ansible-runner; parse_args {arguments}"
            )
            self.assertNotEqual(result.returncode, 0)

            self.assertIn(message, result.stdout + result.stderr)
        result = self.run_bash(
            """
            export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
            source files/grayhaven-ansible-runner
            prepare_runner_runtime() { printf 'prepare\n'; }
            prepare_rotate_vault_password_file() { printf 'password\n'; }
            run_convergence() { printf 'converge\n'; }
            run_gcs_restic_bucket_cleanup() { printf 'cleanup\n'; }
            run_rotate_vault_password() { printf 'rotate-password\n'; }
            run_rotate_vault_deploy_key() { printf 'rotate-deploy\n'; }
            run_rotate_ansible_control_key() { printf 'rotate-control\n'; }
            for RUNNER_COMMAND in converge cleanup-gcs-restic-buckets rotate-vault-password rotate-vault-deploy-key rotate-ansible-control-key; do run_once; done
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rotate-control", result.stdout)

    def test_new_inventory_host_keys_are_scanned_without_replacing_existing_trust(self):
        for trusted, scan_ok in ((True, True), (False, True), (False, False)):
            with (
                self.subTest(trusted=trusted, scan_ok=scan_ok),
                tempfile.TemporaryDirectory() as folder,
            ):
                root = pathlib.Path(folder)
                result = self.run_bash(
                    r"""
                    export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
                    source files/grayhaven-ansible-runner
                    INVENTORY_KNOWN_HOSTS="$FIXTURE_ROOT/bridge"
                    INVENTORY_GLOBAL_KNOWN_HOSTS="$FIXTURE_ROOT/trusted"
                    CHECKOUT_DIR="$FIXTURE_ROOT/config"
                    export TMPDIR="$FIXTURE_ROOT"
                    log() { :; }
                    running_as_ansible() { return 0; }
                    run_as_ansible() {
                      printf '%s' '{"_meta":{"hostvars":{"local.example.test":{"ansible_connection":"local","ansible_host":"127.0.0.1"},"new.example.test":{"ansible_host":"192.0.2.7"}}}}'
                    }
                    ssh-keygen() { [[ "$FIXTURE_TRUSTED" == true ]]; }
                    ssh-keyscan() {
                      printf '%s\n' "$*" > "$FIXTURE_ROOT/scanned"
                      [[ "$FIXTURE_SCAN_OK" == true ]] || return 1
                      printf '%s\n' '192.0.2.7 ssh-ed25519 AAAAfixture'
                    }
                    prepare_inventory_known_hosts
                    """,
                    env={
                        "FIXTURE_ROOT": folder,
                        "FIXTURE_TRUSTED": str(trusted).lower(),
                        "FIXTURE_SCAN_OK": str(scan_ok).lower(),
                    },
                )
                self.assertEqual(result.returncode == 0, trusted or scan_ok)
                self.assertEqual((root / "scanned").exists(), not trusted)
                if result.returncode == 0:
                    rows = (root / "bridge").read_text().splitlines()
                    self.assertEqual(len(rows), 0 if trusted else 2)
                    if rows:
                        self.assertEqual(
                            {row.split()[0] for row in rows},
                            {"192.0.2.7", "new.example.test"},
                        )

    def test_runner_failure_is_reported_once_by_its_own_invocation(self) -> None:
        for fault, status in (("direct", 17), ("subshell", 1), ("pipeline", 1)):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as folder:
                notices = pathlib.Path(folder) / "notices"
                for invocation in range(2):
                    result = self.run_bash(
                        """
                        export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
                        source files/grayhaven-ansible-runner
                        LOCK_FILE="$FIXTURE_ROOT/lock"
                        PENDING_FILE="$FIXTURE_ROOT/pending"
                        drop_to_ansible() { :; }
                        cleanup_runtime_files() { :; }
                        write_convergence_metric() { :; }
                        send_failure_notification() {
                          printf '%s\\n' "$1" >> "$FIXTURE_ROOT/notices"
                          return "$1"
                        }
                        run_once() {
                          case "$FAULT" in
                            direct) return 17 ;;
                            subshell) ( false ) ;;
                            pipeline) ( false ) | cat ;;
                          esac
                        }
                        main
                        """,
                        env={"FIXTURE_ROOT": folder, "FAULT": fault},
                    )
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(
                        notices.read_text().splitlines(),
                        [str(status)] * (invocation + 1),
                    )

    def test_runner_retries_failure_notification_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            curl_args = pathlib.Path(temp_dir) / "curl-args"
            result = self.run_bash(
                f"""
                export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
                source files/grayhaven-ansible-runner
                DISCORD_WEBHOOK_URL=https://hooks.example.invalid/id/token
                export CURL_ARGS={str(curl_args)!r}
                failed_playbook_targets() {{ printf 'test-host\\n'; }}
                curl() {{ cat -- >/dev/null; printf '%s\\n' "$*" > "$CURL_ARGS"; }}
                send_failure_notification 1
                """
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            arguments = shlex.split(curl_args.read_text(encoding="utf-8"))
            for option in ("--retry", "--retry-max-time"):
                self.assertGreater(int(arguments[arguments.index(option) + 1]), 0)

    def test_success_and_other_runner_commands_do_not_send_failure_notices(
        self,
    ) -> None:
        for command, status in (("converge", 0), ("fixture-action", 19)):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as folder:
                result = self.run_bash(
                    """
                    export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
                    source files/grayhaven-ansible-runner
                    LOCK_FILE="$FIXTURE_ROOT/lock"
                    PENDING_FILE="$FIXTURE_ROOT/pending"
                    drop_to_ansible() { :; }
                    parse_args() { RUNNER_COMMAND="$FIXTURE_COMMAND"; }
                    cleanup_runtime_files() { :; }
                    write_convergence_metric() { :; }
                    send_failure_notification() { touch "$FIXTURE_ROOT/notified"; }
                    run_once() { return "$FIXTURE_STATUS"; }
                    main
                    """,
                    env={
                        "FIXTURE_ROOT": folder,
                        "FIXTURE_COMMAND": command,
                        "FIXTURE_STATUS": str(status),
                    },
                )
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertFalse((pathlib.Path(folder) / "notified").exists())

    def test_poller_change_and_no_change_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            for name in ("control", "credential"):
                (root / name).touch()
            for name in ("first", "second"):
                (root / f"{name}-upstream").write_text("same")
                (root / f"{name}-current").write_text("same")
            environment = root / "runner.env"
            environment.write_text(
                f"REPO_URL=first\nREPO_REF=example\n"
                f"VAULT_REPO_URL=second\nVAULT_REPO_REF=example\n"
                f"CHECKOUT_DIR='{root / 'first-current'}'\n"
                f"VAULT_CHECKOUT_DIR='{root / 'second-current'}'\n",
                encoding="utf-8",
            )
            marker = root / "called"

            def poll(extra: str = "") -> subprocess.CompletedProcess[str]:
                return self.run_bash(
                    f"""
                    export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
                    source files/grayhaven-ansible-poller
                    STATE_DIR={str(root)!r}; RUNNER_ENV={str(environment)!r}
                    ANSIBLE_CONTROL_PRIVATE_KEY={str(root / "control")!r}
                    VAULT_DEPLOY_PRIVATE_KEY={str(root / "credential")!r}
                    drop_to_ansible() {{ :; }}; prepare_github_known_hosts() {{ :; }}
                    remote_ref() {{ cat "$FIXTURE_ROOT/$1-upstream"; }}
                    checkout_head() {{ if [[ -f "$1" ]]; then cat "$1"; fi; }}
                    sudo() {{ printf 'called\n' >> "$FIXTURE_ROOT/called"; }}
                    {extra}
                    main
                    """,
                    env={"FIXTURE_ROOT": str(root)},
                )

            result = poll()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())

            for index, name in enumerate(("first", "second"), 1):
                (root / f"{name}-upstream").write_text("updated")
                result = poll()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(marker.read_text().splitlines()), index)
                (root / f"{name}-current").write_text("updated")
                self.assertEqual(poll().returncode, 0)
                self.assertEqual(len(marker.read_text().splitlines()), index)

            (root / "first-current").unlink()
            self.assertEqual(poll().returncode, 0)
            self.assertEqual(len(marker.read_text().splitlines()), 3)
            self.assertEqual(poll("sudo() { return 7; }").returncode, 7)
            for extra in ("remote_ref() { return 1; }", "remote_ref() { :; }"):
                self.assertNotEqual(poll(extra).returncode, 0)

            fake_checkout = root / "checkout"
            (fake_checkout / ".git").mkdir(parents=True)
            result = self.run_bash(
                """
                export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1
                source files/grayhaven-ansible-poller
                GITHUB_KNOWN_HOSTS="$FIXTURE_ROOT/hosts"
                git() {
                  if [[ "$1" == "-C" ]]; then printf '%s\n' "$FIXTURE_REF";
                  else printf '%s\t%s\n' "$FIXTURE_REF" refs/heads/example; fi
                }
                remote_ref fixture-repository example
                checkout_head "$FIXTURE_ROOT/checkout"
                checkout_head "$FIXTURE_ROOT/missing"
                """,
                env={"FIXTURE_ROOT": str(root), "FIXTURE_REF": "mock-ref"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["mock-ref", "mock-ref"])

        missing = self.run_bash(
            "export GRAYHAVEN_UNIT_TEST_SOURCE_ONLY=1; source files/grayhaven-ansible-poller; require_file /fixture/missing"
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_gtmux_validation_create_and_attach(self) -> None:
        script = REPO_ROOT / "roles/admin_access/files/gtmux"
        result = self.run_bash(f"bash {script} bad")
        self.assertEqual(result.returncode, 2)
        result = self.run_bash(f"bash {script} one two")
        self.assertEqual(result.returncode, 2)
        result = self.run_bash(f"TMUX=active bash {script} --reset")
        self.assertEqual(result.returncode, 1)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            fake_bin, home = root / "bin", root / "home"
            fake_bin.mkdir()
            home.mkdir()
            executable(
                fake_bin / "tmux",
                """
                #!/usr/bin/env bash
                printf '%s\n' "$*" >> "$TMUX_LOG"
                if [[ "$1" == has-session ]]; then [[ -f "$TMUX_STATE" ]];
                elif [[ "$1" == new-session ]]; then touch "$TMUX_STATE";
                elif [[ "$1" == kill-session ]]; then rm -f "$TMUX_STATE"; fi
                """,
            )
            executable(
                fake_bin / "hostname",
                "#!/usr/bin/env bash\nprintf 'host.example.invalid\n'\n",
            )
            env = {
                "HOME": str(home),
                "PATH": f"{fake_bin}:{os.defpath}",
                "GRAYHAVEN_TMUX_SESSION_NAME": "mock-session",
                "SSH_AUTH_SOCK": str(root / "agent.sock"),
                "TMUX_LOG": str(root / "tmux.log"),
                "TMUX_STATE": str(root / "state"),
            }
            result = self.run_bash(f"env -u TMUX bash {script}", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            stable_sock = home / ".ssh" / "ssh_auth_sock"
            self.assertTrue(stable_sock.is_symlink())
            self.assertEqual(os.readlink(stable_sock), str(root / "agent.sock"))

            result = self.run_bash(f"env -u TMUX bash {script}", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "new-session", (root / "tmux.log").read_text(encoding="utf-8")
            )

            (root / "state").touch()
            log_start = (root / "tmux.log").read_text(encoding="utf-8").splitlines()
            result = self.run_bash(f"env -u TMUX bash {script} --reset", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            log_end = (root / "tmux.log").read_text(encoding="utf-8").splitlines()
            reset_log = log_end[len(log_start) :]
            self.assertEqual(
                reset_log[0], "has-session -t " + env["GRAYHAVEN_TMUX_SESSION_NAME"]
            )
            self.assertEqual(
                reset_log[1], "kill-session -t " + env["GRAYHAVEN_TMUX_SESSION_NAME"]
            )
            self.assertTrue(
                any(entry.startswith("new-session ") for entry in reset_log)
            )
            self.assertTrue((root / "state").exists())

            (root / "state").unlink()
            (home / ".tmux").mkdir()
            workspace = home / ".tmux" / "grayhaven-workspace.tmux"
            workspace.write_text(
                'touch "$WORKSPACE_MARKER"\ntmux new-session -d -s "$GRAYHAVEN_TMUX_SESSION_NAME"\n',
                encoding="utf-8",
            )
            env["WORKSPACE_MARKER"] = str(root / "workspace-ran")
            result = self.run_bash(f"env -u TMUX bash {script}", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "workspace-ran").exists())

            workspace.unlink()
            (root / "state").unlink()
            executable(
                fake_bin / "tmux", '#!/usr/bin/env bash\n[[ "$1" != has-session ]]\n'
            )
            result = self.run_bash(f"env -u TMUX bash {script}", env=env)
            self.assertEqual(result.returncode, 1)

    def test_galaxy_installer_success_and_count_failure(self) -> None:
        script = REPO_ROOT / "scripts/install-galaxy-collections"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            artifact = root / "example.tar.gz"
            artifact.write_bytes(b"artifact")
            requirements, checksums = root / "requirements.yml", root / "checksums"
            requirements.write_text("collections: []\n", encoding="utf-8")
            checksums.write_text(
                f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  example.tar.gz\n",
                encoding="utf-8",
            )
            fake = root / "ansible-galaxy"
            executable(
                fake,
                f"""
                #!/usr/bin/env bash
                if [[ "$1 $2" == "collection download" ]]; then cp {str(artifact)!r} "${{@: -1}}/example.tar.gz";
                else printf '%s\n' "$3" >> {str(root / "installed")!r}; fi
                """,
            )
            env = {
                "ANSIBLE_GALAXY_BIN": str(fake),
                "ANSIBLE_COLLECTIONS_PATH": str(root / "collections"),
            }
            result = self.run_bash(f"{script} {requirements} {checksums}", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "installed").exists())
            executable(fake, "#!/usr/bin/env bash\nexit 0\n")
            result = self.run_bash(f"{script} {requirements} {checksums}", env=env)
            self.assertNotEqual(result.returncode, 0)
