#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Package only declared Debug BDAs, host clients and corresponding source.

Official packaging requires a clean committed checkout and --commit. A new
uncommitted workspace may produce a visibly marked preparation package only.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
NAME = "BBK9588-9688-debug"
MANIFEST_NAME = "release-manifest.json"
SOURCE_PROVENANCE = "SOURCE-PROVENANCE.json"
SDK_MARKER = "sdk/SDK-SOURCE-COMMIT"
SHA_RE = re.compile(r"[0-9a-f]{40}")
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?")
SOURCE_ROOT_FILES = {
    ".gitattributes", ".gitignore", ".gitmodules", "README.md", "INSTALL.md",
    "LICENSE", "NOTICE", "NOTICE.md", "VERSION", "build.py", "build.ps1", "package_release.py",
    "requirements.txt", "requirements-dev.txt", "requirements-client.txt",
}
SOURCE_ROOT_DIRS = {"9588-debug", "9688-debug", "src", "docs", ".github", "scripts", "tests", "compat", "assets", "licenses"}
EXCLUDED_PARTS = {".git", "__pycache__", ".venv", "venv", ".toolchain", "node_modules", "build", "dist", "runtime", "output", "private", "系统", "应用"}
EXCLUDED_SUFFIXES = {".bda", ".dba", ".dlx", ".bin", ".elf", ".o", ".map", ".zip", ".tar", ".gz", ".7z", ".rar", ".exe", ".dll", ".pyd", ".whl", ".pyc", ".wav", ".mp3", ".flac", ".mp4", ".avi", ".pdf", ".nand", ".img", ".rom", ".raw", ".yuyv", ".rgb565", ".dct"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(directory, *arguments, optional=False):
    result = subprocess.run(["git", "-c", "safe.directory="+directory.as_posix(), "-C", str(directory), *arguments], capture_output=True)
    if result.returncode:
        if optional:
            return None
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def relative_name(value):
    require(isinstance(value, str) and value, "Empty or non-string path")
    name = value.replace("\\", "/")
    path = PurePosixPath(name)
    require(not path.is_absolute() and ":" not in name and all(x not in ("..", ".", "") for x in name.split("/")), "Unsafe relative path: "+value)
    return path.as_posix()


def project_file(value):
    name = relative_name(value)
    path = (ROOT/name).resolve()
    require(path.is_relative_to(ROOT.resolve()) and path.is_file() and not path.is_symlink(), "Missing or external project file: "+name)
    return path


def flat_name(value):
    name = relative_name(value)
    require("/" not in name and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name), "Invalid release asset filename: "+name)
    return name


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2)+"\n").encode("utf-8")


def version():
    value = (ROOT/"VERSION").read_text(encoding="utf-8").strip()
    require(VERSION_RE.fullmatch(value), "VERSION is not a supported semantic version")
    return value


def identity(expected_commit=None, *, require_checkout=True):
    raw = git(ROOT, "rev-parse", "HEAD", optional=True)
    commit = raw.decode().strip() if raw else "uncommitted"
    if expected_commit:
        require(SHA_RE.fullmatch(expected_commit.lower()), "--commit requires a full 40-digit Git commit")
        resolved = git(ROOT, "rev-parse", expected_commit+"^{commit}").decode().strip()
        require(commit == resolved, "Checkout HEAD does not match --commit")
        require(not git(ROOT, "status", "--porcelain", "--untracked-files=normal").strip(), "Official package requires a clean committed source tree")
    sdk_top_raw = git(ROOT/"sdk", "rev-parse", "--show-toplevel", optional=True)
    sdk_is_checkout = bool(sdk_top_raw and Path(sdk_top_raw.decode().strip()).resolve() == (ROOT/"sdk").resolve())
    actual_sdk_raw = git(ROOT/"sdk", "rev-parse", "HEAD", optional=True) if sdk_is_checkout else None
    actual_sdk = actual_sdk_raw.decode().strip() if actual_sdk_raw else None
    link_raw = git(ROOT, "rev-parse", "HEAD:sdk", optional=True)
    link = link_raw.decode().strip() if link_raw else None
    sdk_commit = link or actual_sdk
    require(sdk_commit and SHA_RE.fullmatch(sdk_commit), "Cannot identify the fixed SDK commit")
    if require_checkout:
        require(actual_sdk == sdk_commit, "SDK checkout differs from the committed gitlink")
    elif actual_sdk is not None:
        require(actual_sdk == sdk_commit, "Present SDK checkout differs from the gitlink")
    return commit, sdk_commit


