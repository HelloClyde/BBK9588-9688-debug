#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""9588 JZ4740 debug client. Never retries an RPC automatically."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import time
import zlib

MAGIC = b"96DB"
TARGET = "9588"
IDENTITIES = {"jz4740": b"9588DEBUG/1 JZ4740 RESIDENT"}
PIDS = {"jz4740": 0xA4A9}  # Experimental local debugger PID, separate from 9688.
IDENTITY = IDENTITIES["jz4740"]
VID, PID = 0x07C4, PIDS["jz4740"]
HEADER = struct.Struct("<4sBBHHH")
STATUS_FIELDS = ("version", "state", "configured", "usb_fault", "resets", "commands", "bad_frames", "tick", "polls", "last_poll", "resident_base", "resident_bytes", "image_id", "exec_state", "last_error", "snapshot_valid")
EXEC_FIELDS = ("version", "state", "result", "cancel_flags", "start_tick", "end_tick", "budget_ticks", "output_size", "received_bytes")


class ProtocolError(RuntimeError):
    pass


class DeviceError(RuntimeError):
    def __init__(self, opcode: int, status: int):
        self.opcode, self.status = opcode, status
        super().__init__(f"device rejected opcode {opcode}: status {status}")


def frame(opcode: int, sequence: int, payload: bytes = b"", status: int = 0) -> bytes:
    if len(payload) > 512 or not 0 <= opcode <= 255:
        raise ValueError("frame exceeds protocol limits")
    data = HEADER.pack(MAGIC, 1, opcode, sequence, len(payload), status) + payload
    return data + struct.pack("<I", zlib.crc32(data))


def words(payload: bytes, count: int) -> tuple[int, ...]:
    if len(payload) != count * 4:
        raise ProtocolError(f"expected {count * 4} reply bytes, got {len(payload)}")
    return struct.unpack(f"<{count}I", payload)


def ports(chip: str | None = None) -> list:
    from serial.tools import list_ports
    if chip is not None and chip not in PIDS:
        raise ValueError("unsupported chip")
    allowed = (PIDS[chip],) if chip else tuple(PIDS.values())
    return [p for p in list_ports.comports() if p.vid == VID and p.pid in allowed]


