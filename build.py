#!/usr/bin/env python3
"""Build the explicitly available BBK Debug targets from the fixed SDK."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SDK_COMMIT = "ef3bf3b7328a3a8fa9ba48593c63285300725fb7"
REFERENCE_9688_SHA = "fe9d5b7ef5aebd6142c4ddb79e0bc721362e49bb1ad3cd8d5349a42a12346765"


def git_value(*arguments: str, directory: Path = ROOT) -> str | None:
    command = ["git", "-c", "safe.directory=" + directory.as_posix(), "-C", str(directory), *arguments]
    result = subprocess.run(command, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def run(command: list[object], environment: dict[str, str]) -> None:
    subprocess.run([str(value) for value in command], cwd=ROOT, env=environment, check=True)


def toolchain_folder(explicit: Path | None) -> Path:
    candidates = [explicit] if explicit else [ROOT / "sdk/.toolchain/bin", ROOT / ".toolchain/bin"]
    if not explicit:
        candidates += sorted((ROOT / "sdk/.toolchain").glob("*/bin"))
        candidates += sorted((ROOT / ".toolchain").glob("*/bin"))
    prefix = os.environ.get("BDA_TOOLCHAIN_PREFIX")
    if prefix:
        candidates.insert(0, Path(prefix).parent)
    for folder in candidates:
        if folder and ((folder / "mipsel-none-elf-gcc.exe").is_file() or (folder / "mipsel-none-elf-gcc").is_file()):
            return folder.resolve()
    raise RuntimeError("MIPS toolchain missing: run sdk/scripts/setup_toolchain.ps1 or pass --toolchain PATH/TO/bin")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", choices=("all", "9588", "9688"), default=["all"])
    parser.add_argument("--toolchain", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--firmware-9588", "--firmware-9588-jz4740", dest="firmware_9588_jz4740", type=Path,
                        help="optional local 9588 JZ4740 firmware; --firmware-9588 is a compatibility alias")
    parser.add_argument("--firmware-9588-jz4730", type=Path)
    parser.add_argument("--firmware-9588-jz4720", type=Path)
    parser.add_argument("--firmware-jz4730", type=Path)
    parser.add_argument("--firmware-jz4740", type=Path)
    parser.add_argument("--firmware-legacy", type=Path)
    args = parser.parse_args(argv)
    available = [name for name in ("9588", "9688") if (ROOT / (name + "-debug/build.py")).is_file()]
    selected = available if "all" in args.models else list(dict.fromkeys(args.models))
    if not selected:
        parser.error("No available Debug source targets")
    missing = set(selected) - set(available)
    if missing:
        parser.error("Source target not present: " + ", ".join(sorted(missing)))
    sdk_top = git_value("rev-parse", "--show-toplevel", directory=ROOT / "sdk")
    sdk_is_checkout = bool(sdk_top and Path(sdk_top).resolve() == (ROOT / "sdk").resolve())
    actual_sdk = git_value("rev-parse", "HEAD", directory=ROOT / "sdk") if sdk_is_checkout else None
    archive_provenance = None
    if actual_sdk != SDK_COMMIT:
        marker = ROOT / "sdk/SDK-SOURCE-COMMIT"
        provenance_file = ROOT / "SOURCE-PROVENANCE.json"
        if not marker.is_file() or not provenance_file.is_file():
            parser.error("Initialize the fixed SDK submodule: git submodule update --init sdk")
        if marker.read_text(encoding="ascii").strip() != SDK_COMMIT:
            parser.error("Source archive SDK marker does not match the pinned commit")
        archive_provenance = json.loads(provenance_file.read_text(encoding="utf-8"))
        if archive_provenance.get("sdk_commit") != SDK_COMMIT:
            parser.error("Source archive provenance has a different SDK commit")
        hashes = archive_provenance.get("files_sha256", {})
        if not hashes or not any(name.startswith("sdk/") for name in hashes):
            parser.error("Source archive does not contain an SDK hash manifest")
        for name, expected in hashes.items():
            relative = Path(name)
            target = (ROOT / relative).resolve()
            if relative.is_absolute() or not target.is_relative_to(ROOT.resolve()):
                parser.error("Invalid source archive manifest path")
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                parser.error("Source archive file hash mismatch: " + name)
    toolchain = toolchain_folder(args.toolchain)
    environment = os.environ.copy()
    environment["BDA_TOOLCHAIN_PREFIX"] = str(toolchain / "mipsel-none-elf-")
    environment["PATH"] = str(toolchain) + os.pathsep + environment.get("PATH", "")
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    artifacts = []
    validation = {}
    for name in selected:
        folder = ROOT / (name + "-debug")
        command = [sys.executable, folder / "build.py", "--toolchain", toolchain]
        firmware_flags = [("firmware_9588_" + chip, "--firmware-" + chip)
                          for chip in ("jz4740", "jz4730", "jz4720")] if name == "9588" else [
            ("firmware_jz4730", "--firmware-jz4730"), ("firmware_jz4740", "--firmware-jz4740")]
        for argument, flag in firmware_flags:
            path = getattr(args, argument)
            if path is not None:
                command += [flag, path.resolve()]
        run(command, environment)
        output = ROOT / "build" / (name + "-debug")
        built = json.loads((output / "build.json").read_text(encoding="utf-8"))
        if args.verify:
            command = [sys.executable, folder / "verify.py"]
            verification_flags = firmware_flags + ([("firmware_legacy", "--firmware-legacy")] if name == "9688" else [])
            for argument, flag in verification_flags:
                path = getattr(args, argument)
                if path is not None:
                    command += [flag, path.resolve()]
            run(command, environment)
            checked = json.loads((output / "verification.json").read_text(encoding="utf-8"))
            stats = checked["tests"]
            validation[name] = {"success": stats["success"], "run": stats["run"],
                                "passed": stats["passed"], "skipped": stats["skipped"],
                                "skipped_tests": stats.get("skipped_tests", stats["skipped"]),
                                "skipped_subtests": stats.get("skipped_subtests", 0),
                                "scope": "host/MIPS/controller models; optional ROM checks depend on supplied local firmware; physical validation is separate"}
        binary = output / built["binary"]
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        if digest != built["sha256"]:
            raise RuntimeError("Built BDA does not match its manifest")
        exact_reference = name == "9688" and digest == REFERENCE_9688_SHA
        item = {"model": name, "path": binary.relative_to(ROOT).as_posix(),
                "sha256": digest, "size": binary.stat().st_size,
                "menu_title": built["menu_title"], "hardware_verified": exact_reference,
                "hardware_verified_chips": ["JZ4740"] if exact_reference else [],
                "hardware_evidence": "docs/validation/9688-jz4740.json" if exact_reference else None}
        if exact_reference:
            item["matched_legacy_artifact_sha256"] = REFERENCE_9688_SHA
        artifacts.append(item)
    (ROOT / "build").mkdir(exist_ok=True)
    source_commit = git_value("rev-parse", "HEAD")
    if archive_provenance is not None:
        source_commit = archive_provenance.get("source_commit")
    manifest = {"schema_version": 1, "version": (ROOT / "VERSION").read_text(encoding="ascii").strip(),
                "source_commit": source_commit or "uncommitted", "sdk_commit": SDK_COMMIT,
                "models": selected, "artifacts": artifacts, "validation": validation}
    (ROOT / "build/release-build.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Release build manifest: build/release-build.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
