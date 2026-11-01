# 9688 Debug BDA

步步高 **9688 V2.32 / JZ4730、JZ4740** 的后台 USB 调试器，参考 H1Debug 驻留实现、9588 Native BDA SDK 和本机 9688 原厂恢复固件开发。默认只生成一个 `Debug9688.bda`，启动时识别固件并选择对应芯片的驻留程序和 USB 驱动；无需手动选芯片，也无需刷写或修改磁盘上的固件。

**当前为实验版；旧版 JZ4740 基础调试产物已完成真机验收，新扫描/管理构建及 JZ4730 仍待真机验证。** 2026-11-01 的 JZ4740 实测覆盖握手、实际 V2.32 固件指纹、自有堆块读写与释放、240×320 截图、RAM 模块执行和 DTR 重连，详见 [真机记录](hardware-validation.md)。交叉编译、MIPS 重定位和控制器模型测试另记在输出目录的 `verification.json`，不能代替未覆盖功能的实机验证。运行时检查所选芯片的代码和服务表 CRC，通过检查后才分配驻留内存并接管 USB。

同日还经 EXEC 验证了 SDK 的 JZ4740 摄像头抓帧、RGB565 转换及原生录音、WAV 收尾和读回，见 [媒体真机记录](media-hardware-validation.md)。两个测试模块位于 `examples/camera_probe.c`、`examples/recording_probe.c`；它们有跨调用资源，必须按源码协议复用同一已加载镜像，确认清理后才能 reset 或更换模块。

当前调试模板依据 **V2.32** 固件；启动先精确匹配，再尝试受约束的 MIPS 引用图扫描，唯一且完整通过后才启用。芯片相同不代表函数体、ABI 或板级 GPIO 相同，未匹配的结构仍被拒绝，见[固件兼容说明](../docs/FIRMWARE-COMPATIBILITY.md)。另有 `9688-legacy-notice.json`，仅为本次原机旧固件（界面报告 V2.1）的消息框建立指纹，用于显示版本不支持提示，不启用该版本的 RTOS 或 USB 调试。

## 构建

在仓库根目录运行：

```powershell
git submodule update --init sdk
python -m pip install -r 9688-debug/requirements.txt
powershell -ExecutionPolicy Bypass -File sdk/scripts/setup_toolchain.ps1
python 9688-debug/build.py
```

构建器自动寻找固定 SDK 的 `sdk/.toolchain/bin`，也可用 `--toolchain PATH/TO/bin` 指定。使用 `-EL -march=mips32 -mabi=32 -msoft-float`，所有目标源码启用 `-Wall -Wextra -Werror`。不需要 H1 工作区或原厂 BDA 模板。

默认产物为 **`build/9688-debug/Debug9688.bda`**，设备菜单名为“9688调试通用2”，用于区分旧单芯片构建。这个文件包含两种芯片各自的驻留镜像，启动后只装载与当前固件匹配的一份。

开发时仍可显式构建单芯片版本，用于比较驱动和模型测试：

| 芯片 | 输出 | 菜单名 | 对应原厂内核 |
| --- | --- | --- | --- |
| JZ4730 | `build/9688-debug-jz4730/Debug9688-JZ4730.bda` | 9688调试4730 | `C100knl.bin` |
| JZ4740 | `build/9688-debug-jz4740/Debug9688-JZ4740.bda` | 9688调试4740 | `kj409688.bin` |

```powershell
python 9688-debug/build.py --chip jz4730
python 9688-debug/build.py --chip jz4740
```

每个输出目录还包含：

- BDA 及 SHA256 校验文件；单芯片开发构建另有 `Debug9688.bda` 同内容别名。
- `build.json`：构建身份、源码散列、固件身份、内存大小、符号地址。
- `launcher.elf/.bin/.map`、重定位清单及生成头；通用构建的芯片组件位于 `components/jz4730/` 与 `components/jz4740/`，各含 `resident.elf/.bin/.map` 和组件清单，不额外生成 BDA。
- `verification.json`：验收记录；硬件和完整固件启动状态单独标记。

也可显式 `--profile PATH/TO/profile.json` 构建单个配置，与 `--chip` 冲突时会拒绝构建。

固件来源与地址证据见 [JZ4740 笔记](firmware-notes.md) 和 [JZ4730 笔记](firmware-jz4730-notes.md)，配置在 `profiles/9688-v232-jz4740.json` 与 `profiles/9688-v232-jz4730.json`。原厂镜像不随源码分发。通用构建可同时指定两份固件，核对 SHA256、长度和全部 CRC 守卫；单芯片开发构建使用 `--firmware`。接受对应的原始 `C100.bin` 或带 64 字节头的 `C100knl.bin` / `kj409688.bin`；两个底层目录都含 `C100.bin`，需按芯片选取。

