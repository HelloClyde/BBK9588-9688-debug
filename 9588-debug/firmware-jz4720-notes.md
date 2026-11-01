# BBK 9588 JZ4720：原厂内核静态绑定

本记录对应 `jz4720-profile.json` 的确切输入。RTOS、服务表、USB 控制器寄存器访问、电源 GPIO、LCD 与原厂内存测试范围均从 **4720 输入本身**核对；相似 4740 函数只用于定位候选。没有连接设备、运行完整 4720 系统或验证 USB 主机通信。JSON 的 `verified: true` 只表示静态绑定和 CRC 已核对。

## 输入和版本

| 输入 | 字节数 | SHA256 |
|---|---:|---|
| 原始 `C200_4720.bin` | 4662392 | `469a833e8984e8c4c531a411955edc38f5fb57c6089b8694f522f995dbe66c49` |
| `4720knl.bin` | 4662456 | `5bfef4a614e2a519c3957aa056564521ae7c8cb006ccc7a95e4b00ab1bf80e4f` |
| `u_boot_9588_4720.bin` | 377456 | `8432d1d3a0a9b0669ac256af6dfd7b04afffdf284747476ff47e9e35cd4099ea` |

恢复包目录标为 **9588 V3.30**，其中选定的 4720 内核实际版本页为 **V3.2**：

- `0x8027ddd0`：内部构建 `2009-02-24 09:58:59`。
- `0x8027def4`：`9588v3.2释放日期`；`0x80025d34..0x80025d50` 将内部构建与此标题交给消息框。
- `0x8027df74`：GBK `版本号：V3.2`，`0x80025f28..0x80025f34` 将该字符串交给实际版本页绘制函数。不是孤立、未引用的字符串。
- 没有在所选 raw 中找到 `V3.30`。因此 profile 的 `firmware_version` 是 `V3.2`，恢复包来源另列。

`4720knl.bin` 是 64 字节封装加原始 payload，没有压缩或加密。解码头标志为 `4720KERNEL`，长度、payload 求和 `0x15d0e65e`、头求和 `0x1083` 均通过现有封装校验器。恢复包的独立 raw、封装内 payload 与工作目录的封装 payload 逐字节一致。

## 地址空间与堆

- raw 加载地址 `0x80004000`，file-backed 末尾为 `0x80476478`，exclusive。
- 启动代码 `0x80004064..0x8000407c` 清零 BSS `0x80476478..0x808761c0`，包含最后一个 word。
- `0x80007450` 原生堆初始化将 `(0x808861c0 & 0xffffc000) = 0x80884000` 与 `0x81c00000` 传给 `0x8000768c`。堆范围是 `[0x80884000, 0x81c00000)`；不能把 `FW_SDRAM_START` 误写成堆起点。
- 全局分配/释放为 `0x80007708` / `0x800068b4`，二者均通过 `0x804764b0` 的信号量保护。它们正是 MEM 表 `+8` / `+0xc`。MEM `+0` 的应用分配器不是本调试驻留需要的全局堆入口。
- BDA 加载器在 `0x8002e3ac..0x8002e3e4` 将正文装入并执行 `0x81c00020`；头的 32 字节服务表种子另行复制到 `0x81c00000`。

原厂 4720 U-Boot 可独立证明工厂软件采用 **32 MiB RAM 窗口**。其自举头和内部绝对跳转确定加载地址为 `0x80900000`。工厂 SDRAM 测试函数 `0x80912a64` 从 `0x80000000` 起逐 word 写入/读回 `0x55555555`、`0xaaaaaaaa` 后恢复原值，跳过自身代码区域，`0x80912b8c..0x80912ba8` 用 `0x81ffffff` 限制末地址。因此 `FW_SDRAM_END=0x82000000`。对应文件范围 `0x12a64 + 0x30c` 的 CRC 在 JSON 中独立记录。

本次未执行该工厂测试，恢复包也没有独立 4720 SDRAM 初始化 loader；故这不是本次测得的物理容量，也没有套用 4740 loader 的 DMCR 几何。LCD 原生缓冲区也落在此窗口内。

## RTOS 和人工唤醒

| 功能或状态 | 4720 地址 |
|---|---|
| OSTaskCreate / OSTaskCreateExt | `0x8000c050` / `0x8000c19c` |
| delete / delay / resume | `0x8000c320` / `0x8000cf3c` / `0x8000ce50` |
| 优先级任务表，64 项 | `0x806c8360` |
| 当前 TCB | `0x80476500` |
| OS running / scheduler lock / interrupt nesting | `0x804764b9` / `0x804764b8` / `0x804764fd` |
| TCB 初始化 / 调度器 | `0x80008414` / `0x800081b0` |
| IRQ 保存关闭 / 恢复 | `0x800a9df0` / `0x800a9e30` |
| IRQ 注册函数 / 默认 handler | `0x80005118` / `0x80005354` |
| IRQ handler+argument 表 | `0x80476c24`，8 字节一项 |
| raw tick getter / counter | `0x8012da30` / `0x80476634` |
| TCU1 IRQ22 handler / slot | `0x8012d810` / `0x80476cd4` |

