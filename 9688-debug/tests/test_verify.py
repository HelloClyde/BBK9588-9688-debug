# SPDX-License-Identifier: GPL-3.0-only
"""Ensure skipped subtests cannot corrupt published verification counts."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("debug9688_verify", Path(__file__).resolve().parents[1] / "verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class VerificationSummaryTests(unittest.TestCase):
    def test_success_whole_case_and_subtest_skips_are_counted_separately(self):
        fixture = """import unittest
class Fixture(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)
    def test_skip(self):
        self.skipTest("needs private ROM")
    def test_partial(self):
        with self.subTest(chip="fixture"):
            self.skipTest("needs private ROM")
"""
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "tests").mkdir()
            (folder / "tests/fixture.py").write_text(fixture, encoding="utf-8")
            with patch.object(verify, "SOURCE", folder), contextlib.redirect_stdout(io.StringIO()):
                result = verify.run_suite(["fixture"], os.environ.copy(), folder, "result.log")
            self.assertEqual((result["run"], result["passed"], result["skipped_tests"],
                              result["skipped_subtests"], result["skipped"]), (3, 1, 1, 1, 2))
            self.assertTrue(result["success"])
