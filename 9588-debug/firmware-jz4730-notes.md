# BBK 9588 JZ4730 firmware binding

This profile describes the **V1.30** JZ4730 payload in `C200knl.bin`, even though the containing recovery package is labeled V3.30. `verified: true` means the bindings were checked against this exact original payload and primary JZ4730 register documentation. It does not mean USB or the LCD was tested on a physical 9588.

The profile is `profiles/9588-jz4730.json`, target `9588`, controller `jz4730-fifo`. It passes the current `9588-debug/build.py` target validator and profile-header generator. It contains 46 normal CRC guards and two GUI-notice guards. It contains no firmware bytes or machine-specific source paths.

| Input | Size | SHA-256 |
| --- | ---: | --- |
| `C200knl.bin` wrapper | 4,673,208 | `dc41701442176ba81bf1b8041b2f9dac449e04f2adf6532993e7c55471de9bea` |
| Payload after 64-byte header | 4,673,144 | `c7a4ba34d5a4c006f88b7e9e0c0a991b2b1e7a1ec8896859f0cc3dfb18e44270` |

Payload load address is `0x80004000`; end is `0x80478e78`; full payload CRC-32 is `0x9d7b39c7`. The wrapper is not compressed.

## Embedded version

`0x80253888` contains the GBK version label ending in `V1.30`. The GUI code at `0x800190cc..0x800190d8` passes this pointer as `a3` to native text drawing at `0x800c2f00`, with coordinates `(10,30)`. A second native reference at `0x80018f90` uses the release label at `0x80253828`, which starts with `9588v1.30`. The native OS build-information string at `0x80253710` contains `2011-02-18` and `11:40:04`. The package label is not used as firmware identity.

## USB controller, clock and IRQ

This is the dedicated JZ4730 USB 1.1 FIFO controller, not JZ4740 MUSB. Native initializer `0x801890e0` occupies `0x38c` bytes. IRQ handler is `0x8018a04c`; registration at `0x801893e8..0x8018940c` passes IRQ 12 and argument zero to native `0x80004a34`. IRQ-table base is `0x80479634`, so its slot is `0x80479694` and argument word `0x80479698`. IRQ mask is `0x1000`.

| Binding | Value | Independent evidence |
| --- | --- | --- |
| UDC base | `0xb3040000` | Native init and IRQ use 32-bit accesses; JZ4730 UDC manual register table. |
| IN registers | `base+0x20*ep`, status `+4`, buffer words `+8`, max packet `+c` | Native init and manual. |
| OUT registers | `base+0x200+0x20*ep`, status `+4`, max packet `+c` | Native init and manual. |
| Configuration/control/status | `+400/+404/+408` | Native init/IRQ. |
| Device interrupt pending/mask | `+40c/+410` | Native init/IRQ. |
| Endpoint interrupt pending/mask | `+414/+418` | Native init/IRQ. |
| Confirm/zero packet | `+41c/+420` | Native transmit path; manual FIFO/packet handling. |
| Endpoint information | `+500`, then `+504+4*ep` | Native init. |
| Shared receive / first transmit FIFO | `+800/+840` | Native receive `0x8018ad7c`; native transmit. |
| Clock gate | `0xb0000020`, bit `0x01000000` | JZ4730 **MSCR**, bit24 UDC, not JZ4740 bit11. Manual says stopped-clock module registers cannot be accessed. |
| PHY suspend | `0xb000001c`, bit `0x40` | JZ4730 **OCR.SPEND0**: set suspends, clear activates. This differs from JZ4740 OPCR location and enable polarity. |
| Clock configuration / PLL | `0xb0000000/0xb0000010` | JZ4730 CPM register map and native clock functions. |
| Host/device port mux | `0xb3000000`, mask `0x80`, device value zero | Native board init `0x800045c8..0x800045d8` clears bit7. UDC manual p1 identifies SBA_CNTL.USBCFG as device/host-port0 selection; memory map calls this HARB/HAPOR. Exact bit value comes from this ROM. |
| INTC mask/read/set/clear | `0xb0001004/+1008/+100c` | Native IRQ registration and primary map. |

Native sleep code `0x8002b124..0x8002b13c` sets OCR bits `0xc0`, and `0x8002b1ac..0x8002b1cc` clears them on return. `0x8002b154..0x8002b160` saves MSCR and prepares a stopped-module value containing bit24; `0x8002b1a4..0x8002b1a8` restores it.

