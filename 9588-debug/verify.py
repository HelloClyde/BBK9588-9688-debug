#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Verify a checked 9588 JZ4740 build using MIPS/controller models.

No USB port or physical device is accessed. Local ROM checks are optional.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE=Path(__file__).resolve().parent
ROOT=SOURCE.parent


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root",type=Path)
    parser.add_argument("--build-dir",type=Path)
    parser.add_argument("--firmware",type=Path,help="optional checked local raw/wrapped 9588 firmware")
    parser.add_argument("--foreign-firmware",type=Path,help="optional 9688/other ROM that must fail 9588 SHA256 guards")
    args=parser.parse_args()
    folder=(args.build_dir or ROOT/"build/9588-debug").resolve()
    report=json.loads((folder/"build.json").read_text(encoding="utf-8"))
    if report.get("target")!="9588" or report.get("soc")!="JZ4740" or report.get("mode")!="single":
        raise ValueError("verification only supports a checked 9588 JZ4740 single build")
    artifact=folder/report["binary"]
    digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
    if digest!=report["sha256"]:
        raise ValueError("BDA does not match its build SHA256")
    spec=importlib.util.spec_from_file_location("debug9588_verify_build",SOURCE/"build.py")
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)
    builder.configure_sdk(args.sdk_root)
    for name,expected in report["source_sha256"].items():
        if name.startswith("external-profile/"):
            raise ValueError("verification requires an in-tree checked profile")
        path=builder.SDK_ROOT/name[4:] if name.startswith("sdk/") else ROOT/name
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError("source changed after build: "+name)
    from bda_packer.header import verify
    checked=verify(artifact.read_bytes())
    if not checked["checksum_ok"] or checked["category"]!=9 or checked["title"]!=report["menu_title"]:
        raise ValueError("BDA header/checksum verification failed")
    profile=json.loads((ROOT/report["profile_file"]).read_text(encoding="utf-8"))
    builder.validate_target_profile(profile)
    environment=os.environ.copy()
    environment["BDA_SDK_ROOT"]=str(builder.SDK_ROOT)
    environment["BBK9588_BUILD_DIR"]=str(folder)
    firmware_check=None
    if args.firmware:
        firmware_check=builder.verify_firmware(args.firmware,profile)
        environment["BBK9588_FIRMWARE"]=str(args.firmware.resolve())
    else:
        environment.pop("BBK9588_FIRMWARE",None)
    foreign_rejected=None
    if args.foreign_firmware:
        try:
            builder.verify_firmware(args.foreign_firmware,profile)
        except ValueError:
            foreign_rejected=True
        else:
            raise ValueError("foreign firmware unexpectedly matched the selected 9588 profile")
    result=run_suite(["test_build","test_client","test_device","test_runtime","test_usb","test_verify"],
                     environment,folder,"verify-tests.log")
    evidence={"target":"9588","soc":"JZ4740","bda_sha256":digest,"bda_bytes":artifact.stat().st_size,
        "source_manifest_matches":True,"bda_header_checksum_valid":True,"tests":result,
        "firmware_profile_verified":firmware_check is not None,"firmware_verification":firmware_check,
        "foreign_firmware_rejected":foreign_rejected,"hardware_verified":False,
        "full_firmware_verified":False,"hardware_modified":False,
        "status":"passed MIPS/controller model verification; physical 9588 verification pending" if result["success"] else "model tests failed"}
    (folder/"verification.json").write_text(json.dumps(evidence,indent=2)+"\n",encoding="utf-8")
    return 0 if result["success"] else 1


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


if __name__=="__main__":
    raise SystemExit(main())