```powershell
python 9688-debug/build.py --firmware-jz4730 PATH/TO/C100knl.bin `
  --firmware-jz4740 PATH/TO/kj409688.bin
python 9688-debug/verify.py --firmware-jz4730 PATH/TO/C100knl.bin `
  --firmware-jz4740 PATH/TO/kj409688.bin
```

`verify.py` 默认验证通用 BDA，核对产物与源码散列，运行两套 USB 模型、启动器选择及协议、内存、LCD、模块和客户端测试；顶层 `verification.json` 汇总两套组件测试及通用启动器的日志。开发时可加 `--chip jz4730` 或 `--chip jz4740` 验证单芯片目录。同时提供两份本地固件会执行跨芯片拒绝测试；未提供固件时，相应原厂镜像检查会记录为跳过。

## 安装与连接

1. 确认机器为 **9688 V2.32**，在普通磁盘模式将 `build/9688-debug/Debug9688.bda` 复制到设备 `应用/程序`。两种芯片使用同一个文件。
2. 安全移除并拔掉 USB，在设备上打开“9688调试通用2”，根据屏幕提示启动并回到菜单。
3. 再接 USB。程序使用 CDC ACM 串口；调试期间 USB 由调试器持有。
4. 用下方命令连接。客户端自动识别芯片；多台设备用全局 `--chip` 或 `--port COMx` 选择。

| 芯片 | USB VID:PID | 精确握手 |
| --- | --- | --- |
| JZ4730 | `07C4:A4A8` | `9688DEBUG/1 V2.32 JZ4730 RESIDENT` |
| JZ4740 | `07C4:A4A7` | `9688DEBUG/1 V2.32 JZ4740 RESIDENT` |

连接时同时检查 USB PID 和精确握手；选择 `--chip jz4730` 不会把 JZ4740 当作目标。

```powershell
python 9688-debug/client.py list
python 9688-debug/client.py ping
python 9688-debug/client.py status
python 9688-debug/client.py caps
python 9688-debug/client.py --report build/9688-usb.json usb-diag
python 9688-debug/client.py --chip jz4730 status
```

供电检测与 USB 接管按各自芯片配置实现，实际板级插拔行为仍待验证。JZ4740 使用 PB18，JZ4730 使用 GP89；它们只确认是外接电源检测输入，与 USB VBUS 的物理连线尚未实测。驱动在控制器被原生服务重新接管时停止服务。初次硬件测试应按拔线启动的顺序操作。

JZ4740 使用 MUSB PIO；JZ4730 使用独立的旧式 UDC 驱动，EP0 为 32 字节，CDC 固定使用 EP1 interrupt IN、EP2 bulk IN、EP5 bulk OUT。两种芯片共享上层帧协议、内存、截图和模块 ABI。

JZ4730 的发送状态依靠 IN token 推进，真实总线的 ACK、错误重传和提交时序仍待真机验证。其 OUT FIFO 没有软件可读的包顺序选择器；检测到多个 OUT 端点同时 pending 时，会以故障 `1024` 停止服务并保留 FIFO，避免把错序数据作为调试命令执行。对应模型测试包含这些拒绝路径。

## 点击后没有反应

旧版在固件 CRC 不匹配时直接返回，可能表现为点击后仍停留原画面。已知 V2.1 真机反馈属于尚未支持的固件版本，不能据此认定为 JZ4730 / JZ4740 选错，也不能用 V2.32 地址强行启动。

1. 在设备系统信息中核对固件版本。旧版本会被拒绝；本次原机旧内核已增加独立消息框指纹，可显示 `Unsupported firmware`。通用版的调试能力仍要求已验证的 V2.32。
2. 替换构建时，先关闭调试器并重启设备，防止旧驻留程序仍占用任务和内存。确认复制的是新的 `build/9688-debug/Debug9688.bda`；旧单芯片文件可移到电脑上，避免从菜单误开。
3. 安全移除、拔掉 USB 后运行“9688调试通用2”。记录屏幕出现的启动、固件检查或错误提示；若仍完全没有提示，记录“无提示且原画面不变”，不要直接尝试 RAM 写入或 SDK 调用。
4. 只有屏幕提示后台启动成功后，才接 USB，运行 `python 9688-debug/client.py list` 和 `status`。没有串口与没有启动提示是不同阶段的问题。

未知固件的 GUI 服务也可能与 V2.32 不同。只有确认消息框调用安全时，程序才能显示诊断；完全不匹配时仍可能直接返回，因此“无提示”并不能单独区分 BDA 未加载与固件检查未通过。

