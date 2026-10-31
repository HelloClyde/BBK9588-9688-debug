# 9688 V2.32 / JZ4740 固件定位证据

本 profile 只对应本机 `9688V2.32SD卡一键恢复系统` 中的 JZ4740 版本。
JZ4730 使用独立 profile 和 USB 驱动，详见 [JZ4730 笔记](firmware-jz4730-notes.md)。
以下结论来自本地二进制的 MIPS32 little-endian 反汇编和调用关系；没有进行真机验证。
不包含原厂固件，也不重新分发反汇编全文。

## 固件身份与地址

| 输入 | 字节数 | SHA256 |
| --- | ---: | --- |
| `底层升级4740/C100.bin` | 4872632 | `7ec707b69a1f4fa7016856b2eba6900412d02d7daefc8b4d11654c63731abca0` |
| `系统数据/系统/数据/kj409688.bin` | 4872696 | `17a50d6f4853bbc638ce5c26434e9722f65eb8a0ddd62ec7ffe230b6c628e930` |
| `底层升级4730/C100.bin`，使用独立 profile | 4871720 | `b18ce485a6ca6a8ea9ef5e3963d5970188ff7a2aeb4a6d4e58fe43625f49e514` |

`kj409688.bin[0x40:]` 与 JZ4740 `C100.bin` 完全逐字节相同，并非需要猜测的解密结果。
raw 映像加载基址为 `0x80004000`，文件偏移为 `VA - 0x80004000`。
固件包含 `9688 OS` / `V2.32`，以及 `9688V2.3` 的旧格式字符串。

BDA 载入器 `0x8002e86c..0x8002e8a8` 把代码读到 `0x81c00020`，同步缓存后 `jalr`。
本实现把 `[0x80004000,0x82000000)` 作为可接受的 KSEG0 RAM 边界；堆初始化
`0x80007454` 的结束地址为 `0x81c00000`，后面的区域用于 BDA。不能把 H1 的 64 MiB
边界 `0x84000000` 直接搬过来。

## BDA 服务表

`0x8002d3d8` 把 `0x8029c8c0` 的八个 word 复制到 `0x81c00000`：

| BDA 参数地址 | 值 | 作用 |
| --- | --- | --- |
| `0x81c00000` | `0` | 保留 |
| `0x81c00004` | `0x8029c0a0` | GUI |
| `0x81c00008` | `0x8029c010` | FS |
| `0x81c0000c` | `0x8029bea0` | SYS |
| `0x81c00010` | `0x8029c8dc` | MEM |
| `0x81c00014` | `0x8029bf70` | RES |
| `0x81c00018` | `0` | 保留 |
| `0x81c0001c` | `0x800616c4` | 与 MEM 首项相同 |

服务表相邻边界与最后非空槽也已核对：

| 表 | 物理区域字节数 | 最后非空 offset | 调试 call 接受的 offset 上界（不含） |
| --- | ---: | ---: | ---: |
| GUI | `0x820` | `0x81c` | `0x820` |
| FS | `0x90` | `0x80` | `0x84` |
| SYS | `0xd0` | `0xc8` | `0xcc` |
| MEM | `0x30` | `0x2c` | `0x30` |
| RES | `0xa0` | `0x98` | `0x9c` |

GUI 内部 +0x1c0/+0x538/+0x53c/+0x5c8 为空；RES 也有内部空槽。表范围有效不代表
该槽可以调用，必须继续检查非空、对齐和函数指针范围。MEM+0x30 为终止 0，后面是字符串。

MEM+8 直接指向全局 allocator `0x80007688`，MEM+C 为 `0x80006844`，两者共用
`0x804a99f0` 的 semaphore。不要把 FS+4 的 `0x80183b60` 当作 allocator；它虽然具有
相同编译后函数形状，却是文件关闭路径，底层调用完全不同。

## RTOS 与驻留生命周期

| 功能 | 地址 | 依据 |
| --- | --- | --- |
| OSTaskCreateExt | `0x8000c0fc` | 参数、priority 范围、TCB 初始化与 H1 完整对应 |
| OSTaskCreate | `0x8000bfb0` | 静态调用创建 priority 7、8、9 |
| OSTaskDel | `0x8000c280` | 删除分支和调度调用 |
| OSTimeDly | `0x8000ce9c` | 移除 ready bit，TCB+0x30 存 delay |
| OSTimeDlyResume | `0x8000cdb0` | 清 delay、重入 ready 表、调度 |
| OS TCB 初始化 | `0x80008394` | 保留扩展指针、stack 元信息 |
| CPU stack 初始化 | `0x80007a40` | 与 H1 逐条标准化指令匹配 |
| OS 调度 | `0x80008130` | ready group/table、任务切换 |
| priority -> TCB 表 | `0x806fb950` | CreateExt 和 Resume 引用一致 |
| current TCB | `0x804a9a40` | OSTimeDly 读取 |
| OSRunning byte | `0x804a99f9` | CreateExt 的启动调度判断 |
| OSLockNesting byte | `0x804a99f8` | scheduler 条件 |
| OSIntNesting byte | `0x804a9a3d` | scheduler 与 TaskDel 条件 |

