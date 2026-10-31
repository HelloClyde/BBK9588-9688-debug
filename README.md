# BBK 9588 / 9688 Debug

用于 BBK 原生 BDA 的后台 USB 调试器源码。一个仓库分别管理机型绑定和构建产物，
不会把不同机型的私有固件地址混用。项目采用 GPL-3.0-only；来源见 [NOTICE](NOTICE.md)。

## 当前支持情况

| 目标 | 状态 |
|---|---|
| 9688 V2.32 / JZ4740 | 已完成真机握手、RAM、截图、EXEC、重连和资源清理验证 |
| 9688 V2.32 / JZ4730 | 同一个通用 BDA 内的独立绑定；MIPS/USB 控制器模型通过，真机未验证 |
| 9588 | 尚待确认对应驻留版源码；不把早期文件轮询桥或 H1 版本当作 9588 成品 |

9688 提供后台 CDC、状态、RAM 读写、240×320 截图、SDK 表调用和 64 KiB 原生模块
执行。文件部署、桌面自动运行、按键/触屏注入与 GDB 单步不在当前 9688 协议范围。

## 快速开始

当前预发布：[v0.1.0-alpha.1](https://github.com/HelloClyde/BBK9588-9688-debug/releases/tag/v0.1.0-alpha.1)，本次仅包含 9688。

- [下载 BDA 与 PC 客户端安装包](https://github.com/HelloClyde/BBK9588-9688-debug/releases/download/v0.1.0-alpha.1/BBK9588-9688-debug-v0.1.0-alpha.1.zip)：解压后将 `9688/应用/程序/Debug9688.bda` 复制到设备对应目录。
- [仅下载 Debug9688.bda](https://github.com/HelloClyde/BBK9588-9688-debug/releases/download/v0.1.0-alpha.1/Debug9688.bda)。
- [下载完整对应源码包](https://github.com/HelloClyde/BBK9588-9688-debug/releases/download/v0.1.0-alpha.1/BBK9588-9688-debug-v0.1.0-alpha.1-source.zip)：已展开固定 SDK；不要用 GitHub 自动生成的源码 ZIP 代替它。

成品与客户端使用步骤见下方“安装 9688”。从源码构建时输出 `build/9688-debug/Debug9688.bda`。

开发者命令（Windows PowerShell，Python 3.12）：

```powershell
git clone https://github.com/HelloClyde/BBK9588-9688-debug.git
cd BBK9588-9688-debug
git submodule update --init sdk
python -m pip install -r requirements-dev.txt
.\sdk\scripts\setup_toolchain.ps1
python build.py --models 9688 --verify
```

只初始化 `sdk`，不需要递归下载其模拟器子模块。自有工具链可用
`--toolchain PATH/TO/bin`；不需要原厂 BDA 模板或固件即可构建。原厂 ROM 仅用于
可选的严格机器码模型验证，不能上传到仓库。具体命令见 [9688 说明](9688-debug/README.md)。

### 安装 9688

1. 确认机器是 **9688 V2.32**。将 `Debug9688.bda` 复制到设备 `应用/程序/Debug9688.bda`。
2. 安全移除并拔掉 USB，重启后打开“9688调试通用2”。启动成功后返回原生菜单。
3. 接回 USB，在电脑安装运行依赖后执行：

```powershell
python -m pip install -r requirements.txt
python 9688-debug/client.py list
python 9688-debug/client.py status
python 9688-debug/client.py screenshot screen.png
```

客户端会枚举 07C4:A4A7（JZ4740）/A4A8（JZ4730），并严格验证机型握手。
不要固定猜测 COM 号。截图默认正向 240×320，`--native-orientation` 可保留原始扫描方向。
再次打开同一个 BDA 或执行 `client.py stop` 请求停止；停止恢复磁盘尚未完成真机验收。
不同运行时版本的更新须先重启。只在已确认自有的内存块上写入，EXEC 模块须协作取消。

## 截图

下面是 **9688/JZ4740 真机**工具桌面，后台 Debug 已启动；图像由本项目截图命令取得。
它不是摄像头照片，也不是模拟效果图。

![9688 真机后台调试截图](docs/assets/9688-tools.png)

验证尺寸、构建散列和覆盖范围见 [9688 真机记录](docs/validation/9688-jz4740.json)。
9588 驻留版来源与验证尚待确认，因此没有用其他机型截图代替。

## 依赖

- 成品设备端：匹配的原厂固件与能运行原生 BDA 的设备，不需要安装 SDK。
- PC 客户端：Python 3.12、pyserial、Pillow；精确已测版本见依赖文件。
- 开发：MIPS little-endian GCC 15.2.0、pyelftools；Unicorn 用于控制器/MIPS 模型测试。
- `sdk/` 固定到 `ef3bf3b7328a3a8fa9ba48593c63285300725fb7`，工具链由该 SDK 的
  校验安装脚本提供。不会自动跟随 SDK main。

主仓库或源码包均包含构建、测试、GPL 许可证与 SDK 来源。本次预发布资产由同一次
Actions 构建产生，包含 BDA、用户包、展开固定 SDK 的对应源码包与 SHA256SUMS。
构建和发布过程不能代替未覆盖机型的真机测试。

## 感谢

- 项目维护者 HelloClyde。
- [BBKH1-debug](https://github.com/HelloClyde/BBKH1-debug) 的驻留、USB 和测试参考实现。
- [BBK9588-bda-sdk](https://github.com/HelloClyde/BBK9588-bda-sdk) 的编译、打包与 SDK。
- 提供本地固件、设备和真机验证的贡献者。

上游代码与 SDK 的许可分别保留，致谢不代替许可；具体来源见 [NOTICE](NOTICE.md)。
