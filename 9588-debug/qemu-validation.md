# 9588 通用 BDA：完整 C200 内核入口诊断

最终通用 `Debug9588.bda` 为113,048字节，SHA256 `3db418d93ae7ce09e41f1030967609d6ebce91972cf3226b6de009cc9105a054`。同一个文件包含JZ4740/JZ4730/JZ4720三份resident，本次完整内核运行实际选择 **JZ4740**，image id `cf2137f9`。4730/4720完整内核没有在此模拟器中运行。

在未修改的原厂 C200 V3.30 下完成 **启动 → 停止 → 复用启动**。三次bda_main返回0，原GUI任务、SP和寄存器恢复；相同堆分配、控制块和priority6任务被复用，错误均为0。停止后的tick/USB IRQ、GPIO、时钟及PHY状态与启动前相同。

[机器可读记录](../docs/validation/9588-universal-qemu.json)绑定确切BDA、原厂内核、固定SDK和本地QEMU二进制散列。它与 [三芯片211项模型记录](../docs/validation/9588-universal-models.json) 分开，不扩大真机覆盖。

## 方法与范围

专用bbk9588 QEMU、24Kf、32MiB，直接启动原始C200，固件补丁为空；NAND使用独立运行副本。通过模拟GPIO完成原生触屏校准和对话框关闭，没有改写TCB、semaphore、原厂指令、CRC守卫或板级寄存器来迎合测试。

这是明确的 **GDB BDA入口诊断**：按原厂约定复制服务seed和真实payload，在GUI任务9的GetMessage入口保存上下文，调用 `0x81c00020`，从未使用的BDA窗口返回断点恢复上下文。没有覆盖普通桌面/FAT菜单加载。该本地QEMU与旧overlay存在差异，完整系统诊断不在公开CI中重跑；公开CI使用MIPS/控制器模型。

| 项目 | 启动 | 停止 | 复用 |
|---|---|---|---|
| 状态 | RUNNING | STOPPED | RUNNING |
| 返回值/错误 | 0 / 0 | 0 / 0 | 0 / 0 |
| 控制块 | `0x80df0a80` | 相同 | 相同 |
| priority6 TCB | `0x806c55a0` | 相同 | 相同 |
| tick IRQ | resident handler | 原生 `0x8012bb90` | resident handler |

## 实际截图接口

调用真正重定位的 `rd_screen_capture`，返回0并复制LCD `0xa1f82000` 的240×320、RGB565、stride480、153600字节。随后使用实际PC客户端解码和默认180°整数旋转；adapter提供实际复制像素，没有经过USB主机传输，也没有插值或生成内容。

![通用BDA在C200下取得的LCD快照](../docs/assets/9588-universal-qemu/universal-lcd-display.png)

专用模拟器已停止，未操作其他实例或源NAND。物理9588、CDC枚举/ACK/重传/收发、停止后的真机磁盘恢复、冷NAND启动与普通菜单加载仍未验收。原alpha.2单芯片记录保留在 `docs/validation/9588-jz4740-qemu.json`，不能代替本次确切通用BDA记录。