`OSTaskCreateExt` 明确检查优先级 `<64`，用 `0x806c8360 + 4*priority` 检查空槽并先写入保留值 1。TCB 初始化的实际 store 证明以下偏移：保存 SP `+0`、扩展参数 `+4`、栈底 `+8`、栈 word 数 `+0xc`、delay `+0x30`、status `+0x34`、priority `+0x35`。延时/恢复函数与调度器也独立使用对应字段。

遍历整个原始内核的两种任务创建直接调用，只有五个 callsite：`0x800099dc/0x800099f8/0x80009a14` 创建 7/8/9；`0x800087b8/0x800087f0` 创建 idle/stat 63/62。静态创建中没有 6。运行期仍必须检查 slot6 为空；本次未观察完整 4720 系统运行后的任务表。

TCU1 初始化 `0x8012d954` 选择外部时钟/16，`750020 / 40` 的整数计数是 18750，原生 tick 频率为 40 Hz。`0x8012da00..0x8012da20` 将 IRQ22、handler `0x8012d810`、**argument=0** 传给 IRQ 注册。handler 增加的是 `0x80476634`，不是 GUI 事件时间 `0x804765f8`。需要保留原 handler 链，调试驻留自己的人工 wake 不能替换原生计时逻辑。slot `+4` 必须仍为 0，TCB 元数据与任务优先级也必须运行期检查。

## BDA 种子与五张表

`0x8002cef8..0x8002cf28` 精确复制 `0x80283960` 的 8 个 word 到 `0x81c00000`。种子布局是 `0, GUI, FS, SYS, MEM, RES, 0, MEM[0]`；最后一项位于邻接 MEM 表起点，并不是第六张表。

| 表 | ROM 基址 | 物理字节数 | 调用 offset exclusive 上限 |
|---|---|---:|---:|
| GUI | `0x80283130` | `0x830` | `0x824` |
| FS | `0x802830a0` | `0x90` | `0x84` |
| SYS | `0x80282f30` | `0xd0` | `0xcc` |
| MEM | `0x8028397c` | `0x34` | `0x30` |
| RES | `0x80283000` | `0xa0` | `0x9c` |

FS 最后有效槽 `+0x80`，后面有 3 个零 word；SYS `+0xcc`、MEM `+0x30`、RES `+0x9c` 是结束零槽。RES 中间 `+0x74..+0x88` 有空洞，所以边界检查之外仍应拒绝空函数指针。GUI 最后一槽是 `+0x820 = 0x8005df84`，随后 `+0x824/+0x828/+0x82c` 为零，再开始种子；这比所参考的 4740 表多一个有效槽。上述物理范围全部有 CRC。

GUI `+0x2b8` 的实际 word 为 `0x800c8244`，给 notice 使用；单独守卫该槽和函数前 `0x80` 字节。运行期还要确认 `0x81c00004/08/0c/10/14` 分别等于以上五个 ROM 表；只有种子 CRC 正确不能证明 runtime 表没有被更改。

## USB：MUSB，PB18 是低有效

控制器族依据 **4720 native driver 自身的访问**，不是由型号命名推定：

- MMIO `0xb3040000`；byte FADDR `+0`、POWER `+1`、USB status/enable `+0xa/+0xb`、INDEX `+0xe`。
- indexed 16-bit TXMAXP/TXCSR/RXMAXP/RXCSR/RXCOUNT `+0x10/+0x12/+0x14/+0x16/+0x18`，FIFO `+0x20+4*ep`；DMA channel registers `+0x204..+0x21c`。原生 PIO 代码还直接使用 FIFO byte/word 访问。
- `0x8000ec8c` 注册 IRQ24，handler `0x8000eddc`，argument0，slot `0x80476ce4`。handler 向 INTC `0xb0001008` 写 `1<<24` 后投递 USB task 信号量 `0x8047651c`。
- enable `0x8000ecf8` 清 `CLKGR=0xb0000020` 的 `0x800`，置 `OPCR=0xb0000024` 的 `0x40`，POWER 写 `0x60`。disable `0x8000ed78` 反向操作。CPCCR/CPPCR 为 `0xb0000000` / `0xb0000010`。
- `0x8000dc30..0x8000ddec` 是完整复位/端点配置函数。它清 FADDR 和控制器中断、配置 EP0/EP1，按 POWER.HSMODE 分支将 bulk max packet 设为 FS64 或 HS512，最后清 DMA 地址/计数/control。它与 4740 的独立 endpoint helper 不同，不能把 `4740地址+固定偏移` 当作该函数。

因此 JSON 的驱动族选择为 `usb_controller: jz4740-musb`。该名字表示所需寄存器族，不表示原厂 4740 和 4720 的行为处处相同。