def allowed_source(name):
    path = PurePosixPath(name)
    return not any(part in EXCLUDED_PARTS for part in path.parts) and path.suffix.lower() not in EXCLUDED_SUFFIXES and not name.endswith("~")


def tracked_source(directory, prefix=""):
    result = git(directory, "ls-files", "--stage", "-z")
    items = []
    for record in result.split(b"\0"):
        if not record:
            continue
        metadata, filename = record.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0].decode("ascii")
        name = relative_name(filename.decode("utf-8"))
        if mode == "160000":
            continue  # Nested emulator submodules are not Debug build inputs.
        require(mode != "120000", "Source archive does not follow symbolic links: "+name)
        if allowed_source(name):
            file = (directory/name).resolve()
            require(file.is_relative_to(directory.resolve()) and file.is_file(), "Tracked source is missing: "+name)
            items.append((prefix+name, file.read_bytes(), 0o755 if mode == "100755" else 0o644))
    return items


def source_files(commit, sdk_commit):
    if commit != "uncommitted":
        items = tracked_source(ROOT)
    else:
        items = []
        candidates = [ROOT/name for name in SOURCE_ROOT_FILES if (ROOT/name).is_file()]
        for directory in sorted(SOURCE_ROOT_DIRS):
            if (ROOT/directory).is_dir():
                candidates.extend((ROOT/directory).rglob("*"))
        for path in sorted(candidates):
            if not path.is_file() or path.is_symlink():
                continue
            name = path.relative_to(ROOT).as_posix()
            if allowed_source(name):
                items.append((name, path.read_bytes(), 0o644))
    # git ls-files does not recurse into the SDK; expand the exact submodule.
    items = [item for item in items if not item[0].startswith("sdk/")]
    items.extend(tracked_source(ROOT/"sdk", "sdk/"))
    items.append((SDK_MARKER, (sdk_commit+"\n").encode("ascii"), 0o644))
    names = [item[0] for item in items]
    require(len(names) == len(set(names)), "Duplicate corresponding-source entry")
    provenance = {"schema_version": 1, "version": version(), "source_commit": commit, "sdk_commit": sdk_commit,
                  "files_sha256": {name: digest(data) for name, data, _ in sorted(items)}}
    items.append((SOURCE_PROVENANCE, json_bytes(provenance), 0o644))
    return items


def write_zip(destination, items):
    names = set()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data, mode in sorted(items):
            name = relative_name(name)
            require(name not in names, "Duplicate ZIP entry: "+name)
            names.add(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, data)


def load_build(path, commit, sdk_commit):
    build = json.loads(path.read_text(encoding="utf-8"))
    require(build.get("schema_version") == 1 and build.get("version") == version(), "Build manifest schema/version mismatch")
    require(build.get("sdk_commit") == sdk_commit, "Build SDK commit mismatch")
    built_commit = build.get("source_commit")
    require(built_commit in (commit, None if commit == "uncommitted" else commit, "uncommitted" if commit == "uncommitted" else commit), "Build manifest source commit is stale; rebuild after committing")
    models = build.get("models")
    require(isinstance(models, list) and models and len(models) == len(set(models)) and all(x in ("9588", "9688") for x in models), "Manifest has no valid declared models")
    declared = build.get("artifacts")
    require(isinstance(declared, list) and declared, "Manifest declares no BDA artifacts")
    artifacts = []
    used = set()
    sys.path.insert(0, str(ROOT/"sdk"))
    from bda_packer.validate import validate_bda
    for item in declared:
        require(item.get("model") in models, "Artifact has an undeclared model")
        source = project_file(item["path"])
        name = flat_name(source.name)
        require(name.lower().endswith(".bda") and name.lower() not in used, "Duplicate or non-BDA artifact")
        used.add(name.lower())
        data = source.read_bytes()
        require(digest(data) == item.get("sha256"), "Build BDA digest mismatch: "+name)
        validated = validate_bda(source)
        require(validated.get("ok") is True, "SDK rejected BDA: "+name)
        hardware = item.get("hardware_verified", False)
        require(isinstance(hardware, bool), "hardware_verified must be a boolean")
        require(item["model"] != "9588" or hardware is False, "9588 has no physical hardware acceptance in this release")
        match = item.get("matched_legacy_artifact_sha256")
        if hardware:
            require(match == digest(data), "Hardware acceptance is not bound to this exact BDA")
        entry = {"model": item["model"], "name": name, "sha256": digest(data), "bytes": len(data),
                 "menu_title": item.get("menu_title", validated.get("title")),
                 "hardware_verified": hardware, "matched_legacy_artifact_sha256": match,
                 "hardware_verified_chips": item.get("hardware_verified_chips", []),
                 "header_checksum_valid": True, "source_path": relative_name(item["path"])}
        artifacts.append((entry, data))
    require(set(models) == {item[0]["model"] for item in artifacts}, "Declared model is missing its BDA")
    return models, artifacts