原机内核同时存在“os文件启动”和“nandflash启动”两条路径。替换磁盘上的 `系统/数据/kj409688.bin` 后，需重启并核对实际运行版本，文件写入成功不代表内部 NAND 已刷写。原厂 SD 一键恢复会格式化，应与保留资料的内核文件替换分开处理。

## 调试命令

内存操作只接受设备的缓存 SDRAM 地址 `0x80000000..0x81ffffff`，拒绝越界、MMIO 和写入调试器自己的分配块。示例读取当前原生 BDA 入口；写入须使用已确认属于目标程序的地址。

```powershell
python 9688-debug/client.py read 0x81c00020 256 --output build/9688-entry.bin
# ADDRESS 是已确认可写的目标 RAM；不是任意空闲地址。
python 9688-debug/client.py write ADDRESS --file build/payload.bin
python 9688-debug/client.py write ADDRESS --hex 01020304
python 9688-debug/client.py screenshot build/9688-screen.png
```

截图保持 **240×320**，默认将 LCD 原始缓冲旋转 180° 后保存，使工具桌面文字和图标正向显示；旋转不插值、不降采样。诊断原始扫描方向时可使用 `screenshot OUTPUT.png --native-orientation`。结果中的 `rotation_degrees` 与 `orientation` 记录输出方向，`raw_sha256` 始终核对设备传来的原始字节。支持 RGB565 与 18/32bpp BGRX 存储，下载全程使用同一个快照 token，检查描述符和尺寸；不支持的布局会报错。前台并发绘制仍可能造成撕裂。断线、USB 复位、DTR 会废弃快照。

原始 SDK 表调用用于已知签名的固件实验，参数为最多四个无符号 32 位整数，缺少的参数填零：

```powershell
# MEM+8 = 原固件全局malloc，返回地址；此分配需自行以MEM+C释放。
python 9688-debug/client.py call mem 0x8 512
python 9688-debug/client.py call mem 0xc RETURNED_ADDRESS
```

表名为 `gui/fs/sys/mem/res`。函数参数、对象生命周期或地址错误可使整机挂起；没有异常隔离。客户端与设备都会检查帧，设备另检查表索引、槽位对齐和函数地址范围。

## 上传并运行 RAM 模块

```powershell
python 9688-debug/client.py exec info
python 9688-debug/client.py exec run 9688-debug/examples/compute.c `
  --directory build/9688-compute --args-hex 616263 --budget-ticks 80
