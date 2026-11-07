"""Keep test output and exception diagnostics free of fixture and environment values."""

from __future__ import annotations

import contextlib
import os
import sys
import traceback
import unittest


def safe_exception(result, error, test):
    del result, test
    kind, _, trace = error
    locations = [
        f"  {frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(trace)
    ]
    return "Code locations:\n" + "\n".join(locations) + "\n" + kind.__name__ + "\n"


def safe_description(result, test):
    del result
    parent = getattr(test, "test_case", test)
    return type(parent).__name__ + "." + getattr(parent, "_testMethodName", "test")


@contextlib.contextmanager
def discard_test_output():
    """Discard Python, logging and inherited child-process output during a test."""
    saved = {descriptor: os.dup(descriptor) for descriptor in (1, 2)}
    try:
        for stream in (sys.stdout, sys.stderr):
            stream.flush()
        with open(os.devnull, "w") as sink:
            for descriptor in saved:
                os.dup2(sink.fileno(), descriptor)
            yield
            for stream in (sys.stdout, sys.stderr):
                stream.flush()
    finally:
        for descriptor, original in saved.items():
            os.dup2(original, descriptor)
            os.close(original)


def install():
    if getattr(unittest.TestCase, "_safe_diagnostics_installed", False):
        return
    original_run = unittest.TestCase.run

    def run(case, result=None):
        with discard_test_output():
            return original_run(case, result)

    unittest.TestCase.run = run
    unittest.TestCase._safe_diagnostics_installed = True
    unittest.TestResult._exc_info_to_string = safe_exception
    unittest.TextTestResult.getDescription = safe_description