def host_files(models):
    items = []
    for name in ("README.md", "INSTALL.md", "LICENSE", "NOTICE", "NOTICE.md", "requirements.txt"):
        path = ROOT/name
        if path.is_file():
            items.append((name, path.read_bytes(), 0o644))
    for model in models:
        base = model+"-debug/"
        candidates = (base+"client.py", base+"host.py", "tools/"+model+"_debug.py")
        client = next((name for name in candidates if (ROOT/name).is_file()), None)
        require(client is not None, "No PC client for declared model "+model)
        items.append((client, project_file(client).read_bytes(), 0o644))
        for path in sorted((ROOT/base).rglob("*")):
            name = path.relative_to(ROOT).as_posix()
            local = path.relative_to(ROOT/base)
            if path.is_file() and not path.is_symlink() and allowed_source(name) and (
                    path.suffix.lower() == ".md" or local.as_posix() == "module_abi.h" or local.parts[0] == "examples"):
                items.append((name, path.read_bytes(), 0o644))
    for directory in ("docs", "licenses"):
        if (ROOT/directory).is_dir():
            for path in sorted((ROOT/directory).rglob("*")):
                name = path.relative_to(ROOT).as_posix()
                if path.is_file() and not path.is_symlink() and allowed_source(name):
                    items.append((name, path.read_bytes(), 0o644))
    return items


def package(build_path, output, expected_commit=None):
    commit, sdk_commit = identity(expected_commit)
    models, artifacts = load_build(build_path, commit, sdk_commit)
    current_version = version()
    archive_name = f"{NAME}-v{current_version}.zip"
    source_name = f"{NAME}-v{current_version}-source.zip"
    build_dir = ROOT/"build"
    build_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-stage-", dir=build_dir) as temporary:
        stage = Path(temporary).resolve()
        require(stage.is_relative_to(build_dir.resolve()), "Release staging escaped build directory")
        install_items = host_files(models)
        for item, data in artifacts:
            (stage/item["name"]).write_bytes(data)
            install_items.append((item["model"]+"/应用/程序/"+item["name"], data, 0o644))
        install_info = {"schema_version": 1, "version": current_version, "source_commit": commit, "sdk_commit": sdk_commit,
                        "models": models, "artifacts": [item for item, _ in artifacts]}
        install_items.append(("BUILD-INFO.json", json_bytes(install_info), 0o644))
        write_zip(stage/archive_name, install_items)
        write_zip(stage/source_name, source_files(commit, sdk_commit))
        notes = [f"# {NAME} v{current_version}", "", "这是预发布调试工具。", "",
                 "本次实际提供的机型："+"、".join(models)+"。未声明机型不包含 BDA。", "",
                 "将 ZIP 中对应机型的 `应用/程序/*.bda` 复制到设备的 `应用/程序`；按 README 拔线启动，再接 USB。", "",
                 "构建提交：`"+commit+"`", "固定 SDK：`"+sdk_commit+"`", "",
                 "验证分层及硬件适用范围以 README、构建清单和验收记录为准。编译/控制器模型通过不等于新增真机覆盖。", "",
                 "安装包提供 PC 客户端；EXEC 编译需要另行准备固定工具链。源码包包含展开的固定 SDK，不包含固件或私有设备数据。", ""]
        (stage/"RELEASE-NOTES.md").write_bytes("\n".join(notes).encode("utf-8"))
        assets = {path.name: {"sha256": digest(path.read_bytes()), "bytes": path.stat().st_size}
                  for path in stage.iterdir() if path.is_file()}
        release = {**install_info, "repository": "HelloClyde/"+NAME, "prerelease": True,
                   "preparation_only": commit == "uncommitted", "assets": assets}
        (stage/MANIFEST_NAME).write_bytes(json_bytes(release))
        sums = "".join(digest(path.read_bytes())+"  "+path.name+"\n" for path in sorted(stage.iterdir()) if path.is_file())
        (stage/"SHA256SUMS.txt").write_bytes(sums.encode("ascii"))
        output.mkdir(parents=True, exist_ok=True)
        staged_names = {p.name for p in stage.iterdir()}
        require(all(p.is_file() and p.name in staged_names for p in output.iterdir()), "Output contains other release assets; choose a separate output directory")
        for path in stage.iterdir():
            os.replace(path, output/path.name)
    verify(output, expected_commit=expected_commit)
    print(f"PASS package {output}; models={','.join(models)}; commit={commit}; sdk={sdk_commit}")


