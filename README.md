# BBK 9588 / 9688 Debug

BBK 原生 BDA 的后台 USB 调试器，提供状态、RAM 读写、240×320 截图、SDK 服务表调用和 64 KiB 原生模块执行。开发版新增启动地址发现、文件部署、桌面刷新与启动、定向窗口输入和长连接测试流程。两个机型的源码、客户端和 BDA 在同一个仓库管理，各自校验固件与设备身份。采用 GPL-3.0-only；来源见 [NOTICE](NOTICE.md)。

## 支持情况

| 目标 | 当前验证范围 |
|---|---|
| 9588 / JZ4740 V3.30 | 同一个通用BDA；原厂指纹、MIPS/USB/RTOS模型；完整内核入口另记，真机未验收 |
| 9588 / JZ4730 V1.30 | 同一个通用BDA；独立FIFO UDC、低有效GP89、PA29连接控制，真机未验收 |
| 9588 / JZ4720 V3.2 | 同一个通用BDA；独立固件绑定、低有效PB18、MUSB，真机未验收 |
| 9688 V2.32 / JZ4740 | 历史基础调试 BDA 已完成真机验收；本次新文件为模型验证，真机 USB 待验收 |
| 9688 V2.32 / JZ4730 | 同一个通用 BDA 内的独立绑定；MIPS/USB 控制器模型通过，真机未验收 |

