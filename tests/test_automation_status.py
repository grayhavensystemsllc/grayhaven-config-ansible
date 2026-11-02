from __future__ import annotations

import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

from tests.helpers import load_program


status = load_program(
    "grayhaven_automation_status", "files/grayhaven-ansible-automation-status"
)
TEST_UNITS = {"poller": "mock-primary", "runner": "mock-secondary"}


def unit_records(part="poller", *, state="waiting", service_state="inactive"):
    prefix = TEST_UNITS[part]
    return [
        {
            "Id": prefix + ".timer",
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": state,
            "UnitFileState": "enabled",
            "TimersCalendar": "[{ OnCalendar=*-*-* *:0/5:00 }]",
            "Result": "success",
            "NeedDaemonReload": "no",
        },
        {
            "Id": prefix + ".service",
            "LoadState": "loaded",
            "ActiveState": service_state,
            "SubState": "dead",
            "Result": "success",
            "ExecMainStatus": "0",
            "NeedDaemonReload": "no",
        },
    ]


def raw_units(records):
    return "\n\n".join(
        "\n".join(f"{k}={v}" for k, v in unit.items()) for unit in records
    )


class AutomationStatusTests(unittest.TestCase):
    def inspect(self, records=None, realtime=20, monotonic=0, components=("poller",)):
        if records is None:
            records = unit_records()
        with (
            mock.patch.object(status, "command", return_value=raw_units(records)),
            mock.patch.object(
                status,
                "next_elapse",
                side_effect=lambda u, c: realtime if c == "Realtime" else monotonic,
            ),
            mock.patch.object(status.time, "time_ns", return_value=10_000),
            mock.patch.object(status.time, "monotonic_ns", return_value=10_000),
        ):
            return status.inspect(components, TEST_UNITS)

    def test_finite_future_clock_required_while_waiting(self):
        for realtime, monotonic, healthy in (
            (20, 0, True),
            (0, 20, True),
            (20, 20, True),
            (0, 0, False),
            (10, 10, False),
            (9, 0, False),
            (status.INFINITY, 0, False),
            (0, status.INFINITY, False),
            (status.INFINITY, 20, True),
        ):
            with self.subTest(realtime=realtime, monotonic=monotonic):
                self.assertEqual(
                    self.inspect(realtime=realtime, monotonic=monotonic)["healthy"],
                    healthy,
                )

    def test_loaded_enabled_timer_and_successful_service_required(self):
        variants = [
            (0, "LoadState", "not-found"),
            (0, "ActiveState", "inactive"),
            (0, "UnitFileState", "disabled"),
            (0, "Result", "resources"),
            (0, "NeedDaemonReload", "yes"),
            (0, "TimersCalendar", ""),
            (0, "SubState", "elapsed"),
            (1, "LoadState", "not-found"),
            (1, "ActiveState", "failed"),
            (1, "Result", "exit-code"),
            (1, "ExecMainStatus", "1"),
            (1, "NeedDaemonReload", "yes"),
        ]
        for index, key, value in variants:
            with self.subTest(index=index, key=key):
                records = unit_records()
                records[index][key] = value
                self.assertFalse(self.inspect(records)["healthy"])
                del records[index][key]
                self.assertFalse(self.inspect(records)["healthy"])
        for records in ([], unit_records()[:1], unit_records()[1:]):
            self.assertFalse(self.inspect(records)["healthy"])

    def test_running_oneshot_is_valid_without_next_deadline(self):
        for service_state, healthy in (
            ("active", True),
            ("activating", True),
            ("inactive", False),
            ("failed", False),
        ):
            self.assertEqual(
                self.inspect(
                    unit_records(state="running", service_state=service_state),
                    realtime=0,
                )["healthy"],
                healthy,
            )
        self.assertFalse(
            self.inspect(unit_records(state="elapsed", service_state="activating"))[
                "healthy"
            ]
        )
        self.assertTrue(self.inspect(unit_records(service_state="active"))["healthy"])

    def test_every_requested_component_must_be_present_and_healthy(self):
        records = unit_records() + unit_records("runner")
        self.assertTrue(self.inspect(records, components=status.COMPONENTS)["healthy"])
        records[-1]["ExecMainStatus"] = "2"
        self.assertFalse(self.inspect(records, components=status.COMPONENTS)["healthy"])
        self.assertTrue(self.inspect(records)["healthy"])

    def test_bus_clock_values_are_strict_unsigned_microseconds(self):
        for value, expected in (
            ("t 0", 0),
            ("t 81", 81),
            (f"t {status.INFINITY}", status.INFINITY),
        ):
            with mock.patch.object(status, "command", return_value=value) as run:
                self.assertEqual(status.next_elapse("a-b.timer", "Realtime"), expected)
                self.assertIn(
                    "/org/freedesktop/systemd1/unit/a_2db_2etimer",
                    run.call_args.args[0],
                )
        for value in (
            "",
            "t",
            "s 81",
            "t nope",
            "t -1",
            f"t {status.INFINITY + 1}",
            "t 1 extra",
        ):
            with (
                mock.patch.object(status, "command", return_value=value),
                self.assertRaises(ValueError),
            ):
                status.next_elapse("a.timer", "Monotonic")

    def test_repair_only_restarts_bad_timers_and_rechecks(self):
        bad = {
            "healthy": False,
            "components": {"poller": False, "runner": True},
            "units": [],
        }
        good = {
            "healthy": True,
            "components": {"poller": True, "runner": True},
            "units": [],
        }
        with (
            mock.patch.object(status, "inspect", side_effect=[bad, good]),
            mock.patch.object(status, "command") as run,
        ):
            result = status.evaluate(status.COMPONENTS, True, TEST_UNITS)
            self.assertTrue(result["healthy"])
            self.assertTrue(result["changed"])
            run.assert_called_once_with(
                ["/usr/bin/systemctl", "restart", TEST_UNITS["poller"] + ".timer"]
            )
        for repair in (False, True):
            with (
                mock.patch.object(status, "inspect", return_value=good) as inspect,
                mock.patch.object(status, "command") as run,
            ):
                self.assertFalse(status.evaluate(status.COMPONENTS, repair, TEST_UNITS)["changed"])
                self.assertEqual(inspect.call_count, 1)
                self.assertEqual(inspect.call_args.args[0], status.COMPONENTS)
                run.assert_not_called()
        with (
            mock.patch.object(status, "inspect", return_value=bad),
            mock.patch.object(status, "command"),
        ):
            self.assertFalse(status.evaluate(status.COMPONENTS, True, TEST_UNITS)["healthy"])

    def test_configured_unit_names_and_invalid_inputs(self):
        import argparse

        for value in ("fixture-poller", "fixture.runner", "a_b@x:y"):
            self.assertEqual(status.unit_base(value), value)
        for value in ("", "--all", "a/b", "a b", "a;reboot"):
            with self.assertRaises(argparse.ArgumentTypeError):
                status.unit_base(value)
        units = TEST_UNITS
        bad = {"healthy": False, "components": {"poller": False}, "units": []}
        with (
            mock.patch.object(status, "inspect", return_value=bad) as inspect,
            mock.patch.object(status, "command") as run,
        ):
            status.evaluate(("poller",), True, units)
            self.assertEqual(inspect.call_args.args[1], units)
            run.assert_called_once_with(
                ["/usr/bin/systemctl", "restart", TEST_UNITS["poller"] + ".timer"]
            )
        records = unit_records()
        with (
            mock.patch.object(
                status, "command", return_value=raw_units(records)
            ) as run,
            mock.patch.object(status, "next_elapse", return_value=status.INFINITY - 1),
        ):
            self.assertTrue(status.inspect(("poller",), units)["healthy"])
            self.assertIn(TEST_UNITS["poller"] + ".timer", run.call_args.args[0])

    def test_command_is_bounded_and_propagates_failure(self):
        with mock.patch.object(
            status.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, " value ", ""),
        ) as run:
            self.assertEqual(status.command(["fixture"]), "value")
            self.assertGreater(run.call_args.kwargs["timeout"], 0)
            self.assertTrue(run.call_args.kwargs["check"])
        for error in (
            OSError("missing"),
            subprocess.TimeoutExpired("fixture", 30),
            subprocess.CalledProcessError(1, "fixture"),
        ):
            with (
                mock.patch.object(status.subprocess, "run", side_effect=error),
                self.assertRaises(type(error)),
            ):
                status.command(["fixture"])

    def test_cli_reports_unhealthy_without_traceback_and_fails_repair(self):
        fake_units = ["--poller-unit", TEST_UNITS["poller"], "--runner-unit", TEST_UNITS["runner"]]
        for args, healthy, expected, parts in (
            ([], True, 0, status.COMPONENTS),
            (["--component", "runner"], False, 0, ("runner",)),
            (["--repair"], False, 1, status.COMPONENTS),
        ):
            output = io.StringIO()
            with (
                mock.patch.object(
                    status, "evaluate", return_value={"healthy": healthy}
                ) as evaluate,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(status.main(fake_units + args), expected)
            self.assertEqual(json.loads(output.getvalue())["healthy"], healthy)
            self.assertEqual(evaluate.call_args.args[0], parts)
            self.assertEqual(evaluate.call_args.args[2], TEST_UNITS)
        for error in (
            OSError("missing"),
            subprocess.TimeoutExpired("fixture", 30),
            ValueError("invalid clock"),
        ):
            for args, expected in (([], 0), (["--repair"], 1)):
                output = io.StringIO()
                with (
                    mock.patch.object(status, "evaluate", side_effect=error),
                    contextlib.redirect_stdout(output),
                ):
                    self.assertEqual(status.main(fake_units + args), expected)
                result = json.loads(output.getvalue())
                self.assertFalse(result["healthy"])
                self.assertIn(str(error), result["error"])


if __name__ == "__main__":
    unittest.main()
