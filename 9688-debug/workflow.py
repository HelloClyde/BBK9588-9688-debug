# SPDX-License-Identifier: GPL-3.0-only
"""Checked management RPCs and reusable, single-connection test workflows.

No operation reconnects or retries after an unknown transport outcome. Device
CRC32 verifies transfer integrity; the SHA-256 in reports identifies host files.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import time
import uuid
import zlib

MANAGEMENT_BIT = 1 << 5
DISCOVERY_BIT = 1 << 6
ROI_BIT = 1 << 7
LOG_BIT = 1 << 8
BINDING_NAMES = ('FW_CURRENT_TCB', 'FW_FS_TABLE', 'FW_GLOBAL_ALLOC', 'FW_GLOBAL_FREE', 'FW_GUI_TABLE', 'FW_INT_NESTING', 'FW_LCD_BITS', 'FW_LCD_HEIGHT', 'FW_LCD_WIDTH', 'FW_MEM_TABLE', 'FW_NATIVE_USB_ENDPOINTS', 'FW_NATIVE_USB_INIT', 'FW_NATIVE_USB_IRQ', 'FW_OS_RUNNING', 'FW_RAW_TICK', 'FW_RES_TABLE', 'FW_SCHED_LOCK', 'FW_SYS_TABLE', 'FW_TASK_CREATE_EXT', 'FW_TASK_DELAY', 'FW_TASK_DELETE', 'FW_TASK_RESUME', 'FW_TASK_TABLE', 'FW_TICK_IRQ_HANDLER', 'FW_TICK_IRQ_SLOT', 'FW_USB_IRQ_SLOT')
FEATURES = {"files": 1, "file_crc": 2, "windows": 4, "refresh": 8, "launch": 16, "input": 32}
INFO_FIELDS = ("version", "features", "chunk_max", "path_max", "job_id", "job_state", "job_action", "progress", "total", "result", "held_target", "held_key")
JOB_FIELDS = ("version", "id", "state", "action", "progress", "total", "result", "crc32")
WINDOW_FIELDS = ("index", "handle", "parent", "child", "next", "handler", "queue_owner", "flags", "foreground", "title_length")
INPUT_KINDS = {"down": 1, "up": 2, "tap": 3, "hold": 4, "touch-down": 5, "touch-up": 6, "click": 7}
KEYS = {"esc": 0x01, "escape": 0x01, "enter": 0x1c, "up": 0x67, "left": 0x69, "right": 0x6a, "down": 0x6c}


class FeatureUnavailable(RuntimeError):
    """The current checked firmware binding does not expose this operation."""


class ManagementError(RuntimeError):
    pass


def unpack_words(data, count):
    if len(data) != count * 4:
        raise ManagementError(f"expected {count * 4} management reply bytes, got {len(data)}")
    return struct.unpack(f"<{count}I", data)


def signed(value):
    return value - 0x100000000 if value >= 0x80000000 else value


def transfer_metrics(started, payload_bytes, chunks, rpc_count):
    elapsed = time.perf_counter() - started
    return {"elapsed_seconds": elapsed, "payload_bytes_per_second": payload_bytes / elapsed if elapsed else 0,
            "chunks": chunks, "rpc_count": rpc_count, "measurement": "client-end-to-end"}


def device_path(value):
    """Canonical absolute A:/ or B:/ path, with lossless GBK encoding."""
    if not isinstance(value, str):
        raise ValueError("device path must be text")
    value = value.replace("\\", "/")
    if len(value) < 3 or value[0].upper() not in ("A", "B") or value[1:3] != ":/":
        raise ValueError("device path must be absolute A:/... or B:/...")
    value = value[0].upper() + value[1:]
    parts = value[3:].split("/")
    if value != value[:3] and (not all(parts) or any(p in (".", "..") for p in parts)):
        raise ValueError("device path has empty, . or .. components")
    if any(c in value[3:] for c in '\x00:*?"<>|') or any(ord(c) < 32 for c in value):
        raise ValueError("device path contains a reserved character")
    encoded = value.encode("gbk", errors="strict")
    if len(encoded) > 240:
        raise ValueError("device path exceeds 240 GBK bytes")
    return value


def pack_path(value):
    data = device_path(value).encode("gbk")
    return struct.pack("<H", len(data)) + data


def screen_comparison(output, baseline):
    from PIL import Image, ImageChops
    with Image.open(output) as actual, Image.open(baseline) as expected:
        actual, expected = actual.convert("RGB"), expected.convert("RGB")
        if actual.size != expected.size:
            raise ValueError("baseline dimensions differ from the captured image")
        diff = ImageChops.difference(actual, expected)
        mask = diff.point(lambda v: 255 if v else 0).convert("RGB")
        red, green, blue = mask.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        changed = mask.histogram()[255]
        return {"baseline": str(baseline), "baseline_sha256": hashlib.sha256(Path(baseline).read_bytes()).hexdigest(),
                "changed_pixels": changed, "total_pixels": actual.width * actual.height,
                "changed_bbox": mask.getbbox(), "identical": changed == 0}


def executable_sections(path):
    """Read ELF32/LSB MIPS executable sections without a compiler dependency."""
    data = Path(path).read_bytes()
    if len(data) < 52 or data[:6] != b"\x7fELF\x01\x01" or struct.unpack_from("<H", data, 18)[0] != 8:
        raise ValueError("running-image verification requires an ELF32 little-endian MIPS file")
    section_offset = struct.unpack_from("<I", data, 32)[0]
    section_size, count = struct.unpack_from("<HH", data, 46)
    if section_size != 40 or not 1 <= count <= 4096 or section_offset > len(data) or count * 40 > len(data) - section_offset:
        raise ValueError("invalid ELF section table")
    result = []
    for index in range(count):
        fields = struct.unpack_from("<10I", data, section_offset + index * 40)
        _, kind, flags, address, offset, size, *_ = fields
        if kind != 1 or flags & 6 != 6 or not size:
            continue
        if offset > len(data) or size > len(data) - offset or not 0x80000000 <= address < 0x82000000 or size > 0x82000000 - address:
            raise ValueError("ELF executable section is outside the supported native image window")
        result.append((address, data[offset:offset + size]))
    if not result:
        raise ValueError("ELF contains no loaded executable section")
    return result


class WorkflowClient:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def management_info(self, *, refresh=False):
        if self.caps is None or not self.caps[1] & MANAGEMENT_BIT:
            raise FeatureUnavailable("this BDA has no management protocol; install a build with management support")
        if refresh or getattr(self, "_management", None) is None:
            info = dict(zip(INFO_FIELDS, unpack_words(self.rpc(40), 12)))
            if info["version"] != 1 or not 1 <= info["chunk_max"] <= 480 or info["path_max"] != 240:
                raise ManagementError("unsupported management protocol or limits")
            info["supported"] = [name for name, bit in FEATURES.items() if info["features"] & bit]
            info["result"] = signed(info["result"])
            self._management = info
        return dict(self._management)

    def require_feature(self, name):
        if not self.management_info()["features"] & FEATURES[name]:
            raise FeatureUnavailable(f"{name} is unavailable for this firmware binding; inspect firmware and management info")

    def management_call(self, action, payload=b"", *, feature=None):
        if feature:
            self.require_feature(feature)
        else:
            self.management_info()
        if len(payload) > 508:
            raise ValueError("management request exceeds the frame capacity")
        return self.rpc(41, struct.pack("<I", action) + payload)

    def firmware_info(self):
        if self.caps is None or not self.caps[1] & DISCOVERY_BIT:
            raise FeatureUnavailable("this BDA has no startup discovery diagnostics")
        data = self.rpc(47)
        if len(data) < 36 or (len(data) - 36) % 8:
            raise ManagementError("invalid firmware discovery diagnostics")
        values = struct.unpack(f"<{len(data) // 4}I", data)
        names = ("version", "mode", "soc", "binding_id", "error", "binding_count", "scanned_words", "matched_functions", "reference_count")
        result = dict(zip(names, values[:9]))
        returned = (len(values) - 9) // 2
        if result["version"] != 1 or result["mode"] > 2 or returned > result["binding_count"]:
            raise ManagementError("unsupported firmware diagnostics or binding count")
        result["mode_name"] = ("rejected", "exact-profile", "startup-scan")[result["mode"]]
        result["bindings"] = [{"index": values[i], "address": values[i + 1], "address_hex": f"0x{values[i + 1]:08x}", "name": BINDING_NAMES[values[i]] if values[5] == len(BINDING_NAMES) and values[i] < len(BINDING_NAMES) else None} for i in range(9, len(values), 2)]
        result["returned_bindings"] = returned
        result["truncated"] = returned < result["binding_count"]
        return result

    def logs(self, cursor=0):
        if self.caps is None or not self.caps[1] & LOG_BIT:
            raise FeatureUnavailable("this BDA has no resident event log")
        if not isinstance(cursor, int) or not 0 <= cursor <= 0xffffffff:
            raise ValueError("log cursor must be uint32")
        entries, truncated, oldest = [], False, None
        for _ in range(4):
            data = self.rpc(48, struct.pack("<I", cursor))
            if len(data) < 16:
                raise ManagementError("short event log reply")
            version, following, available, count = unpack_words(data[:16], 4)
            if version != 1 or count > 31 or len(data) != 16 + count * 16:
                raise ManagementError("invalid event log reply")
            if oldest is None:
                oldest = available
                truncated = bool(available and cursor < available - 1)
            batch = [dict(zip(("sequence", "tick", "event", "detail"), unpack_words(data[16+i*16:32+i*16], 4))) for i in range(count)]
            if count and (following != batch[-1]["sequence"] or any(entry["sequence"] <= cursor for entry in batch)):
                raise ManagementError("event log sequence mismatch")
            entries.extend(batch)
            if not count or following == cursor:
                break
            cursor = following
        return {"next_cursor": cursor, "oldest_available": oldest, "truncated": truncated, "entries": entries,
                "event_names": {1: "resident-init", 2: "command-or-error", 3: "session-change", 4: "stop"}}

    def job_status(self, job_id=None):
        self.management_info()
        payload = b"" if job_id is None else struct.pack("<I", job_id)
        result = dict(zip(JOB_FIELDS, unpack_words(self.rpc(42, payload), 8)))
        if result["version"] != 1 or result["state"] > 5 or (job_id is not None and result["id"] != job_id):
            raise ManagementError("job identity or state mismatch")
        result["result"] = signed(result["result"])
        return result

    def job_result(self, job_id):
        self.management_info()
        values = unpack_words(self.rpc(43, struct.pack("<I", job_id)), 5)
        if values[0] != job_id or values[1] not in (3, 4, 5):
            raise ManagementError("job result is not complete or belongs to another job")
        return dict(zip(("id", "state", "result", "crc32", "bytes"), (values[0], values[1], signed(values[2]), values[3], values[4])))

    def job_cancel(self, job_id):
        self.management_info()
        self.rpc(44, struct.pack("<I", job_id))
        return {"cancel_requested": job_id}

    def wait_job(self, job_id, *, wait_seconds=30, progress=None):
        if not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("wait-seconds must be finite and positive")
        deadline = time.monotonic() + wait_seconds
        last = None
        while True:
            state = self.job_status(job_id)
            if progress and state != last:
                progress(dict(state))
            last = state
            if state["state"] in (3, 4, 5):
                result = self.job_result(job_id)
                if result["state"] != 3 or result["result"]:
                    raise ManagementError(f"device job failed: {result}")
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError(f"job {job_id} is still pending; inspect it in this session; no operation was retried")
            time.sleep(0.02)

    def fs_stat(self, path):
        path = device_path(path)
        reply = self.management_call(1, pack_path(path), feature="files")
        values = unpack_words(reply, 5)
        return dict(zip(("path", "size", "attributes", "time0", "time1", "time2"), (path, *values)))

    def fs_exists(self, path):
        try:
            return self.fs_stat(path)
        except Exception as exc:
            if getattr(exc, "opcode", None) == 41 and getattr(exc, "status", None) == 4:
                return None
            raise

    def fs_io_state(self):
        reply = self.management_call(8, feature="files")
        if len(reply) < 32:
            raise ManagementError("short I/O recovery state")
        values = unpack_words(reply[:32], 8)
        if len(reply) != 32 + values[7]:
            raise ManagementError("invalid I/O recovery path length")
        result = dict(zip(("reader_token", "reader_size", "writer_token", "writer_size", "writer_offset", "listing_token", "job_id", "writer_path_length"), values))
        result["writer_path"] = reply[32:].decode("gbk", errors="strict")
        return result

    def fs_abort(self, token):
        self.management_call(23, struct.pack("<I", token), feature="files")
        return {"aborted_writer": token, "partial_file_retained": True}

    def fs_list(self, path):
        path = device_path(path)
        token, = unpack_words(self.management_call(2, pack_path(path), feature="files"), 1)
        entries = []
        try:
            while True:
                reply = self.management_call(3, struct.pack("<I", token))
                if len(reply) < 16:
                    raise ManagementError("short directory entry")
                received, size, attr, length = unpack_words(reply[:16], 4)
                if received != token or len(reply) != 16 + length:
                    raise ManagementError("invalid directory cursor or name length")
                if not length:
                    break
                name = reply[16:].decode("gbk", errors="strict")
                if name in (".", ".."):
                    continue
                if "/" in name or "\\" in name or "\x00" in name:
                    raise ManagementError("directory entry contains a path separator")
                full = device_path(path.rstrip("/") + "/" + name)
                entries.append({"name": name, "path": full, "size": size, "attributes": attr, "directory": bool(attr & 0x4000)})
                if len(entries) > 65535:
                    raise ManagementError("directory enumeration exceeded its bounded limit")
        finally:
            if not self.broken:
                self.management_call(4, struct.pack("<I", token))
        return entries

    def fs_mkdir(self, path):
        self.management_call(5, pack_path(path), feature="files")
        return {"created": device_path(path)}

    def fs_remove(self, path):
        self.management_call(6, pack_path(path), feature="files")
        return {"removed": device_path(path)}

    def fs_rename(self, old, new):
        old, new = device_path(old), device_path(new)
        if old[0] != new[0]:
            raise ValueError("rename requires the same device volume")
        self.management_call(7, pack_path(old) + pack_path(new), feature="files")
        return {"old": old, "new": new}

    def fs_crc(self, path, *, wait_seconds=30, progress=None):
        job_id, = unpack_words(self.management_call(30, pack_path(path), feature="file_crc"), 1)
        return self.wait_job(job_id, wait_seconds=wait_seconds, progress=progress)

    def fs_download(self, path, output, *, wait_seconds=60):
        started, initial_rpc = time.perf_counter(), getattr(self, "rpc_count", 0)
        path, output = device_path(path), Path(output)
        self.require_feature("file_crc")
        token, size = unpack_words(self.management_call(10, pack_path(path), feature="files"), 2)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".part-" + uuid.uuid4().hex[:12])
        digest, crc, offset = hashlib.sha256(), 0, 0
        try:
            with temporary.open("xb") as stream:
                while offset < size:
                    count = min(self.management_info()["chunk_max"], size - offset)
                    reply = self.management_call(11, struct.pack("<III", token, offset, count))
                    if len(reply) != 12 + count or unpack_words(reply[:12], 3) != (token, offset, size):
                        raise ManagementError("download token, offset, size or chunk length mismatch")
                    stream.write(reply[12:]);digest.update(reply[12:]);crc=zlib.crc32(reply[12:],crc);offset+=count
        finally:
            if not self.broken:
                self.management_call(12, struct.pack("<I", token))
        verification = self.fs_crc(path, wait_seconds=wait_seconds)
        if verification["bytes"] != size or verification["crc32"] != crc:
            raise ManagementError(f"download changed or CRC mismatch; partial copy retained at {temporary}")
        temporary.replace(output)
        result = {"path": path, "output": str(output), "bytes": size, "crc32": crc, "sha256": digest.hexdigest()}
        result.update(transfer_metrics(started, size, (size + self.management_info()["chunk_max"] - 1) // self.management_info()["chunk_max"], getattr(self, "rpc_count", 0) - initial_rpc))
        return result

    def fs_upload(self, source, path, *, wait_seconds=60, progress=None):
        started, initial_rpc = time.perf_counter(), getattr(self, "rpc_count", 0)
        source, path = Path(source), device_path(path)
        data = source.read_bytes()
        if len(data) > 0x7fffffff:
            raise ValueError("file exceeds the signed native seek range")
        crc = zlib.crc32(data)
        self.require_feature("file_crc")
        token, = unpack_words(self.management_call(20, struct.pack("<II", len(data), crc) + pack_path(path), feature="files"), 1)
        sealed = False
        try:
            chunk = self.management_info()["chunk_max"]
            for offset in range(0, len(data), chunk):
                part = data[offset:offset+chunk]
                following, = unpack_words(self.management_call(21, struct.pack("<II", token, offset) + part), 1)
                if following != offset + len(part):
                    raise ManagementError("upload acknowledged an unexpected offset; no chunk was retried")
                if progress:
                    progress({"stage": "upload", "progress": following, "total": len(data)})
            job_id, = unpack_words(self.management_call(22, struct.pack("<I", token)), 1)
            sealed = True
            result = self.wait_job(job_id, wait_seconds=wait_seconds, progress=progress)
            if result["bytes"] != len(data) or result["crc32"] != crc:
                raise ManagementError("device readback CRC or size mismatch; target was not replaced")
        except BaseException:
            if not self.broken and not sealed:
                try:
                    self.management_call(23, struct.pack("<I", token))
                except Exception:
                    pass
            raise
        response = {"path": path, "source": str(source), "bytes": len(data), "crc32": crc,
                "sha256": hashlib.sha256(data).hexdigest(), "verification": "device-readback-crc32", "job": result}
        response.update(transfer_metrics(started, len(data), (len(data) + chunk - 1) // chunk, getattr(self, "rpc_count", 0) - initial_rpc))
        return response

    def deploy(self, files, *, wait_seconds=60, progress=None):
        """Stage and verify every file before renaming any live target.

        Each rename is native and same-volume; the multi-file transaction is
        journaled and rolled back on a known failure. It is not power-loss
        atomic. An unknown wire outcome retains all paths for inspection.
        """
        self.require_feature("files");self.require_feature("file_crc")
        transaction = uuid.uuid4().hex[:12]
        journal, seen = [], set()
        for index, item in enumerate(files):
            if index >= 65536:
                raise ValueError("deployment bundle exceeds the transaction file limit")
            source, target = Path(item["source"]), device_path(item["target"])
            if not source.is_file():
                raise ValueError(f"source file does not exist: {source}")
            identity = target.casefold()
            if identity in seen:
                raise ValueError("bundle contains duplicate device targets")
            seen.add(identity)
            parent = target.rsplit("/", 1)[0]
            short_name = "D" + transaction[:3].upper() + f"{index:04X}"
            temporary, backup = device_path(parent + "/" + short_name + ".TMP"), device_path(parent + "/" + short_name + ".BAK")
            journal.append({"source": str(source), "target": target, "temporary": temporary, "backup": backup,
                            "backed_up": False, "installed": False, "skipped": False})
        if not journal:
            raise ValueError("deployment requires at least one file")
        self.last_deployment = {"transaction": transaction, "files": journal, "state": "staging"}
        try:
            for entry in journal:
                existing = self.fs_exists(entry["target"])
                entry["had_target"] = existing is not None
                if existing and existing["attributes"] & 0x4000:
                    raise ValueError("deployment target is a directory")
                data = Path(entry["source"]).read_bytes()
                entry.update({"bytes": len(data), "crc32": zlib.crc32(data), "sha256": hashlib.sha256(data).hexdigest()})
                if existing and existing["size"] == len(data):
                    current = self.fs_crc(entry["target"], wait_seconds=wait_seconds)
                    if current["bytes"] == len(data) and current["crc32"] == entry["crc32"]:
                        entry["skipped"] = True
                        continue
                if self.fs_exists(entry["temporary"]) or self.fs_exists(entry["backup"]):
                    raise ManagementError("deployment staging paths already exist")
                entry["upload"] = self.fs_upload(entry["source"], entry["temporary"], wait_seconds=wait_seconds, progress=progress)
                if any(entry["upload"][key] != entry[key] for key in ("bytes", "crc32", "sha256")):
                    raise ManagementError("local source changed during staging; live files were not replaced")
            self.last_deployment["state"] = "committing"
            for entry in journal:
                if entry["skipped"]:
                    continue
                if entry["had_target"]:
                    self.fs_rename(entry["target"], entry["backup"]);entry["backed_up"] = True
                self.fs_rename(entry["temporary"], entry["target"]);entry["installed"] = True
            self.last_deployment["state"] = "committed"
            self.last_deployment["note"] = "backups retained; native same-volume renames; the bundle is not power-loss atomic"
            return self.last_deployment
        except BaseException:
            if self.broken:
                self.last_deployment["state"] = "outcome-unknown"
            else:
                failures = []
                for entry in reversed(journal):
                    try:
                        if entry["installed"]:
                            self.fs_rename(entry["target"], entry["temporary"]);entry["installed"] = False
                        if entry["backed_up"]:
                            self.fs_rename(entry["backup"], entry["target"]);entry["backed_up"] = False
                    except Exception as exc:
                        failures.append({"path": entry["target"], "error": str(exc)})
                        if self.broken:
                            break
                self.last_deployment["state"] = "outcome-unknown" if self.broken else "rollback-failed" if failures else "rolled-back"
                if failures:
                    self.last_deployment["rollback_errors"] = failures
            raise

    def apps_list(self, directories=None):
        entries = []
        for directory in directories or ("A:/应用/程序", "B:/应用/程序"):
            try:
                entries += [entry for entry in self.fs_list(directory) if not entry["directory"] and entry["name"].lower().endswith(".bda")]
            except Exception as exc:
                if getattr(exc, "opcode", None) == 41 and getattr(exc, "status", None) == 4:
                    continue
                raise
        return {"installed_files": entries, "source": "filesystem", "desktop_registration": "inspect refresh result separately"}

    def windows(self):
        self.require_feature("windows")
        entries = []
        for index in range(128):
            reply = self.management_call(50, struct.pack("<I", index))
            if len(reply) < 40:
                raise ManagementError("short window diagnostics")
            values = unpack_words(reply[:40], 10)
            if values[0] != index or len(reply) != 40 + values[9]:
                raise ManagementError("window index or title length mismatch")
            if not values[1]:
                break
            entry = dict(zip(WINDOW_FIELDS, values))
            entry["title"] = reply[40:].decode("gbk", errors="strict")
            entries.append(entry)
        else:
            raise ManagementError("window tree exceeds the diagnostic limit")
        return entries

    def targets(self):
        values = unpack_words(self.management_call(55, feature="windows"), 3)
        return dict(zip(("foreground", "desktop", "desktop_handler"), values))

    def resolve_target(self, target, *, for_input=False):
        if isinstance(target, int) and not isinstance(target, bool):
            if not 0 < target <= 0xffffffff:
                raise ValueError("target must be a nonzero uint32 handle")
            self._last_target_resolution = {"requested": target, "handle": target, "source": "explicit-handle"}
            if for_input:
                active = [entry for entry in self.windows() if entry["handle"] == target and entry["foreground"] == target]
                if len(active) != 1:
                    raise ManagementError("the explicit input target is not the current foreground window")
                self._last_target_resolution.update({name: active[0][name] for name in ("handler", "queue_owner")})
            return target
        if target == "desktop":
            candidates = self.targets()
            if not candidates["desktop"] or candidates["foreground"] != candidates["desktop"]:
                raise ManagementError("the authenticated native desktop is not in the foreground; return to the desktop first")
            self._last_target_resolution = {"requested": target, "handle": candidates["desktop"],
                                            "handler": candidates["desktop_handler"], "source": "firmware-checked-desktop"}
            if for_input:
                active = [entry for entry in self.windows() if entry["handle"] == candidates["desktop"] and entry["foreground"] == candidates["desktop"]]
                if len(active) != 1 or active[0]["handler"] != candidates["desktop_handler"]:
                    raise ManagementError("authenticated desktop changed before input")
                self._last_target_resolution["queue_owner"] = active[0]["queue_owner"]
            return candidates["desktop"]
        if target not in ("$launched", "launched", "foreground"):
            raise ValueError("target must be a handle, desktop, $launched or foreground")
        remembered = getattr(self, "_launched_target", None)
        if not remembered or not remembered.get("image_elf"):
            raise ManagementError("automatic app input requires a launch in this session with image_elf; an arbitrary foreground window is not selected")
        active = [entry for entry in self.windows() if entry["handle"] == entry["foreground"]]
        if len(active) != 1 or any(active[0][name] != remembered[name] for name in ("handle", "handler", "queue_owner")):
            raise ManagementError("foreground handle, handler or queue owner changed after launch; input was not sent")
        evidence = self.verify_running_image(remembered["image_elf"], active) if for_input else remembered["image_verification"]
        self._last_target_resolution = {"requested": target, "handle": remembered["handle"], "handler": remembered["handler"],
                                        "queue_owner": remembered["queue_owner"], "source": "verified-launch-in-this-session", "image_verification": evidence}
        return remembered["handle"]

    def apps_refresh(self, target, *, wait_seconds=30):
        target = self.resolve_target(target)
        job_id, = unpack_words(self.management_call(51, struct.pack("<I", target), feature="refresh"), 1)
        result = self.wait_job(job_id, wait_seconds=wait_seconds)
        return {"target": target, "resolved_target": self._last_target_resolution, "job": result, "windows": self.windows()}

    def verify_running_image(self, elf, windows=None):
        sections = executable_sections(elf)
        results = []
        for address, expected in sections:
            actual = self.read(address, len(expected))
            expected_hash, actual_hash = hashlib.sha256(expected).hexdigest(), hashlib.sha256(actual).hexdigest()
            results.append({"address": address, "bytes": len(expected), "expected_sha256": expected_hash,
                            "actual_sha256": actual_hash, "matches": expected_hash == actual_hash})
        windows = self.windows() if windows is None else windows
        active = [entry for entry in windows if entry["handle"] == entry["foreground"]]
        owner_matches = bool(active) and all(any(address <= entry["handler"] < address + len(data) for address, data in sections) for entry in active)
        result = {"elf": str(elf), "elf_sha256": hashlib.sha256(Path(elf).read_bytes()).hexdigest(),
                  "sections": results, "foreground_handler_in_image": owner_matches,
                  "verified": all(section["matches"] for section in results) and owner_matches}
        if not result["verified"]:
            raise ManagementError("running program does not match the supplied ELF or foreground handler; " + json.dumps(result))
        return result

    def apps_launch(self, target, path, *, wait_seconds=30, image_elf=None):
        if image_elf is not None:
            executable_sections(image_elf)  # Fail a missing/invalid proof file before launching.
        target = self.resolve_target(target)
        resolution = dict(self._last_target_resolution)
        path = device_path(path)
        checksum = self.fs_crc(path, wait_seconds=wait_seconds)
        before = self.windows()
        job_id, = unpack_words(self.management_call(52, struct.pack("<I", target) + pack_path(path), feature="launch"), 1)
        result = self.wait_job(job_id, wait_seconds=wait_seconds)
        after = self.windows()
        response = {"path": path, "resolved_target": resolution, "file_crc32": checksum["crc32"], "file_bytes": checksum["bytes"], "job": result,
                "windows_before": before, "windows_after": after,
                "loaded_version_verified": False,
                "note": "file CRC identifies deployed bytes; window owner/handler must be checked before input; no running-image hash is inferred"}
        if image_elf is not None:
            response["image_verification"] = self.verify_running_image(image_elf, after)
            response["loaded_version_verified"] = True
            response["note"] = "all executable ELF sections match RAM and the foreground handler belongs to that image; this is not a normal-loader acceptance test"
            active = [entry for entry in after if entry["handle"] == entry["foreground"]]
            if len(active) != 1:
                raise ManagementError("launch did not identify one foreground window")
            self._launched_target = {**{name: active[0][name] for name in ("handle", "handler", "queue_owner")},
                                     "image_elf": str(image_elf), "image_verification": response["image_verification"]}
        else:
            self._launched_target = None
        return response

    def input_event(self, target, kind, *, code=0, argument=0, duration_ticks=1):
        if kind not in INPUT_KINDS:
            raise ValueError("input requires a known kind")
        if not 0 <= code <= 0xffffffff or not 0 <= argument <= 0xffffffff or not 1 <= duration_ticks <= 400:
            raise ValueError("invalid input value or hold duration")
        self.require_feature("input")
        target = self.resolve_target(target, for_input=True)
        expected_handler, expected_queue_owner = (self._last_target_resolution[name] for name in ("handler", "queue_owner"))
        # The native worker checks this identity again while posting, closing
        # the window-reuse race between the diagnostic RPC and input RPC.
        self.management_call(53, struct.pack("<7I", target, INPUT_KINDS[kind], code, argument, duration_ticks, expected_handler, expected_queue_owner), feature="input")
        self._held_input = kind not in ("up", "touch-up")
        if kind in ("tap", "hold", "click"):
            deadline = time.monotonic() + max(10, duration_ticks * 0.1 + 2)
            while self.management_info(refresh=True)["held_target"]:
                if time.monotonic() >= deadline:
                    raise TimeoutError("injected input has not released; inspect management info or release-input in this session")
                time.sleep(0.02)
            self._held_input = False
        return {"target": target, "resolved_target": self._last_target_resolution, "kind": kind, "code": code, "argument": argument, "duration_ticks": duration_ticks, "accepted": True}

    def release_input(self):
        if not getattr(self, "_held_input", False):
            return {"released": False}
        if self.broken:
            return {"released": False, "note": "transport lost; device disconnect/lease cleanup releases injected input"}
        self.management_call(54, feature="input")
        self._held_input = False
        return {"released": True}

    def run_steps(self, steps, *, report_directory=None, progress=None):
        """Run an explicit plan over this connection and save failure evidence."""
        directory = Path(report_directory) if report_directory is not None else None
        if directory:
            directory.mkdir(parents=True, exist_ok=True)
        report = {"schema_version": 1, "target": getattr(self, "target", None), "chip": self.chip,
                  "caps": self.caps, "started_unix": time.time(), "stages": [], "ok": False}
        try:
            report["device"] = self.status()
            if self.caps and self.caps[1] & DISCOVERY_BIT:
                report["firmware"] = self.firmware_info()
            if self.caps and self.caps[1] & LOG_BIT:
                report["logs_before"] = self.logs()
            for index, step in enumerate(steps):
                if not isinstance(step, dict) or "action" not in step:
                    raise ValueError("each workflow step must be an object with action")
                stage = {"index": index, "request": step, "started_unix": time.time()}
                report["stages"].append(stage)
                start = time.monotonic()
                try:
                    stage["result"] = self.workflow_step(step, directory=directory)
                    stage["ok"] = True
                except BaseException as exc:
                    stage.update({"ok": False, "error": str(exc), "error_type": type(exc).__name__, "outcome_unknown": bool(self.broken)})
                    raise
                finally:
                    stage["elapsed_seconds"] = time.monotonic() - start
                    if progress:
                        progress(stage)
            report["ok"] = True
        except Exception as exc:
            report.update({"error": str(exc), "error_type": type(exc).__name__, "session_lost": bool(self.broken)})
            if hasattr(self, "last_deployment"):
                report["deployment"] = self.last_deployment
            if not self.broken:
                evidence = {}
                for name, operation in (("status", self.status), ("windows", self.windows)):
                    try:
                        evidence[name] = operation()
                    except Exception as diagnostic:
                        evidence[name] = {"unavailable": str(diagnostic)}
                if directory and not self.broken:
                    try:
                        evidence["screenshot"] = self.screenshot(directory / "failure.png")
                    except Exception as diagnostic:
                        evidence["screenshot"] = {"unavailable": str(diagnostic)}
                report["failure_evidence"] = evidence
        finally:
            try:
                report["input_cleanup"] = self.release_input()
            except Exception as exc:
                report["input_cleanup"] = {"error": str(exc)}
                report["ok"] = False
            if not self.broken and self.caps and self.caps[1] & LOG_BIT:
                try:
                    report["logs_after"] = self.logs()
                except Exception as exc:
                    report["logs_after"] = {"unavailable": str(exc)}
            report["finished_unix"] = time.time()
            if directory:
                (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        return report

    def workflow_step(self, step, *, directory=None):
        action = step["action"]
        if action == "status":
            return self.status()
        if action == "firmware":
            return self.firmware_info()
        if action == "management":
            return self.management_info(refresh=True)
        if action == "logs":
            return self.logs(step.get("cursor", 0))
        if action == "deploy":
            return self.deploy(step["files"], wait_seconds=step.get("wait_seconds", 60))
        if action == "apps-list":
            return self.apps_list(step.get("directories"))
        if action == "refresh":
            return self.apps_refresh(step["target"], wait_seconds=step.get("wait_seconds", 30))
        if action == "launch":
            return self.apps_launch(step["target"], step["path"], wait_seconds=step.get("wait_seconds", 30), image_elf=step.get("image_elf"))
        if action == "verify-image":
            return self.verify_running_image(step["image_elf"])
        if action == "windows":
            return self.windows()
        if action == "input":
            return self.input_event(step["target"], step["kind"], code=key_code(step["key"]) if "key" in step else step.get("code", 0), argument=step.get("argument", 0), duration_ticks=step.get("duration_ticks", 1))
        if action == "release-input":
            return self.release_input()
        if action == "job-status":
            return self.job_status(step.get("id"))
        if action == "job-wait":
            return self.wait_job(step["id"], wait_seconds=step.get("wait_seconds", 30))
        if action == "job-cancel":
            return self.job_cancel(step["id"])
        if action == "file-crc":
            return self.fs_crc(step["path"])
        if action == "file-list":
            return self.fs_list(step["path"])
        if action == "file-io":
            return self.fs_io_state()
        if action == "file-abort":
            return self.fs_abort(step["token"])
        if action == "download":
            return self.fs_download(step["path"], step["output"], wait_seconds=step.get("wait_seconds",60))
        if action == "upload":
            return self.fs_upload(step["source"], step["path"], wait_seconds=step.get("wait_seconds",60))
        if action == "screenshot":
            path = Path(step.get("output", "screen.png"))
            if directory and not path.is_absolute():
                path = directory / path
            options = {}
            if "roi" in step:
                options["region"] = tuple(step["roi"])
            if "rotation_degrees" in step:
                options["rotation_degrees"] = step["rotation_degrees"]
            result = self.screenshot(path, **options)
            if "baseline" in step:
                result["comparison"] = screen_comparison(path, Path(step["baseline"]))
            return result
        if action == "exec-status":
            return self.exec_status()
        raise ValueError(f"unknown workflow action: {action}")


def number(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def key_code(value):
    return KEYS[value.lower()] if value.lower() in KEYS else number(value)


def target_value(value):
    if value in ("desktop", "$launched", "launched", "foreground"):
        return value
    return number(value)


def roi_value(value):
    try:
        result = tuple(int(v, 0) for v in value.split(","))
    except ValueError as exc:
        raise ValueError("region must be x,y,width,height") from exc
    if len(result) != 4:
        raise ValueError("region must be x,y,width,height")
    return result


def add_arguments(commands):
    for name in ("firmware", "management", "windows", "input-release"):
        commands.add_parser(name)
    p=commands.add_parser("logs");p.add_argument("--cursor", type=number, default=0)
    p=commands.add_parser("fs");sub=p.add_subparsers(dest="action", required=True)
    for name in ("list", "stat", "crc", "mkdir", "remove"):
        p=sub.add_parser(name);p.add_argument("path")
        if name=="crc":p.add_argument("--wait-seconds",type=float,default=60)
    sub.add_parser("io")
    p=sub.add_parser("abort");p.add_argument("token",type=number)
    p=sub.add_parser("get");p.add_argument("path");p.add_argument("output", type=Path);p.add_argument("--wait-seconds",type=float,default=60)
    p=sub.add_parser("put");p.add_argument("source",type=Path);p.add_argument("path");p.add_argument("--wait-seconds",type=float,default=60)
    p=sub.add_parser("rename");p.add_argument("old");p.add_argument("new")
    p=commands.add_parser("deploy");p.add_argument("source",type=Path,nargs="?");p.add_argument("target",nargs="?");p.add_argument("--bundle",type=Path);p.add_argument("--wait-seconds",type=float,default=60)
    p=commands.add_parser("apps");sub=p.add_subparsers(dest="action",required=True)
    p=sub.add_parser("list");p.add_argument("--directory",action="append",dest="directories")
    p=sub.add_parser("refresh");p.add_argument("--target",type=target_value,default="desktop");p.add_argument("--wait-seconds",type=float,default=30)
    p=sub.add_parser("launch");p.add_argument("path");p.add_argument("--target",type=target_value,default="desktop");p.add_argument("--image-elf",type=Path);p.add_argument("--wait-seconds",type=float,default=30)
    p=commands.add_parser("key");p.add_argument("key",type=key_code);p.add_argument("--target",type=target_value,required=True);p.add_argument("--action",choices=("down","up","tap","hold"),default="tap");p.add_argument("--ticks",type=int,default=1)
    p=commands.add_parser("click");p.add_argument("x",type=int);p.add_argument("y",type=int);p.add_argument("--target",type=target_value,required=True);p.add_argument("--ticks",type=int,default=1)
    p=commands.add_parser("touch");p.add_argument("x",type=int);p.add_argument("y",type=int);p.add_argument("--target",type=target_value,required=True);p.add_argument("--action",choices=("down","up"),default="down")
    p=commands.add_parser("jobs");sub=p.add_subparsers(dest="action",required=True)
    p=sub.add_parser("status");p.add_argument("id",type=number,nargs="?")
    p=sub.add_parser("wait");p.add_argument("id",type=number);p.add_argument("--wait-seconds",type=float,default=30)
    p=sub.add_parser("cancel");p.add_argument("id",type=number)
    p=commands.add_parser("session");p.add_argument("plan",type=Path,nargs="?",help="JSON or JSONL plan; omit for live JSONL on stdin");p.add_argument("--directory",type=Path)
    p=commands.add_parser("run-test");p.add_argument("manifest",type=Path);p.add_argument("--directory",type=Path,required=True)


COMMANDS = {"firmware","management","windows","input-release","logs","fs","deploy","apps","key","click","touch","jobs","session","run-test"}


def _relative_files(files, base):
    result = []
    for entry in files:
        item = dict(entry)
        source = Path(item["source"])
        item["source"] = str(source if source.is_absolute() else base / source)
        result.append(item)
    return result


def resolve_step_paths(step, base):
    step = dict(step)
    if "files" in step:
        step["files"] = _relative_files(step["files"], base)
    if step.get("action")=="download" and "output" in step:
        output=Path(step["output"]);step["output"]=str(output if output.is_absolute() else base/output)
    for name in ("source", "image_elf", "baseline"):
        if name in step:
            path = Path(step[name]);step[name] = str(path if path.is_absolute() else base / path)
    for name in ("target", "code", "argument", "id"):
        if name in step:
            step[name] = target_value(step[name]) if name == "target" else number(step[name])
    return step


def read_steps(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        document = json.loads(text)
        steps = document["steps"] if isinstance(document, dict) else document
    except json.JSONDecodeError:
        steps = [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(steps, list):
        raise ValueError("plan must be a JSON step array, an object with steps, or JSONL")
    return [resolve_step_paths(step, Path(path).resolve().parent) for step in steps]


def run_command(client, ns):
    command = ns.command
    if command == "firmware":return client.firmware_info()
    if command == "management":return client.management_info(refresh=True)
    if command == "logs":return client.logs(ns.cursor)
    if command == "windows":return client.windows()
    if command == "input-release":
        # Explicit release also works after reconnecting, without local state.
        client.management_call(54,feature="input");client._held_input=False;return {"released":True}
    if command == "fs":
        if ns.action == "io":return client.fs_io_state()
        if ns.action == "abort":return client.fs_abort(ns.token)
        if ns.action == "list":return client.fs_list(ns.path)
        if ns.action == "stat":return client.fs_stat(ns.path)
        if ns.action == "crc":return client.fs_crc(ns.path,wait_seconds=ns.wait_seconds)
        if ns.action == "mkdir":return client.fs_mkdir(ns.path)
        if ns.action == "remove":return client.fs_remove(ns.path)
        if ns.action == "get":return client.fs_download(ns.path,ns.output,wait_seconds=ns.wait_seconds)
        if ns.action == "put":return client.fs_upload(ns.source,ns.path,wait_seconds=ns.wait_seconds)
        return client.fs_rename(ns.old,ns.new)
    if command == "deploy":
        if ns.bundle:
            if ns.source or ns.target:raise ValueError("use a source and target or --bundle, not both")
            document=json.loads(ns.bundle.read_text(encoding="utf-8-sig"))
            files=_relative_files(document["files"],ns.bundle.resolve().parent)
        else:
            if ns.source is None or ns.target is None:raise ValueError("deployment requires source and target or --bundle")
            files=[{"source":str(ns.source),"target":ns.target}]
        return client.deploy(files,wait_seconds=ns.wait_seconds)
    if command == "apps":
        if ns.action == "list":return client.apps_list(ns.directories)
        if ns.action == "refresh":return client.apps_refresh(ns.target,wait_seconds=ns.wait_seconds)
        return client.apps_launch(ns.target,ns.path,wait_seconds=ns.wait_seconds,image_elf=ns.image_elf)
    if command in ("key","click","touch"):
        return client.input_event(ns.target,ns.action if command=="key" else "click" if command=="click" else "touch-"+ns.action,
                                  code=ns.key if command=="key" else ns.x,argument=0 if command=="key" else ns.y,duration_ticks=getattr(ns,"ticks",1))
    if command == "jobs":
        if ns.action == "status":return client.job_status(ns.id)
        if ns.action == "wait":return client.wait_job(ns.id,wait_seconds=ns.wait_seconds)
        return client.job_cancel(ns.id)
    if command == "session":
        if ns.plan:
            return client.run_steps(read_steps(ns.plan),report_directory=ns.directory)
        def incoming():
            for line in sys.stdin:
                if line.strip():yield resolve_step_paths(json.loads(line),Path.cwd())
        return client.run_steps(incoming(),report_directory=ns.directory,
                                progress=lambda stage:print(json.dumps({"stage":stage},ensure_ascii=False),flush=True))
    if command == "run-test":
        document=json.loads(ns.manifest.read_text(encoding="utf-8-sig"));base=ns.manifest.resolve().parent
        steps=[]
        if document.get("files"):steps.append({"action":"deploy","files":document["files"]})
        if document.get("refresh", bool(document.get("files") or "launch" in document)):
            steps.append({"action":"refresh","target":document.get("refresh_target", "desktop")})
        if "launch" in document:
            step={"action":"launch","target":document.get("desktop_target","desktop"),"path":document["launch"]}
            if "image_elf" in document:step["image_elf"]=document["image_elf"]
            steps.append(step)
        steps.extend(document.get("steps",[]))
        steps += [{"action":"windows"},{"action":"screenshot","output":"screen.png"},{"action":"status"}]
        return client.run_steps([resolve_step_paths(step,base) for step in steps],report_directory=ns.directory)
    raise ValueError("unknown management command")
