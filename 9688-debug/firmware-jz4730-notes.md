# 9688 V2.32 / JZ4730 固件定位证据

本 profile 针对恢复包的 **JZ4730** 内核，地址均从该映像重新定位。
这里记录静态证据；未操作物理设备，不能据此宣称 CDC/插拔/后台运行已经真机通过。

## 身份、BDA 与内存

| 输入 | 字节数 | SHA256 |
| --- | ---: | --- |
| `底层升级4730/C100.bin` | 4871720 | `b18ce485a6ca6a8ea9ef5e3963d5970188ff7a2aeb4a6d4e58fe43625f49e514` |
| `系统数据/系统/数据/C100knl.bin` | 4871784 | `a093a22ce5aa5a77d3fd1a3ee4153542e34dbcb9d4c12cb107adc210c5249dc3` |

`C100knl.bin[64:]` 与 JZ4730 raw 映像完全逐字节相同。raw 基址 `0x80004000`。
原生 BDA 参数/代码地址仍是 `0x81c00000` / `0x81c00020`。
全局堆初始化 `0x80006c80/84` 明确传入结束地址 `0x81c00000`，本程序把 KSEG0
RAM 边界约束为 `[0x80004000,0x82000000)`；不使用 H1 的 64 MiB 范围。

参数复制函数 `0x800203a0` 从 seed `0x8025dcc0` 复制八个 word：

| 参数 | 值 |
| --- | --- |
| reserved | `0` |
| GUI | `0x8025d4a0` |
| FS | `0x8025d410` |
| SYS | `0x8025d2a0` |
| MEM | `0x8025dcdc` |
| RES | `0x8025d370` |
| reserved | `0` |
| MEM 首项别名 | `0x80023544` |

GUI/FS/SYS/RES 的物理跨度仍分别是 `0x820/0x90/0xd0/0xa0`。
最后非空槽分别是 `0x81c/0x80/0xc8/0x98`；MEM 是 12 个有效 word（`0x30` 字节），
最后一项 `+0x2c = 0x80023888`，之后是终止零和字符串。
GUI +0x1c0/+0x538/+0x53c/+0x5c8 为空；RES 内部也有空槽。
因此 table call 的 offset 上界（不含）仍可用 `0x820/0x84/0xcc/0x30/0x9c`，
但每次仍需检查指针非空、对齐和代码地址范围。

MEM+8 = `0x80006eb8` 全局 allocator；MEM+C = `0x80006074` free。
二者共用 semaphore `0x804a9660`，调用关系和 RTOS 包装已独立核对。
FS+4 = `0x80183300` 是文件关闭路径，不是 allocator，不能因为函数形状相似而混用。

## RTOS、TCB 与唤醒

| 功能 | JZ4730 地址 |
| --- | --- |
| OSTaskCreateExt | `0x8000f89c` |
| OSTaskCreate | `0x8000f750` |
| OSTaskDel | `0x8000fa20` |
| OSTimeDly | `0x8001063c` |
| OSTimeDlyResume | `0x80010550` |
| 初始 CPU stack frame | `0x8000ada0` |
| TCB 初始化 | `0x8000b6f4` |
| scheduler | `0x8000b490` |
| priority -> TCB 表 | `0x808af4e0` |
| current TCB | `0x804a96e4` |
| OSRunning byte | `0x804a9679` |
| OSLockNesting byte | `0x804a9678` |
| OSIntNesting byte | `0x804a96e1` |

这些 RTOS 函数与 JZ4740 对应函数具有逐条匹配的标准化 MIPS 指令，且所有新全局地址
均从本机的 CreateExt/Delay/Resume/scheduler 指令重新读取，没有假定统一地址差。
TCB+4 是扩展指针，+8/+0xc 为 stack bottom/word count，+0x30 delay，+0x35 priority，
布局与现有驻留代码一致。默认普通任务创建在 `0x8000ce90/ceac/cec8` 分别使用 7/8/9；
idle/stat 为 0x3f/0x3e。priority 6 仍必须运行时检查是否空闲。

JZ4730 使用 OST1 IRQ23：

- 原始 handler `0x80134e0c`，先更新 tick `0x804a989c`，再进行键盘/GUI housekeeping。
- raw tick getter `0x80134f48`。
- init `0x80134eb4` 给 `0x80004a34` 传入 IRQ23、handler、argument 0。
- IRQ 表基址 `0x804a9e64`，每项仍为 `(handler, argument)` 两个 word；
  IRQ23 slot 为 `0x804a9f1c`，+4 为 0。
- OST1 寄存器为 `0xb0002038`、`0xb0002030`、`0xb0002034`；
  init 设置 12000020/40 的 reload，原生 tick 约 40 Hz，未真机测量。

故 `firmware.c` 可保留原 handler 链和仅唤醒自身任务的做法，换 profile 地址即可。
OSTimeTick `0x8000b234` 中有递减 TCB+0x30 的实现，但没有发现任何直接 jal 调用。
不能因为它存在就擅自启用系统全局 RTOS tick。

## LCD

LCD controller 仍是 `0xb3050000`，CTRL+0x30 的 bit3 表示 enabled，DA0+0x40。
`0x80007398` / `0x80007438` 写 DA0；`0x80007444..0x80007588` 构建 16 字节
DMA descriptor，其布局依然是 next/source/id/command。
RGB565 情况选择数据 descriptor 自环，command 低 24 位为传输 word 数。

