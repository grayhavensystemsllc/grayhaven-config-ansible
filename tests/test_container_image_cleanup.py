"""Image retention is exercised with invented container and registry metadata."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import unittest
from unittest import mock

from tests.helpers import load_program

cleanup = load_program(
    "fixture_image_cleanup",
    "roles/system_packages/files/grayhaven-prune-container-images",
)


class ImageCleanupTests(unittest.TestCase):
    def setUp(self):
        self.container = "fixture-container"
        self.repository = "registry.example.test:5443/team/fixture-service"
        self.digest = "sha256:" + "1" * 64
        self.image = self.repository + "@" + self.digest
        self.current = {
            "Id": "f" * 64,
            "Image": "a" * 64,
            "ImageDigest": self.digest,
            "State": {"Running": True, "Health": {"Status": "healthy"}},
        }
        self.images = [
            self.row("a", [self.image]),
            self.row("b", [self.repository + ":previous"]),
            self.row("c", [self.repository + "@sha256:" + "2" * 64]),
            self.row("d", ["registry.example.test/other/service:retained"]),
            self.row("e", [self.repository + ":alias", "registry.example.test/other/service:alias"]),
            self.row("0", None),
            self.row("9", []),
        ]
        self.used = [{"ImageID": "a" * 64}, {"ImageID": "sha256:" + "c" * 64}]
        self.removed = []
        self.calls = []
        self.inspections = []
        self.references = []

    @staticmethod
    def row(identity, names):
        return {"Id": identity * 64, "Names": names}

    def podman(self, arguments):
        self.calls.append(arguments)
        if arguments[:2] == ["container", "inspect"]:
            result = self.inspections.pop(0) if self.inspections else [self.current]
        elif arguments[0] == "ps":
            result = self.references.pop(0) if self.references else self.used
        elif arguments[0] == "images":
            result = self.images
        elif arguments[:3] == ["image", "rm", "--no-prune"]:
            identity = arguments[3]
            self.removed.append(identity)
            self.images = [row for row in self.images if row.get("Id", row.get("ID")) != identity]
            return identity
        else:
            raise AssertionError("Unexpected fixture command")
        return json.dumps(result)

    def run_cleanup(self, **kwargs):
        with mock.patch.object(cleanup, "command", side_effect=self.podman):
            return cleanup.cleanup(self.container, self.image, **kwargs)

    def test_removes_only_unused_repository_images_and_is_idempotent(self):
        before = copy.deepcopy(self.images)
        self.assertEqual(self.run_cleanup(), {"removed": 1, "eligible": 1})
        self.assertEqual(self.removed, ["b" * 64])
        self.assertEqual(self.images, [row for row in before if row["Id"] != "b" * 64])
        self.assertEqual(self.run_cleanup(), {"removed": 0, "eligible": 0})
        self.assertTrue(all("--force" not in args for args in self.calls))

    def test_dry_run_reports_candidates_without_removing_images(self):
        self.images.append(self.row("b", [self.repository + ":duplicate-alias"]))
        self.assertEqual(self.run_cleanup(dry_run=True), {"removed": 0, "eligible": 1})
        self.assertEqual(self.removed, [])

    def test_new_container_reference_preserves_an_original_candidate(self):
        self.references = [self.used, [*self.used, {"ImageID": "b" * 64}]]
        self.assertEqual(self.run_cleanup(), {"removed": 0, "eligible": 1})
        self.assertEqual(self.removed, [])

    def test_changed_deployment_stops_cleanup(self):
        replacement = copy.deepcopy(self.current)
        replacement["Id"] = "8" * 64
        self.inspections = [[self.current], [replacement]]
        with self.assertRaises(cleanup.ImageCleanupError):
            self.run_cleanup()
        self.assertEqual(self.removed, [])

    def test_unhealthy_unapproved_or_missing_container_stops_before_deletion(self):
        stopped = copy.deepcopy(self.current)
        stopped["State"]["Running"] = False
        unhealthy = copy.deepcopy(self.current)
        unhealthy["State"]["Health"]["Status"] = "starting"
        different = copy.deepcopy(self.current)
        different["ImageDigest"] = "sha256:" + "3" * 64
        for result in ([], [self.current, self.current], [stopped], [unhealthy], [different]):
            with self.subTest(result_count=len(result)):
                self.inspections = [result]
                with self.assertRaises(cleanup.ImageCleanupError):
                    self.run_cleanup()
        self.assertEqual(self.removed, [])

    def test_unhealthy_recheck_stops_before_removal(self):
        unhealthy = copy.deepcopy(self.current)
        unhealthy["State"]["Health"]["Status"] = "unhealthy"
        self.inspections = [[self.current], [unhealthy]]
        with self.assertRaises(cleanup.ImageCleanupError):
            self.run_cleanup()
        self.assertEqual(self.removed, [])

    def test_invalid_caller_inputs_never_reach_podman(self):
        for container, image in (("-invalid", self.image), (self.container, self.repository + ":floating")):
            with self.subTest(), mock.patch.object(cleanup, "command") as command:
                with self.assertRaises(cleanup.ImageCleanupError):
                    cleanup.cleanup(container, image)
                command.assert_not_called()

    def test_image_identity_normalization_and_repository_tags(self):
        self.assertEqual(cleanup.image_id("sha256:" + "a" * 64), "a" * 64)
        for invalid in (None, "short"):
            with self.subTest(), self.assertRaises(cleanup.ImageCleanupError):
                cleanup.image_id(invalid)
        self.assertEqual(cleanup.repository(self.repository + ":previous"), self.repository)
        self.assertEqual(cleanup.repository(self.image), self.repository)
        with mock.patch.object(cleanup, "command", side_effect=self.podman):
            result = cleanup.cleanup(self.container, self.repository + ":stable@" + self.digest)
        self.assertEqual(result["removed"], 1)

    def test_alternate_id_field_is_supported(self):
        old = self.images[1]
        old["ID"] = old.pop("Id")
        self.assertEqual(self.run_cleanup()["removed"], 1)

    def test_malformed_metadata_stops_without_removal(self):
        with mock.patch.object(cleanup, "command", return_value="{}"):
            with self.assertRaises(cleanup.ImageCleanupError):
                cleanup.records(["images"])
        with mock.patch.object(cleanup, "command", return_value="[0]"):
            with self.assertRaises(cleanup.ImageCleanupError):
                cleanup.records(["images"])
        for names in ("unexpected", [None]):
            with self.subTest():
                self.images[1]["Names"] = names
                with self.assertRaises(cleanup.ImageCleanupError):
                    self.run_cleanup()
        self.assertEqual(self.removed, [])

    def test_command_checks_exit_status_and_bounds_execution(self):
        result = subprocess.CompletedProcess([], 0, stdout="[]")
        with mock.patch.object(cleanup.subprocess, "run", return_value=result) as run:
            self.assertEqual(cleanup.command(["images", "--format", "json"]), "[]")
        args, options = run.call_args
        self.assertEqual(args[0], ["podman", "images", "--format", "json"])
        self.assertTrue(options["check"])
        self.assertGreater(options["timeout"], 0)

    def test_cli_reports_json_and_redacts_provider_failures(self):
        output = io.StringIO()
        with (mock.patch.object(cleanup, "command", side_effect=self.podman),
              contextlib.redirect_stdout(output)):
            self.assertEqual(cleanup.main([self.container, self.image, "--dry-run"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"removed": 0, "eligible": 1})
        for error in (
            OSError("provider diagnostic"),
            subprocess.TimeoutExpired("fixture-command", 1),
            subprocess.CalledProcessError(1, "fixture-command", stderr="provider diagnostic"),
            ValueError("provider diagnostic"),
        ):
            output, errors = io.StringIO(), io.StringIO()
            with (mock.patch.object(cleanup, "command", side_effect=error),
                  contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors)):
                self.assertEqual(cleanup.main([self.container, self.image]), 1)
            self.assertEqual(output.getvalue(), "")
            self.assertTrue(errors.getvalue())
            self.assertNotIn("provider diagnostic", errors.getvalue())

    def test_removal_failure_is_not_reported_as_success(self):
        original = self.podman

        def reject_removal(args):
            if args[:2] == ["image", "rm"]:
                raise subprocess.CalledProcessError(1, "fixture-command")
            return original(args)

        with mock.patch.object(cleanup, "command", side_effect=reject_removal):
            with self.assertRaises(subprocess.CalledProcessError):
                cleanup.cleanup(self.container, self.image)
        self.assertEqual(self.removed, [])

    def test_cli_requires_explicit_inputs(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            cleanup.main([])
        self.assertEqual(result.exception.code, 2)