def verify(output, *, expected_commit=None, tag=None):
    payload = json.loads((output/MANIFEST_NAME).read_text(encoding="utf-8"))
    commit, sdk_commit = identity(expected_commit, require_checkout=False)
    require(payload.get("schema_version") == 1 and payload.get("version") == version(), "Release manifest version mismatch")
    require(payload.get("source_commit") == commit and payload.get("sdk_commit") == sdk_commit, "Release source/SDK differs from checkout")
    if expected_commit or tag:
        require(payload.get("preparation_only") is False and commit != "uncommitted", "Uncommitted preparation package cannot be published")
    if tag:
        require(tag == "v"+version(), "Tag and VERSION differ")
        pointed = git(ROOT, "rev-parse", "refs/tags/"+tag+"^{commit}").decode().strip()
        require(pointed == commit, "Release tag does not point at the packaged commit")
    assets = payload.get("assets", {})
    require(isinstance(assets, dict) and assets, "Release has no assets")
    sums = {}
    for line in (output/"SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
        matched = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        require(matched and matched[2] not in sums, "Invalid/duplicate checksum line")
        sums[matched[2]] = matched[1]
    require(set(sums) == set(assets)|{MANIFEST_NAME}, "Checksum list and release assets differ")
    require({p.name for p in output.iterdir()} == set(sums)|{"SHA256SUMS.txt"}, "Release directory has missing/extra files")
    for name, expected in sums.items():
        path = output/flat_name(name)
        require(path.is_file() and digest(path.read_bytes()) == expected, "Release digest mismatch: "+name)
        if name in assets:
            require(assets[name] == {"sha256": expected, "bytes": path.stat().st_size}, "Release manifest asset mismatch: "+name)
    source_name = f"{NAME}-v{version()}-source.zip"
    with zipfile.ZipFile(output/source_name) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Duplicate source ZIP entry")
        for name in names:
            relative_name(name)
        provenance = json.loads(archive.read(SOURCE_PROVENANCE))
        require(provenance.get("source_commit") == commit and provenance.get("sdk_commit") == sdk_commit and provenance.get("version") == version(), "Source archive provenance mismatch")
        files = provenance.get("files_sha256", {})
        require(set(names) == set(files)|{SOURCE_PROVENANCE}, "Source archive contains undeclared files")
        for name, expected in files.items():
            require(digest(archive.read(name)) == expected, "Source entry digest mismatch: "+name)
        require(archive.read(SDK_MARKER).decode("ascii").strip() == sdk_commit, "Expanded SDK marker mismatch")
    with zipfile.ZipFile(output/f"{NAME}-v{version()}.zip") as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Duplicate install ZIP entry")
        for name in names:
            relative_name(name)
        info = json.loads(archive.read("BUILD-INFO.json"))
        require(info.get("source_commit") == commit and info.get("sdk_commit") == sdk_commit and info.get("models") == payload.get("models"), "Install ZIP provenance differs")
        for item in payload["artifacts"]:
            data = (output/flat_name(item["name"])).read_bytes()
            require(digest(data) == item["sha256"] and len(data) == item["bytes"], "BDA record mismatch")
            installed = item["model"]+"/应用/程序/"+item["name"]
            require(archive.read(installed) == data, "Installed BDA differs from standalone asset")
    print(f"PASS release verification {output}; tag={tag or '(not supplied)'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT/"build/release-build.json")
    parser.add_argument("--output", type=Path, default=ROOT/"dist/release")
    parser.add_argument("--commit", help="Exact source commit for an official committed package")
    parser.add_argument("--tag", help="Verify an existing vVERSION tag")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    require(output != ROOT and output.is_relative_to(ROOT), "Release output must stay inside this project")
    if args.verify:
        verify(output, expected_commit=args.commit, tag=args.tag)
    else:
        require(args.tag is None, "Use --tag with --verify after building the release")
        package(args.manifest.resolve(), output, expected_commit=args.commit)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, KeyError, zipfile.BadZipFile) as error:
        print("ERROR:", error, file=sys.stderr)
        raise SystemExit(1)