EP0 supports 8/16/32-byte maximum packets; this ROM selects 32. EP1 is interrupt IN, EP2/3 bulk IN, EP4 isochronous IN, EP5/6 bulk OUT, EP7 isochronous OUT. The debug CDC layout must therefore use notification `0x81`, bulk IN `0x82`, bulk OUT `0x05`. In this ROM, `0x801892dc` writes `0x20` to EP5 control `0xb30402a0`; `0x8018935c` writes 64 to EP5 max-packet register `0xb30402ac`. Both values were obtained by executing the actual native initializer.

The primary manual's p14 setup table assigns SET_ADDRESS and several standard configuration/interface requests to hardware. Software should not emit duplicate status handling for hardware-consumed SET_CONFIGURATION / SET_INTERFACE; their device interrupt indications and status fields drive software state. The FIFO controller has different register/acknowledgement behavior from MUSB.

## Cable sense and software connection output

**GP89 is low-active.** Its raw input is `0xb0010060 & 0x02000000`.

| Raw GP89 bit | Native query `0x8002917c` | Wrapper `0x8002abf4` | Native USB meaning |
| ---: | ---: | ---: | --- |
| 0 | 1 | `0x80` | USB-entry path permitted / cable-present condition |
| `0x02000000` | 0 | 0 | Native USB window can disconnect |

This is more than a guess based on a power-function name. At `0x8001d86c` and `0x8001e3a8`, native callers invoke the USB window `0x800189d8` only if the wrapper is nonzero; that window calls UDC init directly. The window at `0x80018618..0x80018628` keeps the connection when the wrapper is nonzero and calls native disconnect `0x8018ad48` when it becomes zero. GP89 therefore supplies an active-low connection condition in this exact firmware, although its physical VBUS wiring was not measured.

Native input/edge setup is `0x80028194..0x8002823c`. It selects GPIO function0, input direction, and enables the GPIO interrupt. It registers IRQ `137 = 48+89`, handler `0x800294f4`, argument89. The actual native registration is covered by the model.

JZ4730 GPIO banks have a `0x30` register-group stride. For GP89, DATA is `0xb0010060`; interrupt **mask** is `0xb0010084` (DATA+0x24), and W1C interrupt flag is `0xb0010088` (DATA+0x28). Do not use JZ4740 bank stride `0x100` or its mask set/clear aliases.

**PA29 is a separate active-high software connection output.** It is not cable sense:

| Profile macro | Value |
| --- | --- |
| `FW_USB_PULLUP_BASE` | `0xb0010000` |
| `FW_USB_PULLUP_MASK` | `0x20000000` |
| `FW_USB_PULLUP_ACTIVE_LOW` | 0 |
| `FW_USB_PULLUP_DIR_ADDR` | `0xb0010004` |
| `FW_USB_PULLUP_FUNC_ADDR` | `0xb0010014` |
| `FW_USB_PULLUP_FUNC_MASK/VALUE` | `0x0c000000 / 0` |
| `FW_USB_PULLUP_IRQ_ADDR` | `0xb0010020` |

Board init at `0x80004668..0x80004694` disables PA29's interrupt, sets output direction, selects GPIO function0, and raises its DATA bit. Native connect `0x8018ad10` sets PA29; native disconnect `0x8018ad48` clears it. A driver must preserve/restore these relevant bits and implement the native connection control instead of relying on PHY clocking alone. The existing 9688 JZ4730 controller logic by itself does not establish this 9588 board behavior.

No auxiliary cable input was independently established, so AUX is disabled. PA24 in nearby low-level code is SD-card detection (`SD Out!`), not a second USB input.

## LCD and orientation

ROM geometry at `0x80366814/18/1c` is 240,320,16. Native getters `0x80007240/4c/58` return those values. LCD mode at `0x80366810` is `0x900`. Native init `0x8000727c`, descriptor constructor `0x80007414`, and IRQ handler `0x80007e28` independently establish:

| Field | Value |
| --- | --- |
| LCD controller | `0xb3050000` |
| Physical dimensions / stride / bytes | 240×320 / 480 / 153600 |
| Palette / frame cached addresses | `0x81f81000 / 0x81f82000` |
| Frame uncached alias | `0xa1f82000` |
| Default frame descriptor | physical `0x00479ff0` |
| Frame descriptor words | `00479ff0,01f82000,beafbeaf,c0009600` |
| Palette descriptor | physical `0x00479fe0` |
| LCD IRQ | 30, slot `0x80479724`, argument0 |

The MIPS model executed real native init, memset, descriptor construction and IRQ registration. Only clock/panel helper functions `0x80004150` and `0x8000755c` were stubbed. It verified LCDDA0=`0x00479ff0` and the complete descriptor words. This is evidence for memory layout, not electrical LCD timing or color order.