**PB18 极性必须单独处理。** `0x8005bc48` 读取 `0xb0010100 & 0x40000`，在返回 delay slot 用 `sltiu(result,1)` 取反，即 **低电平为1**。`0x8005d4d4` 将该结果变为电源标志 `0x80`。USB 窗口关闭路径 `0x80025240..0x80025290` 在 `0x80025258` 读取该标志；仍为1时不关闭窗口，变为0时才调用 USB disable。此证据支持 `FW_USB_CABLE_ACTIVE_LOW=1`；照搬 4740 的高电平检测会把本机启动条件反过来。

`0x8005be70..0x8005bf00` 明确配置 PB18：GPIO bank stride `0x100`，mask set/clear `+0x24/+0x28`，ack `+0x14`，flag `+0x80`；调用 IRQ 注册的编号为 `0x62`，handler `0x8005be00`，argument `0x32`。handler 自己加 `0x30` 恢复 GPIO IRQ编号98，并进行电源消抖。这是共享电源服务，不是 4740 的 `UDC_CONNECT` handler。

限制：该查询也被电池/外接电源逻辑调用，无法仅从这些代码证明 PB18 是专用 USB VBUS 引脚；应按“USB/外接电源已连接则拒绝抢占”的保守条件处理。屏蔽 GPIO IRQ 也不能证明原生任务永远不轮询电源状态，仍需 controller ownership 检查和失去所有权后的安全停止。

4720 原生驱动只配置 bulk EP1，EP2 IN 另外通过 **JZ4720 实际板级工程**确认，而不是套用 4740 手册：

1. Qi Hardware 的 [Ben NanoNote 原始手册](https://git.wut.ee/qi-hardware/ben-nanonote/raw/commit/9e5d258308f68ed2538bbba08f25ff10f8ce3bdd/manual/manual.sla)，固定 commit `9e5d258308f68ed2538bbba08f25ff10f8ce3bdd` 的第908行，明确该板 CPU 是 JZ4720。
2. Qi OpenWrt 固定 commit `5938c827edeb1bab6471384cdb67d06dbb393f24` 的 [qi_lb60 板配置](https://git.wut.ee/qi-hardware/openwrt-xburst/raw/commit/5938c827edeb1bab6471384cdb67d06dbb393f24/target/linux/xburst/qi_lb60/config-3.3) 第35行实际启用 `CONFIG_USB_JZ4740=y`；该子板的 target.mk 将板名标为 Ben NanoNote。
3. 同工程选中的 [Ingenic UDC 补丁](https://git.wut.ee/qi-hardware/openwrt-xburst/raw/commit/5938c827edeb1bab6471384cdb67d06dbb393f24/target/linux/xburst/patches-3.3/0002-Add-jz4740-udc-driver.patch)，第2090..2104行实际 endpoint 表包含 EP2 IN interrupt，地址 `0x82`，FIFO(2)。第2293行 interrupt 最大包为64字节；第135行 FIFO 计算为 `4*ep+0x20`，因此 EP2 FIFO 是控制器 `+0x28`。这覆盖当前 CDC 16字节 EP2 IN 通知需求。补丁开头的概述注释有端点编号反写，结论取实际 endpoint 表与中断处理实现。

以上来源的 SHA256 记入 JSON。此链确认所需 EP2 IN 能力，不声称 EP2 OUT/bulk；也不能替代 BBK 4720 的板级 GPIO、reset 细节、完整 CDC 枚举及流量验证。不能把软件模型通过等同于真机已测。

## LCD

几何 ROM `0x8033e444/448/44c` 实际为 `240/320/16`，LCD 基址 `0xb3050000`。初始化 `0x80010bac` 配置缓存地址 `0x81f91000` palette、`0x81f92000` framebuffer，非缓存别名 `0xa1f91000/0xa1f92000`。它以 IRQ30、handler `0x80011a50`、argument0 注册。

16bpp 路径将 DA0 `LCD+0x40` 指向物理 `0x0047a2b0`。构造函数 `0x80010d64` 在 `0x8047a2b0` 写 16 字节 DMA descriptor：next 指向自身、source=`0x01f92000`、word length=`38400`，command 高位 `0xc0000000`。CTRL 为 `LCD+0x30`，启动位 bit3。framebuffer 长度是 `240*320*2=153600`。这与本机实际机器码对应，可供现有屏幕抓取路径的布局检查；没有取得 4720 真机截图。

## 守卫与验证边界

JSON 包含 44 个 ROM 范围 CRC 和 2 个 notice CRC，覆盖上述核心代码、完整导出表、种子、几何、版本及低有效电源路径。CRC 范围均落在 raw file-backed 区间；U-Boot 测试范围的 CRC 独立归档，不会被当成运行时 kernel ROM 守卫。

本研究只写入研究输出目录。未修改原厂二进制、debug 源码或开源仓库。未刷机、未访问真机、未启动或操作 QEMU。profile 可供真实 MIPS 模型及源码适配；完整系统/枚举/USB traffic/物理 GPIO/截图仍未验证。