class Client:
    def __init__(self, serial, timeout: float = 5, chip: str | None = None):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if chip is not None and chip not in IDENTITIES:
            raise ValueError("unsupported chip")
        self.serial, self.timeout, self.sequence, self.broken = serial, timeout, 0, False
        self.caps, self.chip, self.expected_chip = None, None, chip

    @classmethod
    def connect(cls, port: str | None = None, timeout: float = 5, chip: str | None = None) -> "Client":
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        import serial
        found = ports(chip)
        selected = [p for p in found if port is None or p.device.lower() == port.lower()]
        if len(selected) != 1:
            raise RuntimeError("expected one 9588 debug device (07C4:A4A9); use list, --chip or --port to select")
        stream = serial.Serial(selected[0].device, baudrate=115200, timeout=min(timeout, 0.25), write_timeout=timeout)
        selected_chip = next(name for name, pid in PIDS.items() if pid == selected[0].pid)
        client = cls(stream, timeout, chip=selected_chip)
        try:
            stream.reset_input_buffer()
            client.handshake()
        except BaseException:
            stream.close()
            raise
        return client

    def close(self):
        self.serial.close()

    def exact(self, count: int, deadline: float) -> bytes:
        data = bytearray()
        while len(data) < count:
            if time.monotonic() >= deadline:
                raise TimeoutError("USB reply timed out; command outcome may be unknown; reconnect and inspect state")
            part = self.serial.read(count - len(data))
            if len(part) > count - len(data):
                raise ProtocolError("serial returned too many bytes")
            data.extend(part)
        return bytes(data)

    def rpc(self, opcode: int, payload: bytes = b"") -> bytes:
        if self.broken:
            raise ProtocolError("session lost; open a new connection before further commands")
        if not 0 < opcode < 128:
            raise ValueError("invalid request opcode")
        self.sequence = (self.sequence + 1) & 0xFFFF
        packet = frame(opcode, self.sequence, payload)
        try:
            if self.serial.write(packet) != len(packet):
                raise ProtocolError("short USB write; command outcome may be unknown")
            deadline = time.monotonic() + self.timeout
            header = self.exact(12, deadline)
            magic, version, reply_op, sequence, count, status = HEADER.unpack(header)
            if (magic, version, reply_op, sequence) != (MAGIC, 1, opcode | 128, self.sequence) or count > 512:
                raise ProtocolError("invalid USB reply header; command outcome may be unknown")
            tail = self.exact(count + 4, deadline)
            if zlib.crc32(header + tail[:-4]) != struct.unpack("<I", tail[-4:])[0]:
                raise ProtocolError("USB reply CRC mismatch; command outcome may be unknown")
        except BaseException:
            self.broken = True
            raise
        if status:
            raise DeviceError(opcode, status)
        return tail[:-4]

    def handshake(self):
        identity = self.rpc(1)
        chip = next((name for name, text in IDENTITIES.items() if text == identity), None)
        if chip is None or (self.expected_chip is not None and self.expected_chip != chip):
            self.broken = True
            raise ProtocolError(f"wrong debug service: {identity!r}")
        self.chip = chip
        self.caps = words(self.rpc(9), 8)
        if self.caps[0] != 1 or self.caps[2] != 512:
            self.broken = True
            raise ProtocolError("unsupported capabilities layout")
        return identity.decode()

    def status(self):
        return dict(zip(STATUS_FIELDS, words(self.rpc(2), 16)))

    def memory_range(self, address: int, size: int):
        if self.caps is None:
            raise ProtocolError("handshake required")
        base, span = self.caps[3:5]
        if size <= 0 or size > 16 * 1024 * 1024 or address < base or address >= base + span or size > base + span - address:
            raise ValueError("RAM range is empty, too large, or outside this device")

    def read(self, address: int, size: int) -> bytes:
        self.memory_range(address, size)
        data = bytearray()
        while len(data) < size:
            wanted = min(512, size - len(data))
            chunk = self.rpc(3, struct.pack("<II", address + len(data), wanted))
            if len(chunk) != wanted:
                raise ProtocolError("short RAM reply")
            data.extend(chunk)
        return bytes(data)

    def write(self, address: int, data: bytes):
        self.memory_range(address, len(data))
        for offset in range(0, len(data), 508):
            self.rpc(4, struct.pack("<I", address + offset) + data[offset:offset + 508])
        return {"address": address, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def screenshot(self, output: Path, *, native_orientation: bool = False, rotation_degrees: int | None = None):
        from PIL import Image
        if rotation_degrees not in (None, 0, 90, 180, 270):
            raise ValueError("rotation must be 0, 90, 180 or 270 degrees")
        token, width, height, stride, fmt, size, address, tick = words(self.rpc(6), 8)
        try:
            pixel_bytes = 2 if fmt == 1 else 4 if fmt == 2 else 0
            if (width, height) != tuple(self.caps[5:7]) or not pixel_bytes or stride != width * pixel_bytes or size != stride * height:
                raise ProtocolError("invalid screen metadata")
            data = bytearray()
            while len(data) < size:
                offset = len(data)
                reply = self.rpc(7, struct.pack("<III", token, offset, min(500, size - offset)))
                if len(reply) < 13 or words(reply[:12], 3) != (token, offset, size) or len(reply) - 12 > min(500, size - offset):
                    raise ProtocolError("invalid screenshot chunk")
                data.extend(reply[12:])
        finally:
            if not self.broken:
                self.rpc(8, struct.pack("<I", token))
        if fmt == 1:
            rgb = bytearray()
            for (v,) in struct.iter_unpack("<H", data):
                r, g, b = v >> 11, (v >> 5) & 63, v & 31
                rgb.extend(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
            img = Image.frombytes("RGB", (width, height), bytes(rgb))
        else:
            img = Image.frombytes("RGB", (width, height), bytes(data), "raw", "BGRX")
        # The unmodified C200 desktop was observed upside down in QEMU's
        # native framebuffer. Physical 9588 orientation still needs checking.
        rotation = (0 if native_orientation else 180) if rotation_degrees is None else rotation_degrees
        if rotation:
            img = img.transpose({90: Image.Transpose.ROTATE_90,
                                 180: Image.Transpose.ROTATE_180,
                                 270: Image.Transpose.ROTATE_270}[rotation])
        output.parent.mkdir(parents=True, exist_ok=True)
        img.save(output, format="PNG")
        return {"output": str(output), "width": width, "height": height, "format": fmt, "framebuffer": address, "tick": tick, "bytes": size, "rotation_degrees": rotation, "orientation": "native" if not rotation else "display" if rotation == 180 else "rotated", "raw_sha256": hashlib.sha256(data).hexdigest()}

    def exec_info(self):
        names = ("version", "base", "capacity", "state", "image_bytes", "memory_bytes", "entry_offset", "args_capacity", "output_capacity", "stack_bytes")
        return dict(zip(names, words(self.rpc(26), 10)))

    def exec_status(self):
        result = dict(zip(EXEC_FIELDS, words(self.rpc(31), 9)))
        if result["result"] >= 0x80000000:
            result["result"] -= 0x100000000
        return result

    def exec_run(self, source: Path, directory: Path, opcode=0, args=b"", budget=80, wait_seconds=30, prefix=None):
        if len(args) > 256 or not 1 <= budget <= 400:
            raise ValueError("invalid module argument size or tick budget")
        if not math.isfinite(wait_seconds) or wait_seconds <= 0:
            raise ValueError("wait-seconds must be finite and positive")
        info = self.exec_info()
        if info["state"] in (3, 4):
            raise RuntimeError("module already pending/running")
        metadata, image = compile_module(source, directory, info["base"], info["capacity"], prefix)
        self.rpc(27, struct.pack("<IIII", len(image), metadata["memory_bytes"], metadata["entry_offset"], zlib.crc32(image)))
        for offset in range(0, len(image), 508):
            self.rpc(28, struct.pack("<I", offset) + image[offset:offset + 508])
        self.rpc(29)
        self.rpc(30, struct.pack("<II", opcode, budget) + args)
        deadline = time.monotonic() + wait_seconds
        while True:
            state = self.exec_status()
            if state["state"] == 5:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("module has not returned; inspect exec status or request exec cancel; no command was retried")
            time.sleep(0.05)
        size = state["output_size"]
        if size > 512:
            raise ProtocolError("module output exceeds capacity")
        data = bytearray()
        while len(data) < size:
            offset = len(data)
            reply = self.rpc(33, struct.pack("<II", offset, min(504, size - offset)))
            if len(reply) <= 8 or words(reply[:8], 2) != (offset, size) or len(reply) - 8 > min(504, size - offset):
                raise ProtocolError("invalid module output chunk")
            data.extend(reply[8:])
        state.update({"output_hex": data.hex(), "module": metadata})
        (directory / "execution.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return state


def tool_prefix():
    configured = os.environ.get("BDA_TOOLCHAIN_PREFIX")
    if configured:
        return configured
    root = Path(__file__).resolve().parents[1]
    base = Path(os.environ.get("BDA_SDK_ROOT", root / "sdk")) / ".toolchain" / "bin" / "mipsel-none-elf-"
    if Path(str(base) + "gcc.exe").is_file() or Path(str(base) + "gcc").is_file():
        return str(base)
    return "mipsel-none-elf-"


def compile_module(source: Path, directory: Path, base: int, capacity: int, prefix=None):
    from elftools.elf.elffile import ELFFile
    directory.mkdir(parents=True, exist_ok=True)
    prefix = prefix or tool_prefix()
    script, elf, binary = (directory / name for name in ("module.ld", "module.elf", "module.bin"))
    script.write_text(f"""ENTRY(debug9588_entry)
SECTIONS {{ . = 0x{base:x};
 .text : {{ *(.text.debug9588_entry) *(.text*) }}
 .rodata : {{ *(.rodata*) }} .data : {{ *(.data*) }}
 . = ALIGN(32); .bss (NOLOAD) : {{ *(.bss*) *(COMMON) }}
 . = ALIGN(32); __module_end = .;
 /DISCARD/ : {{ *(.MIPS.abiflags) *(.reginfo) *(.pdr) *(.comment) *(.note*) }}
}}
""", encoding="ascii")
    command = [prefix + "gcc", "-EL", "-march=mips32", "-mabi=32", "-msoft-float", "-mno-abicalls", "-G0", "-fno-pic", "-Os", "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-nostdlib", "-Wall", "-Wextra", "-Werror", "-Wl,--build-id=none", f"-Wl,-T,{script.resolve()}", "-I", str(Path(__file__).resolve().parent), "-I", str(Path(os.environ.get("BDA_SDK_ROOT", Path(__file__).resolve().parents[1] / "sdk")) / "sdk/include"), str(source.resolve()), "-o", str(elf.resolve())]
    subprocess.run(command, check=True)
    subprocess.run([prefix + "objcopy", "-O", "binary", str(elf.resolve()), str(binary.resolve())], check=True)
    with elf.open("rb") as stream:
        symbols = {s.name: s["st_value"] for s in ELFFile(stream).get_section_by_name(".symtab").iter_symbols()}
    image = binary.read_bytes()
    memory = symbols["__module_end"] - base
    entry = symbols["debug9588_entry"] - base
    if not 0 <= entry < len(image) <= memory <= capacity or entry % 4:
        raise ValueError("compiled module exceeds device allocation")
    metadata = {"source": str(source.resolve()), "base": base, "entry_offset": entry, "image_bytes": len(image), "memory_bytes": memory, "sha256": hashlib.sha256(image).hexdigest()}
    (directory / "module.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata, image


def number(s):
    return int(s, 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port")
    parser.add_argument("--chip", choices=("auto", "jz4740"), default="auto")
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--report", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "ping", "status", "caps", "stop", "usb-diag"):
        commands.add_parser(name)
    p = commands.add_parser("read");p.add_argument("address", type=number);p.add_argument("size", type=number);p.add_argument("--output", type=Path, required=True)
    p = commands.add_parser("write");p.add_argument("address", type=number);group=p.add_mutually_exclusive_group(required=True);group.add_argument("--file", type=Path);group.add_argument("--hex")
    p = commands.add_parser("screenshot");p.add_argument("output", type=Path);p.add_argument("--native-orientation", action="store_true", help="keep the raw framebuffer; default corrects the QEMU-observed 180 degree rotation");p.add_argument("--rotate", type=int, choices=(0,90,180,270), default=None, help="explicit rotation override")
    p = commands.add_parser("call");p.add_argument("table", choices=("gui", "fs", "sys", "mem", "res"));p.add_argument("offset", type=number);p.add_argument("args", type=number, nargs="*", default=[])
    p = commands.add_parser("exec");sub=p.add_subparsers(dest="action", required=True)
    for name in ("info", "status", "cancel", "reset"):
        sub.add_parser(name)
    p=sub.add_parser("run");p.add_argument("source", type=Path);p.add_argument("--directory", type=Path, default=Path("build/9588-debug-module"));p.add_argument("--opcode", type=number, default=0);p.add_argument("--args-hex", default="");p.add_argument("--budget-ticks", type=int, default=80);p.add_argument("--wait-seconds", type=float, default=30);p.add_argument("--prefix")
    ns = parser.parse_args(argv)
    client = None
    try:
        if not math.isfinite(ns.timeout) or ns.timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        chip = None if ns.chip == "auto" else ns.chip
        if ns.command == "list":
            result = [{"port": p.device, "chip": next(name for name, pid in PIDS.items() if pid == p.pid), "description": p.description, "serial_number": p.serial_number} for p in ports(chip)]
        else:
            client = Client.connect(ns.port, ns.timeout, chip)
            if ns.command == "ping":result = {"identity": client.rpc(1).decode(), "chip": client.chip}
            elif ns.command == "status":result = client.status()
            elif ns.command == "caps":result = {"words": client.caps}
            elif ns.command == "stop":client.rpc(10);result = {"stop_accepted": True, "note": "ACK received; CDC may disappear before final restore; verify disk after reconnecting USB"}
            elif ns.command == "usb-diag":result = {"words": words(client.rpc(14), 56)}
            elif ns.command == "read":
                data=client.read(ns.address,ns.size);ns.output.parent.mkdir(parents=True,exist_ok=True);ns.output.write_bytes(data);result={"output":str(ns.output),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest()}
            elif ns.command == "write":result = client.write(ns.address, ns.file.read_bytes() if ns.file else bytes.fromhex(ns.hex))
            elif ns.command == "screenshot":result = client.screenshot(ns.output, native_orientation=ns.native_orientation, rotation_degrees=ns.rotate)
            elif ns.command == "call":
                if len(ns.args)>4:raise ValueError("call accepts at most four arguments")
                args=ns.args+[0]*(4-len(ns.args));idx=("gui","fs","sys","mem","res").index(ns.table)
                result={"result":words(client.rpc(20,struct.pack("<6I",idx,ns.offset,*args)),1)[0]}
            elif ns.action == "info":result=client.exec_info()
            elif ns.action == "status":result=client.exec_status()
            elif ns.action in ("cancel","reset"):
                client.rpc(32 if ns.action=="cancel" else 34);result={"accepted":True}
            else:result=client.exec_run(ns.source,ns.directory,ns.opcode,bytes.fromhex(ns.args_hex),ns.budget_ticks,ns.wait_seconds,ns.prefix)
        report={"ok":True,"result":result}
    except Exception as exc:
        report={"ok":False,"error":str(exc),"type":type(exc).__name__,"session_lost":bool(client and client.broken)}
    finally:
        if client:client.close()
    text=json.dumps(report,ensure_ascii=False,indent=2)
    print(text)
    if ns.report:
        ns.report.parent.mkdir(parents=True,exist_ok=True);ns.report.write_text(text+"\n",encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
