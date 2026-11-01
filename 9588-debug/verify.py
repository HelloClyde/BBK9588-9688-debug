#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Verify one universal 9588 BDA, or an explicit single-SoC developer build.

No serial ports or physical devices are accessed. Original ROMs are optional,
local, read-only inputs and are never bundled with source or verification logs.
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
SOCS=("JZ4740","JZ4730","JZ4720")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-root",type=Path)
    parser.add_argument("--chip",choices=tuple(soc.lower() for soc in SOCS))
    parser.add_argument("--build-dir",type=Path)
    parser.add_argument("--firmware",type=Path,help="optional local ROM for explicit --chip verification")
    for soc in SOCS:
        parser.add_argument("--firmware-"+soc.lower(),type=Path,help="optional local ROM for universal verification")
    parser.add_argument("--foreign-firmware",type=Path,help="optional other-target ROM that every selected profile must reject")
    args=parser.parse_args()
    folder=(args.build_dir or ROOT/"build"/("9588-debug-"+args.chip if args.chip else "9588-debug")).resolve()
    report=json.loads((folder/"build.json").read_text(encoding="utf-8"))
    universal=report.get("mode")=="universal"
    if report.get("target")!="9588":
        raise ValueError("selected manifest does not describe 9588 Debug")
    if universal:
        if args.chip or args.firmware:
            parser.error("universal verification uses --firmware-jz4740/4730/4720, not --chip or --firmware")
        if set(report.get("supported_socs",[]))!=set(SOCS):
            raise ValueError("universal build does not contain all three checked SoCs")
    elif not args.chip or report.get("soc","").lower()!=args.chip:
        parser.error("single-SoC verification requires its matching explicit --chip")
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
    selected=([item["soc"] for item in report["components"]] if universal else [report["soc"]])
    if universal:
        registered=builder.registered_profiles()
        profiles={soc:profile for soc,path,profile in registered}
        targets=[(item["soc"],folder/item["directory"]) for item in report["components"]]
    else:
        profiles={report["soc"]:json.loads((ROOT/report["profile_file"]).read_text(encoding="utf-8"))}
        builder.validate_target_profile(profiles[report["soc"]])
        targets=[(report["soc"],folder)]
    environment=os.environ.copy()
    environment["BDA_SDK_ROOT"]=str(builder.SDK_ROOT)
    environment["BBK9588_UNIVERSAL_BUILD_DIR"]=str(folder)
    firmware_paths={soc:getattr(args,"firmware_"+soc.lower()) for soc in SOCS}
    if not universal:
        if any(firmware_paths.values()):
            parser.error("single-SoC verification uses --firmware")
        firmware_paths[report["soc"]]=args.firmware
    firmware_checks={}
    for soc in SOCS:
        path=firmware_paths[soc]
        key="BBK9588_FIRMWARE_"+soc
        environment.pop(key,None)
        if path and soc in profiles:
            firmware_checks[soc]=builder.verify_firmware(path,profiles[soc])
            environment[key]=str(path.resolve())
    cross_rejections=[]
    for soc,profile in profiles.items():
        for other,path in firmware_paths.items():
            if other==soc or not path:
                continue
            try:
                builder.verify_firmware(path,profile)
            except ValueError:
                cross_rejections.append({"profile":soc,"firmware":other,"rejected":True})
            else:
                raise ValueError("cross-SoC ROM unexpectedly matched "+soc)
    foreign_rejected=None
    if args.foreign_firmware:
        for profile in profiles.values():
            try:
                builder.verify_firmware(args.foreign_firmware,profile)
            except ValueError:
                continue
            raise ValueError("foreign firmware unexpectedly matched a 9588 profile")
        foreign_rejected=True
    suites=[]
    for soc,target in targets:
        environment["BBK9588_BUILD_DIR"]=str(target)
        environment.pop("BBK9588_FIRMWARE",None)
        if soc in firmware_checks:
            environment["BBK9588_FIRMWARE"]=str(firmware_paths[soc].resolve())
        usb_test="test_usb_jz4730" if soc=="JZ4730" else "test_usb"
        suites.append(run_suite(["test_build","test_client","test_device","test_runtime",usb_test,"test_verify"],
                                environment,folder,"verify-"+soc.lower()+"-tests.log"))
    if universal:
        suites.append(run_suite(["test_universal"],environment,folder,"verify-universal-tests.log"))
    success=all(suite["success"] for suite in suites)
    log="\n".join((folder/suite["log"]).read_text(encoding="utf-8") for suite in suites)
    (folder/"verify-tests.log").write_text(log,encoding="utf-8")
    evidence={"target":"9588","soc":report["soc"],"bda_sha256":digest,"bda_bytes":artifact.stat().st_size,
        "source_manifest_matches":True,"bda_header_checksum_valid":True,
        "tests":{"suites":suites,"run":sum(s["run"] for s in suites),"passed":sum(s["passed"] for s in suites),
                 "skipped":sum(s["skipped"] for s in suites),"skipped_tests":sum(s["skipped_tests"] for s in suites),
                 "skipped_subtests":sum(s["skipped_subtests"] for s in suites),"success":success,"log":"verify-tests.log"},
        "firmware_profile_verified":all(soc in firmware_checks for soc in selected),
        "firmware_verification":firmware_checks,"cross_firmware_rejections":cross_rejections,
        "foreign_firmware_rejected":foreign_rejected,"hardware_verified":False,
        "full_firmware_verified":False,"hardware_modified":False,
        "status":"passed MIPS/controller model verification; physical 9588 verification pending" if success else "model tests failed"}
    (folder/"verification.json").write_text(json.dumps(evidence,indent=2)+"\n",encoding="utf-8")
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


if __name__=="__main__":
    raise SystemExit(main())