GUI export `+0x5f4` is orientation setter `0x800d0eb0`. It stores the logical orientation at `0x804a8e84`: zero produces logical240×320; nonzero produces logical320×240 at globals `0x804a8e88/8c`. Both paths were executed with explicit GUI callback/clip stubs. Export `+0x738` at `0x8001923c` independently returns panel orientation code `0x130`. Physical scanout geometry, logical GUI rotation, and this panel code are separate facts. Snapshot extraction should follow the physical LCD descriptor source; matching a visually upright image remains a physical-device validation step.

## Service tables, notices and runtime

The five original seed import values are GUI `0x80253f90`, FS `0x80253f00`, SYS `0x80253d90`, MEM `0x802547cc`, RES `0x80253e60`, copied from seed `0x802547b0` to BDA runtime `0x81c00000`. Seed CRC over 0x20 bytes is `0x638d904f`.

| Table | CRC span | CRC-32 | Call offset bound |
| --- | ---: | --- | ---: |
| GUI | `0x820` | `0x6c9983ce` | `0x820`, with per-slot NULL rejection |
| FS | `0x90` | `0xec64208b` | `<0x84` |
| SYS | `0xd0` | `0xf4b7c3dd` | `<0xcc` |
| MEM | `0x34` | `0x29f7feb5` | `<0x30` |
| RES | `0xa0` | `0x10f8e3aa` | `<0x9c` |

GUI last non-NULL export is `+0x810`; `+0x814/+0x818/+0x81c` are NULL. Internal NULLs are also present, so bounds never replace NULL/pointer validation. FS, SYS, MEM and RES trailing NULL regions start at their call bounds above. The GUI guard spans all structural table bytes.

Safe notice is GUI `+0x2b8`, slot `0x80254248`, pointer `0x800c8704`: four-byte slot CRC=`0x0b518962`, entry0x80 CRC=`0xa1c38c7a`. Notice safety may depend only on validated GUI import/notice guards; a rejected FS/SYS/MEM/RES import must still prevent resident allocation, task creation and USB startup.

RTOS bindings come from independently checked `firmware-findings-jz4730.json` and were CRC-verified again against this raw payload. Important boundaries: task table `0x8087eb90`; task-create-ext `0x8000f5ec`; global alloc/free `0x80006e88/0x80006034`; tick handler `0x8012dd5c`, IRQ23 slot `0x804796ec`, argument0; tick reader `0x8012de98`; 40Hz tick. Static task priorities are7/8/9/62/63; resident priority6 must still be checked empty at launch.

## Memory evidence and its limit

Native BSS clear covers `[0x80478e78,0x80a3c790)`. Native global-heap setup at `0x80006bd0/0x80006e0c` establishes `[0x80a4c000,0x81c00000)`. The original BDA runtime sits at that upper boundary. The LCD frame lies near the top of the first32MiB window.

There is independent JZ4730 geometry evidence: ROM helper `0x8002caa0..0x8002cab0` constructs `DMCR=0x059a0a0d` and stores it to `0xb3010080`. Primary EMC pp31–32 decode BW=0,CA=1,RA=1,BA=1 as32-bit bus,9 column bits,12 row bits,4 internal banks: `4 * 2**(9+12) * 4 = 33554432` bytes **per populated chip select**. The manual's p66 example uses the same geometry fields.

A direct caller of this small helper, a JZ4730 bootloader, actual DMAR chip-select population, and a physical RAM-capacity measurement were not established. Consequently the profile uses the supported restricted KSEG0 window `[0x80004000,0x82000000)` and records `physical_sdram_bytes: null`. It does not claim there can be no additional populated memory. It does not borrow the JZ4740 loader as JZ4730 proof.

## Reproduce native evidence

Run `python validation/native_jz4730.py C200knl.bin` with Python Unicorn installed. The script also accepts this exact raw payload. It reads original input and writes only `jz4730-native-model.json` beside itself. It validates all48 CRC guards before executing original MIPS instructions. MMIO is explicit register storage through Unicorn `mmio_map`; the script does not access a physical USB controller, serial device or disk.

The report records all native UDC writes, IRQ arguments, GP89 polarity, PA29/SBA outcomes, GPIO setup, LCD descriptors and logical GUI rotation. Original-input hashes are checked before execution. The pass does not demonstrate host enumeration/ACK timing, physical VBUS connection, RFIFO concurrency, native mass-storage restoration, or upright physical LCD output.

Primary reference hashes and relevant PDF pages are embedded in the JSON. Only the local JZ4730 programming manual and the exact original payload were used for hardware bindings; 9688 addresses were not copied into this profile.
