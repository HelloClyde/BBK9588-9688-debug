# 9588 Debug 通用 BDA

从同仓库的 GPL-3.0-only 9688 Debug 移植，默认只生成一个 `Debug9588.bda`。启动时根据五张服务表和原厂代码CRC，自动选择唯一匹配的固件绑定，仅装载对应芯片的一份驻留程序。

## 已核对的绑定

| 芯片 | 内核显示版本 | 原厂输入 | USB |
|---|---|---|---|
| JZ4740 | V3.30 | C200.bin / kj409588.bin | MUSB，PB18高有效 |
| JZ4730 | V1.30 | C200knl.bin | 独立FIFO UDC，GP89低有效、PA29控制连接 |
| JZ4720 | V3.2 | 4720knl.bin | MUSB，PB18低有效 |

这是不同内核分支，不能仅凭版本数字比较兼容性。匹配的raw SHA256、地址与守卫见 `profiles/9588-jz4740.json`、`9588-jz4730.json`、`9588-jz4720.json`。`verified=true` 指静态核对，不能代替真机验收。

**三种芯片都尚未完成9588真机USB验收。** MIPS/控制器模型、原始固件检查和完整内核入口诊断分别记录。JZ4730/JZ4720没有用其他机器的截图或完整系统结果代替。

## 构建

在独立仓库根目录运行：

```powershell
git submodule update --init sdk
python -m pip install -r requirements-dev.txt
.\sdk\scripts\setup_toolchain.ps1
python build.py --models 9588 --verify
```

固定SDK为 `ef3bf3b7328a3a8fa9ba48593c63285300725fb7`。默认产物 `build/9588-debug/Debug9588.bda`，菜单名“9588调试”。不需要原厂BDA模板或固件即可构建；已有工具链可在根构建命令传 `--toolchain PATH/TO/bin`。独立开发目录可向内层构建器传 `--sdk-root PATH/TO/SDK`。

可选本地ROM核对，分别提供三份原厂输入，仅读取、不分发：

```powershell
python build.py --models 9588 --verify --firmware-9588-jz4740 PATH/TO/kj409588.bin --firmware-9588-jz4730 PATH/TO/C200knl.bin --firmware-9588-jz4720 PATH/TO/4720knl.bin
```

内层 `9588-debug/build.py` 与 `verify.py` 对应参数为 `--firmware-jz4740/4730/4720`。仅开发比较时才用 `--chip jz4740`、`jz4730` 或 `jz4720` 生成单芯片目录；该模式用 `--firmware`，验证时也要明确 `--chip`。正常安装使用默认通用文件。

模型执行真实重定位resident、USB寄存器/FIFO、协议与CRC、RAM保护、EXEC、PC客户端到MIPS端到端、LCD DMA、RTOS O32与通用启动器。通用启动器验证三种固件选择、跨芯片拒绝、每段守卫、导入表污染、歧义与重复分配。USB模型还覆盖停钟后的frame/poll/收发/停止清理边界，4730另覆盖PA29连接顺序、相关位恢复和所有权丢失。未提供ROM时，原厂代码检查明确跳过。

本次完整ROM模型共211项通过；完整原始C200内核中的启动/停止/复用和实际截图诊断仅覆盖JZ4740，见 [确切通用BDA记录](qemu-validation.md)。真机USB状态仍为未验收。

[4730依据](firmware-jz4730-notes.md)、[4720依据](firmware-jz4720-notes.md) 与 [4740依据](firmware-notes.md) 分别记录地址来源。可选原生4730指令验证：

```powershell
python 9588-debug/validation/native_jz4730.py PATH/TO/C200knl.bin --output build/9588-native-jz4730.json
```

该脚本执行原ROM的USB/IRQ/GPIO/LCD初始化及版本相关函数，外部边界stub在输出中列明。它不模拟物理USB ACK或完整OS。

## 安装与客户端

将通用BDA复制到设备 `应用/程序/Debug9588.bda`。安全移除并拔掉USB，运行“9588调试”，按提示启动并返回菜单，再接USB。

| 芯片 | 实验性VID:PID | 精确握手 |
|---|---|---|
| JZ4740 | 07C4:A4A9 | 9588DEBUG/1 JZ4740 RESIDENT |
| JZ4730 | 07C4:A4AA | 9588DEBUG/1 JZ4730 RESIDENT |
| JZ4720 | 07C4:A4AB | 9588DEBUG/1 JZ4720 RESIDENT |

同一个PC客户端自动枚举这三种设备，PID与握手交叉检查；不会选择9688的A4A7/A4A8。超时、短写或坏CRC后会废弃会话，不自动重发写入或执行。

```powershell
python 9588-debug/client.py list
python 9588-debug/client.py status
python 9588-debug/client.py --chip jz4730 status
python 9588-debug/client.py screenshot build/9588-screen.png
python 9588-debug/client.py screenshot build/native.png --native-orientation
python 9588-debug/client.py exec run 9588-debug/examples/compute.c --directory build/9588-compute --args-hex 616263
python 9588-debug/client.py stop
```

三个芯片的LCD扫描尺寸都为240×320。客户端默认180°显示方向，`--native-orientation`保留扫描方向，`--rotate`显式覆盖；真机方向仍待核对。RAM模块ABI字段保持v1，入口为 `debug9588_entry`；固件专用API仍需相应绑定，不能因此混用9688媒体模块。

再次打开同一BDA或执行stop请求停止。相同resident构建复用原堆块和休眠任务；替换版本前重启。未匹配或歧义固件不会启用调试；只有已确认GUI可安全调用时才显示诊断。

## 固件依赖与许可

汇编也必须拿到正确的函数、结构和寄存器地址。当前后台模式调用原厂调度器、全局堆与中断服务，相关布局随固件分支变化；通用文件负责自动选取已核对的绑定。详见 [固件兼容说明](../docs/FIRMWARE-COMPATIBILITY.md)。

代码采用GPL-3.0-only，保留H1Debug及9688实现来源，SDK选择兼容的Apache-2.0许可路径。原厂固件、NAND、手册、用户文件及私有媒体不分发。
