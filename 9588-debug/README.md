# 9588 Debug 通用 BDA

从同仓库的 GPL-3.0-only 9688 Debug 移植，默认只生成一个 `Debug9588.bda`。启动时先匹配已核对的绑定，再尝试受约束的 MIPS 调用/数据引用图扫描；唯一且完整通过校验后，仅装载对应芯片的一份驻留程序。详见[固件兼容说明](../docs/FIRMWARE-COMPATIBILITY.md)。

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

历史 alpha.3 的完整 ROM 模型共 211 项通过；当时原始 C200 内核中的启动/停止/复用和截图诊断仅覆盖 JZ4740，见[绑定旧产物的记录](qemu-validation.md)。新构建使用自身的 verification 记录，不能借用旧 SHA 的运行证据。真机 USB 状态仍为未验收。

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


## 文件、桌面与自动测试客户端

`0.2.0-alpha.1` 增加正式管理协议；先运行 `firmware` 和 `management` 查看启动扫描结果及当前固件实际启用的能力。旧 BDA 或未通过相应 FS/GUI 指纹检查的固件会明确拒绝管理操作。各模型/固件的验证范围见 [完整工作流说明](../docs/DEBUG-WORKFLOW.md)，新增功能尚不能沿用旧 BDA 的真机验收记录。

```powershell
python 9588-debug/client.py firmware
python 9588-debug/client.py management
python 9588-debug/client.py fs list 'A:/应用/程序'
python 9588-debug/client.py fs get 'A:/应用/程序/MyApp.bda' build/MyApp-backup.bda
python 9588-debug/client.py deploy build/MyApp.bda 'A:/应用/程序/MyApp.bda'
python 9588-debug/client.py apps list
python 9588-debug/client.py apps refresh
python 9588-debug/client.py windows
python 9588-debug/client.py apps launch 'A:/应用/程序/MyApp.bda' --image-elf build/MyApp.elf
python 9588-debug/client.py screenshot build/region.png --roi 10,20,100,80
python 9588-debug/client.py screenshot build/current.png --baseline build/baseline.png
python 9588-debug/client.py logs
```

路径使用绝对 `A:/` 或 `B:/`，客户端无损编码 GBK 中文。`fs put` 只写不存在的文件；日常更新使用 `deploy`。部署先把程序和资源写入同目录 8.3 临时文件，再异步回读 CRC32，全部成功后才替换目标。原文件保留为备份，可判定失败会逆序回滚；整个多文件包不具备掉电原子性。传输超时或回执损坏后停止，不重发、不猜测是否已替换，报告保留事务路径。新的连接可以用 `fs io` 查写入 token/offset/path、`jobs status` 查 CRC 进度，需要放弃时显式 `fs abort TOKEN` 或 `jobs cancel ID`；部分文件保留供检查。

`apps refresh/launch` 默认自动查找固件认证的原厂前台桌面，拒绝同名 BDA 桌面。`windows` 返回前台、父子/兄弟窗口、handler 和 `queue_owner`；后者是原厂消息队列归属，不是应用名称。`apps list` 列出 A/B 程序目录内的 BDA 文件，不把文件列表冒充当前桌面图标表。启动结果分别报告磁盘文件 CRC、前后窗口及运行版本是否核对；给出 `--image-elf` 时才核对全部可执行节的实际 RAM SHA256，并要求前台 handler 属于该镜像。这仍不等于对正常菜单 loader 的验收。

保持单连接可用 `session plan.json` 或 `session` 从 stdin 逐行读取 JSONL。Python API 支持 `with Client.connect() as client:`，RPC 在同一连接中串行化；另一条状态查询不会主动取消运行中的 EXEC。USB 复位、取消配置、显式 cancel/stop 和模块预算仍可取消模块。客户端正常退出和设备断线会释放注入输入。持键 down/up 应放在一个 session 中，`key --action hold --ticks N` 则在同一连接等待设备释放后才退出。

例如把下面清单保存为 `MyApp.test.json`，与 `MyApp.bda`、`MyApp.elf` 放在一起：

```json
{
  "files": [{"source": "MyApp.bda", "target": "A:/应用/程序/MyApp.bda"}],
  "launch": "A:/应用/程序/MyApp.bda",
  "image_elf": "MyApp.elf",
  "steps": [
    {"action": "input", "target": "$launched", "kind": "tap", "key": "enter"},
    {"action": "screenshot", "output": "panel.png", "roi": [10, 20, 100, 80]}
  ]
}
```

```powershell
python 9588-debug/client.py run-test MyApp.test.json --directory build/MyApp-test
```

该命令在一个连接中完成部署、刷新、启动、版本核对、输入、截图和日志报告。资源可以加入 `files` 数组，所有资源通过回读校验后才进入替换阶段。`$launched`/`foreground` 只引用本 session 已验证镜像的窗口；每次发送前重新核对句柄、handler、队列归属和代码字节，前台变更就拒绝发送。整数 `--target` 可用于手动诊断，设备仍检查它是否为当前前台。输入是原厂窗口消息，不修改物理按键轮询数组；仅处理 polling packet 的应用可能不响应窗口按键消息。

清单相对路径按清单目录解析；stdin JSONL 的路径按当前目录解析。输出 `report.json` 保存设备身份、固件扫描模式和地址、程序 SHA256、设备回读 CRC、阶段结果与耗时、事件日志；失败时在健康连接上补取窗口和截图，CLI 返回非零。文件与截图结果还记录客户端总耗时、载荷字节/秒、分块数及 RPC 数；主机模型的计时不代表实机 USB 吞吐。ROI 按输出显示方向定义，并转换为 LCD 原生矩形，只传相应区域；`--baseline` 是本地像素差分，报告变化像素数与包围框。

## 固件依赖与许可

汇编也必须拿到正确的函数、结构和寄存器地址。当前后台模式调用原厂调度器、全局堆与中断服务，相关布局随固件分支变化；通用文件自动选取模板或扫描得到的已验证引用图绑定；函数体和 ABI 改变的固件仍需新的特征支持。详见 [固件兼容说明](../docs/FIRMWARE-COMPATIBILITY.md)。

代码采用GPL-3.0-only，保留H1Debug及9688实现来源，SDK选择兼容的Apache-2.0许可路径。原厂固件、NAND、手册、用户文件及私有媒体不分发。