从 [Releases](https://github.com/HelloClyde/BBK9588-9688-debug/releases) 下载，并按发布说明确认芯片和固件范围。`0.1.0-alpha.3` 起的9588构建使用一个三芯片通用文件；早期alpha.2仅支持9588/JZ4740。新通用版也可按下方命令从源码构建。

开发中的 `0.2.0-alpha.1` 在精确指纹快速匹配之外，增加 MIPS 地址归一特征和引用图扫描：可以发现同一内核分支中移动的函数和数据地址；代码流程、结构或板级接线改变时仍可能拒绝。已经发布的旧 BDA 不会自动获得这些功能；合并前请使用 [PR #1](https://github.com/HelloClyde/BBK9588-9688-debug/pull/1) 的测试分支构建。具体机制见 [固件兼容说明](docs/FIRMWARE-COMPATIBILITY.md)。

## 快速开始

### 从源码构建

Windows PowerShell、Python 3.11/3.12（本地验证为 3.11.5，CI 配置为 3.12）：

```powershell
git clone https://github.com/HelloClyde/BBK9588-9688-debug.git
cd BBK9588-9688-debug
git submodule update --init sdk
python -m pip install -r requirements-dev.txt
.\sdk\scripts\setup_toolchain.ps1
python build.py --models all --verify
```

只初始化 `sdk`，无需递归下载模拟器。可以用 `--models 9588` 或 `--models 9688` 单独构建；已有编译器可传 `--toolchain PATH/TO/bin`。构建不需要原厂固件或 BDA 模板，可选的原厂机器码验证只读取本地 ROM。更多命令见 [9588 说明](9588-debug/README.md)、[9688 说明](9688-debug/README.md) 和 [发布说明](docs/RELEASING.md)。

### 安装与连接

| 设备 | 构建文件 | 设备菜单 | PC 客户端 |
|---|---|---|---|
| 9588 / JZ4740、JZ4730、JZ4720（匹配上表固件） | `build/9588-debug/Debug9588.bda` | 9588调试 | `9588-debug/client.py` |
| 9688 V2.32 / 两种芯片 | `build/9688-debug/Debug9688.bda` | 9688调试通用2 | `9688-debug/client.py` |

1. 将与机型、固件匹配的 BDA 复制到设备 `应用/程序/`。安装包按 `9588/`、`9688/` 分目录，选择对应机型。
2. 安全移除并拔掉 USB，在设备上运行调试 BDA。成功提示后返回菜单，再接回 USB。
3. 在电脑安装客户端依赖，并执行对应命令：

```powershell
python -m pip install -r requirements.txt
python 9588-debug/client.py list
python 9588-debug/client.py status
python 9588-debug/client.py screenshot screen.png
# 9688 使用同一命令形式，将路径换为 9688-debug/client.py
```

客户端枚举设备并核验精确握手，不要猜测 COM 号。9588 使用实验性 `07C4:A4A9`/`A4AA`/`A4AB`；9688 使用 `A4A7`/`A4A8`，两套客户端拒绝串用。截图默认纠正 LCD 的 180° 扫描方向，`--native-orientation` 保留原始方向；9588 的方向目前只在模拟器观察。

再次打开同一 BDA 或执行 `client.py stop` 请求停止。不同运行时版本更新前先重启；停止后磁盘恢复的真机覆盖范围见各机型说明。内存写入使用已确认自有的地址，EXEC 模块需要协作处理取消。

## 部署与测试

开发版客户端提供 `firmware`、`management`、`fs list/get/put/crc`、`deploy`、`apps list/refresh/launch`、`windows`、`key/click`、`jobs`、`logs`、`session` 和 `run-test`。先查询 `management`，当前固件的每项能力经过独立认证后才开放。

`session` 和 `run-test` 让部署、刷新、启动、输入、截图与报告复用同一连接。部署先写同目录临时文件，设备读回计算 CRC32，通过后再保留旧文件并重命名替换；程序与资源可以组成一个有回滚记录的 bundle。对通信超时后结果不明的写入和执行，客户端保留现场且不自动重试。

区域截图只传指定像素，并可与本地基准图比较；事件日志、设备/固件信息、文件 SHA-256、每步耗时和失败截图可保存到测试报告。完整参数、示例和验证边界见 [完整调试流程](docs/DEBUG-WORKFLOW.md)。

## 截图与验证

下图是**历史版本的 9688/JZ4740 真机**工具桌面，后台 Debug 已启动，由本项目截图命令取得；它不代表本次新文件的真机验收。

![9688 真机后台调试截图](docs/assets/9688-tools.png)

下面是本次 `0.2.0-alpha.1` 的 **9588/JZ4740 未修改 C200 内核模拟器**截图。新版 Debug 实际部署并启动 SDK 触摸测试程序，程序收到点击 `(70,100)` 和回车后，由驻留截图函数复制真实 LCD 像素。命令诊断使用 GDB 适配器，没有 USB 主机收发参与。

![9588 新版部署、启动与输入流程](docs/assets/9588-workflow-qemu/app-full.png)

[本次完整流程验证](docs/WORKFLOW-VALIDATION.md) 绑定新 BDA 的确切 SHA-256，区分原厂内核、模型和真机结果。

[9588 完整内核入口诊断](9588-debug/qemu-validation.md) 与 [9688 真机记录](docs/validation/9688-jz4740.json) 均绑定确切 BDA SHA-256。9588 的模型、GDB入口诊断及真机验证分别记录。这些历史验证只覆盖各记录中的确切旧文件。新开发版的地址发现和管理功能须以新的构建与验证记录为准；管理命令见 [完整调试流程](docs/DEBUG-WORKFLOW.md)。GDB 单步仍不在调试协议范围。

## 依赖

- 设备：匹配的原厂固件、可运行原生 BDA 的机器；设备端无需 SDK。
- PC：Python 3.11 或 3.12、pyserial、Pillow，精确版本见依赖文件。
- 开发：MIPS little-endian GCC 15.2.0、pyelftools；Unicorn 用于 MIPS/控制器模型。
- `sdk/` 固定到 `ef3bf3b7328a3a8fa9ba48593c63285300725fb7`，工具链由 SDK 的校验安装脚本提供。

Release 提供 BDA、客户端安装包、展开固定 SDK 的完整对应源码包及 SHA256SUMS。GitHub 自动生成的源码 ZIP 不含子模块内容，独立构建应使用本项目上传的 `-source.zip`。构建和 CI 不扩大真机验收范围。

## 感谢

- 项目维护者 HelloClyde。
- [BBKH1-debug](https://github.com/HelloClyde/BBKH1-debug) 的驻留、USB 与测试参考实现。
- [BBK9588-bda-sdk](https://github.com/HelloClyde/BBK9588-bda-sdk) 的编译、打包与 SDK。
- 提供本地固件、设备与真机验证的贡献者。

上游许可、原作者与来源分别保留，致谢不代替许可；详见 [NOTICE](NOTICE.md)。
