"""Test that unexpected failures cannot disclose supplied values."""

import io
import unittest
import uuid


class DiagnosticTests(unittest.TestCase):
    def test_failures_errors_and_subtests_omit_supplied_values(self):
        value = uuid.uuid4().hex

        class Probe(unittest.TestCase):
            def runTest(self):
                with self.subTest(value=value):
                    self.fail(value)
                raise RuntimeError(value)

        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(
            unittest.TestSuite([Probe()])
        )
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(result.errors), 1)
        self.assertTrue(value not in output.getvalue())