TCB ABI 与 H1 一致：+4 为 OSTaskCreateExt 的扩展指针、+8 stack bottom、+0x0c
stack word count、+0x30 delay、+0x35 priority。本实现仍需动态检查 priority 6 空闲，
不能只根据固件默认创建任务集合判断。

TCU1 IRQ handler 为 `0x80134d90`，更新 `0x804a9bc4` 后处理原生键盘/GUI housekeeping。
raw tick getter `0x80134fb0` 读取该计数。IRQ 注册器 `0x800050a8` 使用八字节条目的表
`0x804aa1a4`；IRQ22 对应 slot `0x804aa254`，下一 word 为 argument（原始值 0）。
`0x80134ed4` 的初始化注册 handler，比较值约由 750020 / 40 得到；所以原生 tick 约为
40 Hz，未由真机测量。不要把 H1 客户端旧 ABI 名称 tick_80hz 当作实际频率。

驻留服务可链入原生 IRQ handler，再仅唤醒自己的 delay，保留系统原来的计时/输入行为。
没有证据支持擅自开启一个之前关闭的系统全局 OSTimeTick。实际 OSTimeTick 位于
`0x80007ed4`，唯一静态调用来自 TCU0 handler `0x800058d0`；TCU0 初始化
`0x800058ec` 没有发现直接调用或数据指针引用，符合 H1 的 dormant 路径。不能把
这个静态结论当作所有启动场景均关闭 TCU0 的真机观测。

## USB / GPIO

UDC 原生代码直接使用 `0xb3040000`；USB IRQ 为 24（mask `0x01000000`）。
`0x8000ec0c` 注册 IRQ worker `0x8000ed6c`，后者 mask IRQ24 并 post
`*(void **)0x804a9a5c` semaphore。USB task `0x80009864` 阻塞在该 semaphore，
醒来才执行 `0x8000e124` 处理控制器并重新 unmask。没有发现这个 task 的独立 VBUS 轮询。

native endpoint init `0x8000dbb0` 设置 TX/RX/USB 中断和 POWER，不能沿用 H1 initializer
的首条 `addiu sp` 指纹。`0x8000ec78` 通过 CLKGR `0xb0000020` 清 bit11，OPCR
`0xb0000024` 置 bit6，再启用 UDC。

9688 不是 H1 的 PD6 cable-detect 布线。原生外接电源检测读取 PB18：
`0xb0010100 & 0x00040000`；`0x8006335c` 用 IRQ98（48+50）注册 `0x80063270`。
高电平在调试器启动前按“仍有外部连接”拒绝控制器接管；低电平并不能单独证明没有 USB
数据线。PB18 与 USB VBUS 是否实际共线，需要真机确认，profile 明确保留此限制。
普通 GPIO IRQ ack `0x80004ee8` 使用 `GPIO bank + 0x14`，PB18 因此为 `0xb0010114`。

独占模式必须 mask IRQ24，关闭 native UDC interrupts，并持续检查所有权；只 mask PB18
不能替代这些操作。若启动前原生 USB semaphore 已 pending，仍可能有一次原生任务运行，
所有权故障必须停用调试器，不能盲重试接管。

## LCD

LCD controller `0xb3050000`。`0x80010cf4` 从以下原生参数构建 DMA descriptor：

| 参数 | 地址 | 原始值 |
| --- | --- | --- |
| width | `0x803575b4` | 240 |
| height | `0x803575b8` | 320 |
| bits | `0x803575bc` | 16 |

`0x80010c48` / `0x80010ce8` 写 DA0（LCDC+0x40）。原生 framebuffer 是纵向
240×320 RGB565；桌面呈现可以转为横向，但截图应保留物理原尺寸并明确方向。
GUI device 描述表 `0x8049fad0` 也包含相同的 240/320/16 元组。

## 守卫与验证边界

JSON 记录了 RTOS、allocator、IRQ、USB、BDA 参数和 LCD 相关选定原厂区域的 CRC32。
运行期守卫只用作兼容性拒绝机制，不是整个固件的密码学认证。构建前可对用户本地固件
执行完整 SHA256 验证。此 JZ4740 profile 对 JZ4730、其他固件版本和 H1 都应拒绝启动。

本地静态检查不能证明 CDC 枚举、吞吐、插拔恢复、供电状态、驻留/前台并发、物理触控或
按键注入已经可用。必须以真机握手、原尺寸截图和关闭恢复磁盘的实际结果补充。
