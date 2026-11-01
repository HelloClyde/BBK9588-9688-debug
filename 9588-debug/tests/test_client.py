# SPDX-License-Identifier: GPL-3.0-only
"""Host framing and failure behavior; no device or serial port is opened."""
import contextlib
import io
import importlib.util
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("debug9588_client", Path(__file__).resolve().parents[1] / "client.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class FakeSerial:
    def __init__(self, responder=None, split=1024):
        self.responder = responder or (lambda op, seq, data: client.frame(op | 128, seq, data))
        self.pending = bytearray()
        self.writes = []
        self.split = split
        self.short_write = False
        self.closed = False

    def write(self, packet):
        self.writes.append(packet)
        _, _, opcode, seq, size, _ = client.HEADER.unpack(packet[:12])
        self.pending.extend(self.responder(opcode, seq, packet[12:12 + size]))
        return len(packet) - int(self.short_write)

    def read(self, size):
        count = min(size, self.split, len(self.pending))
        out = bytes(self.pending[:count])
        del self.pending[:count]
        return out

    def reset_input_buffer(self):
        self.pending.clear()

    def close(self):
        self.closed = True


class HostTests(unittest.TestCase):



    def test_crc_known_vector(self):
        packet = client.frame(1, 0x1234, b"abc")
        self.assertEqual(packet[:12], bytes.fromhex("393644420101341203000000"))
        self.assertEqual(struct.unpack("<I", packet[-4:])[0], client.zlib.crc32(packet[:-4]))

    def test_fragmented_reads(self):
        serial = FakeSerial(split=1)
        c = client.Client(serial)
        self.assertEqual(c.rpc(3, b"a" * 512), b"a" * 512)
        self.assertFalse(c.broken)

    def test_handshake_and_h1_identity_rejected(self):
        def respond(op, seq, _):
            data = client.IDENTITY if op == 1 else struct.pack("<8I", 1, 31, 512, 0x80000000, 0x2000000, 240, 320, 123)
            return client.frame(op | 128, seq, data)
        c = client.Client(FakeSerial(respond))
        self.assertEqual(c.handshake(), client.IDENTITY.decode())
        self.assertEqual(c.caps[5:7], (240, 320))
        other = client.Client(FakeSerial(lambda op, seq, _: client.frame(op | 128, seq, b"H1DEBUG/1 V1.41 RESIDENT")))
        with self.assertRaises(client.ProtocolError):
            other.handshake()
        self.assertTrue(other.broken)

    def test_crc_error_poison_no_retry(self):
        def corrupt(op, seq, data):
            packet = bytearray(client.frame(op | 128, seq, data));packet[-1] ^= 1;return packet
        serial = FakeSerial(corrupt)
        c = client.Client(serial)
        with self.assertRaises(client.ProtocolError):c.rpc(4, b"abcd")
        with self.assertRaises(client.ProtocolError):c.rpc(4, b"abcd")
        self.assertEqual(len(serial.writes), 1)

    def test_wrong_sequence_and_oversize(self):
        cases = (
            lambda op, seq, data: client.frame(op | 128, seq + 1, data),
            lambda op, seq, data: client.HEADER.pack(client.MAGIC, 1, op | 128, seq, 513, 0),
            lambda op, seq, data: client.HEADER.pack(b"H1DB", 1, op | 128, seq, 0, 0),
        )
        for respond in cases:
            with self.subTest(respond=respond):
                c = client.Client(FakeSerial(respond))
                with self.assertRaises(client.ProtocolError):c.rpc(2)
                self.assertTrue(c.broken)

    def test_device_rejection_keeps_session(self):
        c = client.Client(FakeSerial(lambda op, seq, _: client.frame(op | 128, seq, status=2)))
        with self.assertRaises(client.DeviceError):c.rpc(4, b"abcd")
        self.assertFalse(c.broken)

    def test_short_write_and_timeout_no_retry(self):
        serial = FakeSerial();serial.short_write = True
        c = client.Client(serial)
        with self.assertRaises(client.ProtocolError):c.rpc(4, b"abcd")
        self.assertTrue(c.broken);self.assertEqual(len(serial.writes), 1)
        serial = FakeSerial(lambda op, seq, _: b"")
        c = client.Client(serial, timeout=0.001)
        with self.assertRaises(TimeoutError):c.rpc(30)
        self.assertTrue(c.broken);self.assertEqual(len(serial.writes), 1)

    def test_memory_chunking_and_bounds(self):
        memory = bytes(i % 256 for i in range(1300))
        def respond(op, seq, data):
            if op == 3:
                address, count = struct.unpack("<II", data)
                result = memory[address - 0x80001000:address - 0x80001000 + count]
            else:result = b""
            return client.frame(op | 128, seq, result)
        serial = FakeSerial(respond);c=client.Client(serial);c.caps=(1,31,512,0x80000000,0x2000000,240,320,1)
        self.assertEqual(c.read(0x80001000, len(memory)), memory)
        self.assertEqual(len(serial.writes), 3)
        c.write(0x80001000, memory)
        self.assertEqual(len(serial.writes), 6)
        self.assertEqual([len(p)-16-4 for p in serial.writes[3:]], [508,508,284])
        for address, count in ((0x82000000,1),(0x81ffffff,2),(0x7fffffff,1),(0x80000000,0),(0xffffffff,4)):
            with self.assertRaises(ValueError):c.read(address,count)

    def test_local_input_invalid_before_transport(self):
        c=client.Client(FakeSerial())
        with self.assertRaises(ValueError):c.rpc(128)
        with self.assertRaises(ValueError):client.frame(1,0,b"x"*513)
        self.assertEqual(c.serial.writes, [])

    def test_timeouts_rejected_before_execution(self):
        for timeout in (0,-1,float("nan"),float("inf")):
            with self.assertRaises(ValueError):client.Client(FakeSerial(),timeout)
        c=client.Client(FakeSerial())
        with self.assertRaises(ValueError):
            c.exec_run(Path("unused.c"),Path("unused"),wait_seconds=-1)
        self.assertEqual(c.serial.writes, [])

    def test_rgb565_screenshot_and_token_release(self):
        from PIL import Image
        pixels=struct.pack("<HHH",0xf800,0x07e0,0x001f)*(240*320//3)
        def respond(op,seq,data):
            if op==6:reply=struct.pack("<8I",7,240,320,480,1,len(pixels),0xa0900000,42)
            elif op==7:
                token,offset,wanted=struct.unpack("<III",data)
                reply=struct.pack("<III",token,offset,len(pixels))+pixels[offset:offset+wanted]
            elif op==8:
                self.assertEqual(struct.unpack("<I",data)[0],7);reply=b""
            else:raise AssertionError(op)
            return client.frame(op|128,seq,reply)
        serial=FakeSerial(respond);c=client.Client(serial);c.caps=(1,31,512,0x80000000,0x2000000,240,320,1)
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/"screen.png"
            result=c.screenshot(output)
            with Image.open(output) as image:
                self.assertEqual(image.size,(240,320))
                self.assertEqual([image.getpixel((i,0)) for i in range(3)],[(0,0,255),(0,255,0),(255,0,0)])
            self.assertEqual(result["raw_sha256"],client.hashlib.sha256(pixels).hexdigest())
        self.assertEqual(client.HEADER.unpack(serial.writes[-1][:12])[2],8)

    def _assert_screenshot_orientation(self, fmt):
        """Use unequal corner blocks to distinguish rotation from mirroring."""
        from PIL import Image
        width, height, token = 240, 320, 51
        native_rgb, raw = bytearray(), bytearray()
        for y in range(height):
            for x in range(width):
                if x < 40 and y < 30:
                    color = (255, 0, 0)
                elif x >= 190 and y < 50:
                    color = (0, 255, 0)
                elif x < 25 and y >= 250:
                    color = (0, 0, 255)
                elif x >= 170 and y >= 260:
                    color = (255, 255, 255)
                else:
                    color = (0, 0, 0)
                native_rgb.extend(color)
                r, g, b = color
                if fmt == 1:
                    raw.extend(struct.pack("<H", (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)))
                else:
                    raw.extend((b, g, r, (x + y) & 255))
        pixels = bytes(raw)
        rotated_rgb = b"".join(native_rgb[i:i + 3] for i in range(len(native_rgb) - 3, -1, -3))
        digest = client.hashlib.sha256(pixels).hexdigest()
        corners = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
        with tempfile.TemporaryDirectory() as temp:
            for native in (False, True):
                with self.subTest(format=fmt, native_orientation=native):
                    def respond(op, seq, data):
                        if op == 6:
                            stride = width * (2 if fmt == 1 else 4)
                            reply = struct.pack("<8I", token,width,height,stride,fmt,len(pixels),0xa0900000,42)
                        elif op == 7:
                            received_token, offset, wanted = struct.unpack("<III", data)
                            self.assertEqual(received_token, token)
                            reply = struct.pack("<III", token,offset,len(pixels)) + pixels[offset:offset + wanted]
                        elif op == 8:
                            self.assertEqual(struct.unpack("<I", data)[0], token)
                            reply = b""
                        else:
                            raise AssertionError(op)
                        return client.frame(op | 128, seq, reply)
                    serial = FakeSerial(respond)
                    c = client.Client(serial)
                    c.caps = (1,31,512,0x80000000,0x2000000,width,height,1)
                    output = Path(temp) / f"screen-{fmt}-{native}.png"
                    result = c.screenshot(output, native_orientation=True) if native else c.screenshot(output)
                    with Image.open(output) as image:
                        self.assertEqual(image.size, (width, height))
                        self.assertEqual(image.mode, "RGB")
                        self.assertEqual(image.tobytes(), bytes(native_rgb) if native else rotated_rgb)
                        expected_corners = [(255,0,0),(0,255,0),(0,0,255),(255,255,255)]
                        self.assertEqual([image.getpixel(point) for point in corners], expected_corners if native else expected_corners[::-1])
                    self.assertEqual(result["raw_sha256"], digest)
                    operations = [client.HEADER.unpack(packet[:12])[2] for packet in serial.writes]
                    self.assertEqual(operations[-1], 8)
                    self.assertEqual(operations.count(8), 1)
                    self.assertFalse(c.broken)

    def test_rgb565_screenshot_display_default_and_native_orientation(self):
        self._assert_screenshot_orientation(1)

    def test_bgrx_screenshot_display_default_and_native_orientation(self):
        self._assert_screenshot_orientation(2)




class TargetIsolationTests(unittest.TestCase):
    def test_inventory_never_opens_9688_even_with_explicit_port(self):
        own=SimpleNamespace(device="COM9",vid=client.VID,pid=0xa4a9)
        old=SimpleNamespace(device="COM7",vid=client.VID,pid=0xa4a7)
        old30=SimpleNamespace(device="COM8",vid=client.VID,pid=0xa4a8)
        with patch("serial.tools.list_ports.comports",return_value=[own,old,old30]):
            self.assertEqual(client.ports(),[own])
            with patch("serial.Serial") as constructor:
                with self.assertRaises(RuntimeError):
                    client.Client.connect(port="COM7")
                constructor.assert_not_called()

    def test_mismatched_9688_identity_poisoned_and_closed_before_caps(self):
        own=SimpleNamespace(device="COM9",vid=client.VID,pid=0xa4a9)
        for identity in (b"9688DEBUG/1 V2.32 JZ4740 RESIDENT",
                         b"9688DEBUG/1 V2.32 JZ4730 RESIDENT",
                         b"H1DEBUG/1 V1.41 RESIDENT"):
            stream=FakeSerial(lambda op,seq,payload:client.frame(op|128,seq,identity))
            with patch("serial.tools.list_ports.comports",return_value=[own]), patch("serial.Serial",return_value=stream):
                with self.assertRaises(client.ProtocolError):
                    client.Client.connect()
            self.assertTrue(stream.closed)
            self.assertEqual(len(stream.writes),1)

    def test_all_three_pids_require_their_matching_target_identity(self):
        for chip,pid in client.PIDS.items():
            for identity_chip,identity in client.IDENTITIES.items():
                with self.subTest(chip=chip,identity=identity_chip):
                    port=SimpleNamespace(device="COM42",vid=client.VID,pid=pid)
                    def respond(op,seq,data):
                        reply=identity if op==1 else struct.pack("<8I",1,31,512,0x80000000,0x2000000,240,320,1)
                        return client.frame(op|128,seq,reply)
                    stream=FakeSerial(respond)
                    with patch("serial.tools.list_ports.comports",return_value=[port]),patch("serial.Serial",return_value=stream):
                        if chip==identity_chip:
                            connection=client.Client.connect()
                            self.assertEqual(connection.chip,chip)
                            connection.close()
                        else:
                            with self.assertRaises(client.ProtocolError):
                                client.Client.connect()
                            self.assertEqual(len(stream.writes),1)
                    self.assertTrue(stream.closed)

    def test_screenshot_cli_defaults_display_and_supports_native_or_rotation(self):
        class Connection:
            broken=False
            def screenshot(self,path,**kwargs):
                self.options=kwargs
                return {"rotation":kwargs["rotation_degrees"]}
            def close(self):
                self.closed=True
        for rotation,native in ((None,False),(None,True),(0,False),(90,False),(180,False),(270,False)):
            connection=Connection()
            arguments=["screenshot","unused.png"]
            if native:arguments+=["--native-orientation"]
            if rotation is not None:arguments+=["--rotate",str(rotation)]
            with patch.object(client.Client,"connect",return_value=connection), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(client.main(arguments),0)
            self.assertEqual(connection.options,{"native_orientation":native,"rotation_degrees":rotation})
            self.assertTrue(connection.closed)

    def test_exact_9588_identity_and_negotiated_dimensions(self):
        def respond(op,seq,payload):
            reply=client.IDENTITY if op==1 else struct.pack("<8I",1,31,512,0x80000000,0x2000000,128,96,1)
            return client.frame(op|128,seq,reply)
        connection=client.Client(FakeSerial(respond),chip="jz4740")
        self.assertEqual(connection.handshake(),"9588DEBUG/1 JZ4740 RESIDENT")
        self.assertEqual(connection.caps[5:7],(128,96))
        with self.assertRaises(ValueError):
            client.Client(FakeSerial(),chip="jz9999")

if __name__ == "__main__":
    unittest.main()