python 9688-debug/client.py exec status
python 9688-debug/client.py exec cancel
```

示例将 `abc` 三字节相加，预期返回值 `294`，输出小端 `26010000`。主机先读取实时模块地址，再编译、顺序分块上传并核对 CRC32，提交后才运行；保留 `module.elf/.bin/.json` 和 `execution.json`。

模块 ABI 在 [module_abi.h](module_abi.h)：`int debug9688_entry(const debug9688_api *, debug9688_call *)`。代码、数据、BSS 共 64 KiB；输入 256 字节，输出 512 字节，与调试任务共用 32 KiB 栈。新模块清零整个区域；重复运行保留模块的数据/BSS。`api->runtime_table(0..4)` 返回启动时保存的五张服务表，后续 BDA 不会覆盖它们。

长循环必须调用并处理 `api->poll_cancel()`：它服务 USB、让出任务并检查预算/取消。`budget_ticks` 为原生计数器，不能据此假设固定频率。没有强制抢占、异常恢复或内存隔离；不配合的死循环无法通过 `cancel` 强制终止。模块运行期间只允许身份、状态、取消和停止类命令。

## 关闭与生命周期

再次打开同一个“9688调试通用2”程序或执行：

```powershell
python 9688-debug/client.py stop
```

`stop` 回执只表示请求被接受；任务随后断开 CDC、恢复原控制器与中断配置。若没有恢复磁盘，拔插一次 USB 后检查。重复启动复用同一全局堆块和休眠任务，完全释放需要重启；替换不同构建后也必须重启再运行。驻留全部位于全局堆，启动器退出或下一 BDA 覆盖 `0x81c00020` 不会覆盖调试服务。

通信采用独立 `96DB` 帧、序号和 CRC32，最多 512 字节载荷，一次只允许一个请求。收到超时、短写、序号错误或坏 CRC 后，客户端会废弃会话，**不会自动重发写入或执行**。错误回执和 USB 诊断可用全局 `--report PATH` 保存。

新管理版的 DTR 变化仅切换主机连接，不取消活跃 EXEC；USB 复位或取消配置仍会请求取消。`exec run` 在同一连接中轮询结果。尚未完成的模块上传不会执行，可由新的 BEGIN 覆盖。异常的原生 USB 退出若留下控制器地址，启动可能保守拒绝；正常拔线退出已在固件中确认会清零地址，实际插拔行为仍待真机验证。

原有后台 CDC、RAM 读写、截图、SDK 表调用、RAM 模块执行和停止恢复继续保留；新管理协议按实际固件能力提供文件事务部署、桌面刷新/启动及窗口输入。GDB 单步及调频自动重连仍未提供。CAPS bit0..4 保持 RAM read/write、screen、EXEC、table call，bit5..8 分别为管理协议、固件扫描诊断、ROI 截图和事件日志；管理协议内再逐项查询 FS/GUI 能力，不能套用 H1 的位定义。


## 文件、桌面与自动测试客户端

`0.2.0-alpha.1` 增加正式管理协议；先运行 `firmware` 和 `management` 查看启动扫描结果及当前固件实际启用的能力。旧 BDA 或未通过相应 FS/GUI 指纹检查的固件会明确拒绝管理操作。各模型/固件的验证范围见 [完整工作流说明](../docs/DEBUG-WORKFLOW.md)，新增功能尚不能沿用旧 BDA 的真机验收记录。

```powershell
python 9688-debug/client.py firmware
python 9688-debug/client.py management
python 9688-debug/client.py fs list 'A:/应用/程序'
python 9688-debug/client.py fs get 'A:/应用/程序/MyApp.bda' build/MyApp-backup.bda
python 9688-debug/client.py deploy build/MyApp.bda 'A:/应用/程序/MyApp.bda'
python 9688-debug/client.py apps list
python 9688-debug/client.py apps refresh
python 9688-debug/client.py windows
python 9688-debug/client.py apps launch 'A:/应用/程序/MyApp.bda' --image-elf build/MyApp.elf
python 9688-debug/client.py screenshot build/region.png --roi 10,20,100,80
python 9688-debug/client.py screenshot build/current.png --baseline build/baseline.png
python 9688-debug/client.py logs
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
python 9688-debug/client.py run-test MyApp.test.json --directory build/MyApp-test
```

该命令在一个连接中完成部署、刷新、启动、版本核对、输入、截图和日志报告。资源可以加入 `files` 数组，所有资源通过回读校验后才进入替换阶段。`$launched`/`foreground` 只引用本 session 已验证镜像的窗口；每次发送前重新核对句柄、handler、队列归属和代码字节，前台变更就拒绝发送。整数 `--target` 可用于手动诊断，设备仍检查它是否为当前前台。输入是原厂窗口消息，不修改物理按键轮询数组；仅处理 polling packet 的应用可能不响应窗口按键消息。

清单相对路径按清单目录解析；stdin JSONL 的路径按当前目录解析。输出 `report.json` 保存设备身份、固件扫描模式和地址、程序 SHA256、设备回读 CRC、阶段结果与耗时、事件日志；失败时在健康连接上补取窗口和截图，CLI 返回非零。文件与截图结果还记录客户端总耗时、载荷字节/秒、分块数及 RPC 数；主机模型的计时不代表实机 USB 吞吐。ROI 按输出显示方向定义，并转换为 LCD 原生矩形，只传相应区域；`--baseline` 是本地像素差分，报告变化像素数与包围框。

## 来源与许可

`9688-debug/` 使用 [GPL-3.0-only](LICENSE)。任务生命周期、JZ4740 CDC PIO、协议及重定位实现改编自 [BBKH1-debug](https://github.com/HelloClyde/BBKH1-debug)（GPLv3），各文件保留说明；来源版本和许可整理见仓库根目录 [NOTICE](../NOTICE.md)；JZ4730 UDC 驱动依据芯片手册和原生固件独立适配。9688 地址、板级差异和固件 CRC 来自本次核对。BDA 容器与图标打包复用固定 `sdk/` 子模块的 `bda_packer` 和公共 SDK。

## 独立仓库构建身份

SDK 子模块固定到 `ef3bf3b7328a3a8fa9ba48593c63285300725fb7`，使用其公开对话框/内存
绑定和 BDA header/icon 打包工具，不依赖原开发工作区的 `reverse/sdk`。
`BDA_TOOLCHAIN_PREFIX` 可指定 `PATH/TO/mipsel-none-elf-`，或通过 `--toolchain PATH/TO/bin`
指定构建工具链。

驻留 `image_id` 表示运行时代码兼容性，而不是当前源码归档的散列。构建先以 ID=0
编译，核对 raw、内存边界及全部重定位记录的 canonical 指纹；只有再验证最终完整
resident SHA256 与既有目标一致，才复用旧 ID。未知代码自动派生新 ID。
当前源码和 SDK 依赖的真实散列仍完整记录在每次构建的 `source_sha256`，没有冻结或伪造
源码散列。独立仓库的整 BDA 还须与既有真机文件逐字节比较，才能复用其真机验收记录。
