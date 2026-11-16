#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Verify a universal or chip-only 9688 BDA and save model-test evidence."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chip", choices=("jz4730", "jz4740"), help="verify a chip-only developer build; default is universal")
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--firmware-jz4730", type=Path)
    parser.add_argument("--firmware-jz4740", type=Path)
    parser.add_argument("--firmware-legacy", type=Path, help="verify an unsupported legacy firmware's GUI notice fingerprint")
    args = parser.parse_args()
    folder = (args.build_dir or ROOT / "build" / ("9688-debug-" + args.chip if args.chip else "9688-debug")).resolve()
    report = json.loads((folder / "build.json").read_text(encoding="utf-8"))
    universal = report.get("mode") == "universal"
    if (universal and args.chip) or (not universal and report.get("soc", "JZ4740").lower() != args.chip):
        raise ValueError("selected test mode does not match build manifest")
    if universal and args.firmware:
        parser.error("universal verification uses --firmware-jz4730 and --firmware-jz4740")
    artifact = folder / report.get("binary", "Debug9688.bda")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if digest != report["sha256"]:
        raise ValueError("BDA does not match build SHA256")
    for name, expected in report["source_sha256"].items():
        if name.startswith("external-profile/"):
            raise ValueError("external profile source cannot be verified from this workspace")
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"source changed after build: {name}; rebuild first")
    sys.path.insert(0, str(ROOT / "sdk"))
    from bda_packer.header import verify
    checked = verify(artifact.read_bytes())
    if (not checked["checksum_ok"] or checked["category"] != 9 or
            checked["file_size_minus_4"] != artifact.stat().st_size - 4 or
            checked["title"] != report.get("menu_title", "9688璋冭瘯")):
        raise ValueError("BDA header verification failed")
    spec = importlib.util.spec_from_file_location("debug9688_verify_build", SOURCE / "build.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    environment = os.environ.copy()
    environment["BBK9688_UNIVERSAL_BUILD_DIR"] = str(folder)
    legacy_notice_verification = None
    if args.firmware_legacy:
        if not universal:
            parser.error("--firmware-legacy is only used by universal GUI diagnostics")
        notice_profile = json.loads(builder.LEGACY_NOTICE_PROFILE.read_text(encoding="utf-8"))
        legacy_notice_verification = builder.verify_notice_firmware(args.firmware_legacy, notice_profile)
        environment["BBK9688_FIRMWARE_LEGACY"] = str(args.firmware_legacy.resolve())
    firmware_verification = {}
    for chip in ("jz4730", "jz4740"):
        path = getattr(args, "firmware_" + chip)
        if path:
            environment["BBK9688_FIRMWARE_" + chip.upper()] = str(path.resolve())
            profile = json.loads(builder.SOC_PROFILES[chip.upper()].read_text(encoding="utf-8"))
            firmware_verification[chip.upper()] = builder.verify_firmware(path, profile)
    if args.firmware:
        environment["BBK9688_FIRMWARE_" + args.chip.upper()] = str(args.firmware.resolve())
        profile = json.loads(builder.SOC_PROFILES[args.chip.upper()].read_text(encoding="utf-8"))
        firmware_verification[args.chip.upper()] = builder.verify_firmware(args.firmware, profile)
    targets = ([(item["soc"].lower(), folder / item["directory"]) for item in report["components"]]
               if universal else [(args.chip, folder)])
    suites = []
    for chip, target in targets:
        environment["BBK9688_BUILD_DIR"] = str(target)
        firmware_path = environment.get("BBK9688_FIRMWARE_" + chip.upper())
        if firmware_path:
            environment["BBK9688_FIRMWARE"] = firmware_path
        else:
            environment.pop("BBK9688_FIRMWARE", None)
        usb_test = "test_usb_jz4730" if chip == "jz4730" else "test_usb"
        modules = ["test_build", "test_client", "test_device", "test_runtime", "test_verify", usb_test, "test_discovery", "test_management", "test_workflow_client", "test_workflow_device"]
        suites.append(run_suite(modules, environment, folder, "verify-" + chip + "-tests.log"))
    if universal:
        suites.append(run_suite(["test_universal"], environment, folder, "verify-universal-tests.log"))
    tests_run = sum(suite["run"] for suite in suites)
    tests_skipped = sum(suite["skipped"] for suite in suites)
    success = all(suite["success"] for suite in suites)
    log = "\n".join((folder / suite["log"]).read_text(encoding="utf-8") for suite in suites)
    (folder / "verify-tests.log").write_text(log, encoding="utf-8")
    evidence = {
        "soc": report["soc"], "binary": artifact.name, "bda_sha256": digest,
        "bda_bytes": artifact.stat().st_size, "image_id": report["image_id"],
        "source_manifest_matches": True, "bda_header_checksum_valid": True,
        "firmware_profile_verified": all(chip.upper() in firmware_verification for chip, _ in targets),
        "firmware_verification": firmware_verification,
        "legacy_notice_verification": legacy_notice_verification,
        "tests": {"suites": suites, "run": tests_run, "skipped": tests_skipped,
                  "passed": sum(suite["passed"] for suite in suites),
                  "skipped_tests": sum(suite["skipped_tests"] for suite in suites),
                  "skipped_subtests": sum(suite["skipped_subtests"] for suite in suites),
                  "success": success, "log": "verify-tests.log"},
        "hardware_verified": False, "full_firmware_verified": False, "hardware_modified": False,
        "status": "passed MIPS/controller model verification; physical device verification pending" if success else "model tests failed",
    }
    (folder / "verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if success else 1


def run_suite(modules, environment, folder, name):
    # unittest's skipped total also includes subtests, so it cannot be
    # subtracted from testsRun to count successful test methods.
    runner = r"""
import json, sys, unittest
class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed = self.skipped_tests = self.skipped_subtests = 0
    def startTest(self, test):
        self.active_test = test
        super().startTest(test)
    def addSuccess(self, test):
        self.passed += 1
        super().addSuccess(test)
    def addSkip(self, test, reason):
        if test is self.active_test:
            self.skipped_tests += 1
        else:
            self.skipped_subtests += 1
        super().addSkip(test, reason)
result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(
    unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]))
print("BBK_TEST_RESULT=" + json.dumps({
    "run": result.testsRun, "passed": result.passed,
    "skipped": len(result.skipped), "skipped_tests": result.skipped_tests,
    "skipped_subtests": result.skipped_subtests,
    "success": result.wasSuccessful()}))
raise SystemExit(0 if result.wasSuccessful() else 1)
"""
    command = [sys.executable, "-c", runner, *modules]
    completed = subprocess.run(command, cwd=SOURCE / "tests", env=environment, text=True, capture_output=True)
    log = completed.stdout + completed.stderr
    (folder / name).write_text(log, encoding="utf-8")
    print(log, end="")
    summaries = [line.removeprefix("BBK_TEST_RESULT=") for line in completed.stdout.splitlines()
                 if line.startswith("BBK_TEST_RESULT=")]
    if len(summaries) != 1:
        raise RuntimeError("test runner did not return exactly one structured summary")
    summary = json.loads(summaries[0])
    summary["success"] = bool(summary["success"] and completed.returncode == 0)
    return {"modules": modules, **summary, "log": name}


if __name__ == "__main__":
    raise SystemExit(main())
