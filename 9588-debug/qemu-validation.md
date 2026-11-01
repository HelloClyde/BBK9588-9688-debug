# 9588 V3.30 / JZ4740 完整内核入口诊断

精确 BDA：64,696 字节，SHA256 `6cdcee4bb6aafc88571338281ebf3891018f095532d9e3675565c13791710e62`，image id `34cf7d3b`。

在原始 C200 内核下完成 **启动 → 停止 → 复用启动**，三次 `bda_main` 均返回0，GUI任务的TCB、SP和保存寄存器恢复。原生堆分配、priority6任务、tick IRQ、USB时钟及GPIO使用真实固件服务，没有改写固件代码、CRC守卫、TCB、semaphore或板级状态来让测试通过。停止后的IRQ/OPCR等快照与启动前相同，重开复用同一分配与TCB。

机器可读证据见 [JSON记录](../docs/validation/9588-jz4740-qemu.json)，其中绑定BDA、原厂C200、固定SDK和实际QEMU可执行文件的SHA256。

## 验证方法

使用 QEMU 11.0.0 本地构建、bbk9588 machine、32 MiB、24Kf，直接加载未修改C200，固件补丁为空，NAND使用独立运行副本。该本地QEMU二进制与仓库旧overlay存在差异；记录其确切散列，不能声称公开CI复现了完整系统测试。公开CI只运行独立MIPS/控制器模型。

这是 **GDB入口诊断**：按原生BDA约定复制ROM服务seed和真实BDA payload，在真实GUI任务9的GetMessage入口保存上下文，调用 `0x81c00020`，以BDA窗口未使用地址作返回断点，再恢复上下文。原生桌面、调度、堆和中断继续执行。未覆盖普通桌面菜单/FAT加载路径。

| 项目 | 首次启动 | 第二次停止 | 第三次复用 |
|---|---|---|---|
| 状态 | RUNNING | STOPPED | RUNNING |
| 错误 | 0 | 0 | 0 |
| 控制块 | `0x80df0a80` | 相同 | 相同 |
| priority6 TCB | `0x806c55a0` | 相同 | 相同 |
| tick IRQ | 驻留handler | 原生 `0x8012bb90` | 驻留handler |

## 实际驻留截图函数

调用同版resident中重定位后的 `rd_screen_capture`，返回0，取得原LCD `0xa1f82000` 的240×320、RGB565、stride480、153,600字节快照。随后用实际PC客户端 `Client.screenshot()` 解码；传输adapter提供上述已复制像素，默认180°整数转置，没有插值或生成内容。这验证了截图复制、解码和显示方向，未经过USB传输。

![9588 C200 模拟器驻留截图，客户端显示方向](../docs/assets/9588-qemu/final-resident-lcd-display.png)

JSON内的图片路径以仓库根目录为基准，原始扫描方向、客户端原始方向和三个生命周期阶段的画面一并保留。

本轮未验证物理9588、USB CDC枚举/ACK/重传/插拔、停止后的真机磁盘重枚举、冷BootROM/NAND启动或普通菜单加载。专用模拟器已停止，未操作其他实例或源NAND。