| 参数 | 地址 | 初值 |
| --- | --- | ---: |
| width | `0x8037f9d4` | 240 |
| height | `0x8037f9d8` | 320 |
| bits | `0x8037f9dc` | 16 |

GUI device 表 `0x8049f710` 也包含相同的 240/320/16 元组。
现有保守截图函数只需 profile 地址，不需复制 JZ4740 的固件地址或增加控制器布局分支。

## USB 与其他差异

JZ4730 的 UDC 基址虽同为 `0xb3040000`，但它不是 JZ4740 的 MUSB 索引寄存器模型。
原生 initializer `0x80190200..0x8019058c` 直接操作 IN/OUT endpoint bank，
`0x80190528` 通过 `0x80004a34` 注册 **IRQ12**，handler `0x8019116c`，argument 0。
USB IRQ mask 因而是 `0x00001000`，不能使用 JZ4740 的 `0x01000000`。

GPIO bank stride 在 `0x80004a34` 中算为 `bank*0x30`，而不是 JZ4740 的 0x100。
不能搬用 PB18 或 JZ4740 的 GPIO mask-set/clear 寄存器。
4730 外接电源输入为 **GP89**：`0xb0010060 & 0x02000000`。
`0x8002975c` 的 getter 对此值取逻辑非，`0x8002b1d4` 包装成电源状态；高电平表示有外接电源。
`0x80028774..0x8002881c` 配置该输入并注册 GPIO IRQ137（48+89），handler
`0x80029ad4`、argument 89。该结论来自本固件的寄存器操作与电源状态调用链，不能把
JZ4740 的 PB18 名称或地址搬来。实际是否与 USB VBUS 共线仍需要真机确认。

时钟和总线选择也不同：CLKGR/MSCR `0xb0000020` 的 USB gate 为 bit24，
OCR `0xb000001c` 的 SPEND0 为 bit6，**active 时需清 bit6**；JZ4740 则是置位。
SBA_CNTL `0xb3000000` 的 USBCFG bit7 需清零选择 device，原生 boot
`0x800045c8..0x800045d8` 有直接证据。

USB transport、GPIO/时钟的精确操作由独立 JZ4730 driver 处理；profile 的宏和 CRC
守卫共同绑定到此固件。底层 MMIO 和原生 IRQ worker 的不同，正是不能仅改握手字符串的原因。

## UDC 手册与原生传输交叉核对

本地 `Jz4730_21_udc_spec.pdf` 的寄存器表和操作流程用于补充原生代码：

- EP0 max packet 只能是 8/16/32；IN1 固定 interrupt、IN2 固定 bulk、OUT5 固定 bulk。
  CDC endpoint 描述符应相应使用 0x81 notification、0x82 bulk IN、0x05 bulk OUT。
- TX RAM 总共 64 个 word。各 IN FIFO 顺序紧邻，base 从 `0xb3040840` 起按
  UIBSRn 的 word 数累加；不使用 JZ4740 的 INDEX/FIFO 地址模型。
- RFIFO 是一个 16-word 公共 OUT FIFO，入口 `0xb3040800`；读精确 RPS 字节数。
  原生 `0x80190690` 的 TFIFO 尾字节写和 `0x80191740` 附近 RFIFO 字节读补充了
  寄存器表仅标注 32-bit 访问的不足。
- SET_ADDRESS 完全由硬件处理；SET_CONFIGURATION/SET_INTERFACE 分别触发
  UDIPR bit0/bit1，EP0 不会再给这两个请求发中断。UDSR 的 CFG/INTF/ALT 分别位于
  bits3:0/7:4/11:8，因此不能把它们当作普通软件 EP0 SETUP 再发一次 status。
- UISRn.IN bit6 表示收到 IN token，软件写 0 清；UIPR 的 IN pending bit 则写 1 清。
  它不是一个有独立定义的 FIFO-empty/ACK-complete 位。
- 原生 `0x80190bcc..0x80190cf8` 提交数据后轮询 UIPR.IN2 或 UISR2.IN，再清这些标志。
  这支持末尾短包也可出现消费 token 事件，并不要求 host 再多发一个 IN token。
  但“clear 标志到 confirm 之间出现 IN/NAK”的物理时序仍需真机验证；主机模型不能
  单凭软件 tx_pending 状态证明硬件 FIFO 已空。
- ZLP 发送按手册顺序写 `0xb3040420`、对应 IN FIFO、再写 `0xb304041c`；
  接收到 OUT ZLP 则读 `0xb304041c` 确认。

## 验证边界

JSON 保存选定原始代码/数据区域的 CRC32；完整 raw/wrapper SHA256 可在构建前检查。
这些是兼容性守卫，不是权限隔离或异常恢复。没有从文件名推定与 JZ4740 互通。
不同 SoC profile 必须交叉拒绝，两种产物的 USB PID 和精确握手也应分别识别。

仍须真机验证：CDC 枚举、EP0 控制请求、跨包命令、DTR 取消、RAM/EXEC、原尺寸截图、
后台前台并发、停止后磁盘恢复，以及外接电源输入与 USB VBUS 的实际关系。
