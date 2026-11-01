# 维护与发布

仓库版本由根目录 `VERSION` 决定，当前预发布为 `0.1.0-alpha.2`，新增独立的 9588 移植。所有发布均从已审查、
干净的 Git 提交构建；SDK 固定为子模块 gitlink，不从 main 临时下载文件。

## 构建和验证

Windows PowerShell、Python 3.12：

```powershell
git clone https://github.com/HelloClyde/BBK9588-9688-debug.git
cd BBK9588-9688-debug
git submodule update --init sdk
python -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File sdk/scripts/setup_toolchain.ps1
python build.py --models all --verify
```

`all` 只构建仓库中实际存在的源码目标；`build/release-build.json` 的 `models` 是本次资产的
唯一机型清单。alpha.2 同时提供 `Debug9588.bda`（9588 V3.30/JZ4740）和
`Debug9688.bda`（9688 V2.32/JZ4730、JZ4740）。两个机型使用独立固件绑定，不共享机型身份。
9688 的两种芯片位于同一个 BDA；9588 当前只支持 JZ4740。单芯片开发构建不加入正式清单。

可选的本地私有固件通过目标专用参数提供；以下命令中的路径必须替换为自己的文件：

```powershell
python build.py --models 9588 --verify --firmware-9588 PATH/TO/9588-firmware.bin
python build.py --models 9688 --verify --firmware-jz4730 PATH/TO/9688-4730.bin --firmware-jz4740 PATH/TO/9688-4740.bin --firmware-legacy PATH/TO/9688-legacy.bin
```

`--firmware-9588` 只转发给 9588 构建器和验证器的 `--firmware`。
`--firmware-jz4730`、`--firmware-jz4740` 只转发给 9688；`--firmware-legacy` 只用于
9688 的旧固件提示验证。两个机型的固件参数互不混用，也不进入发布包或公开 CI。

验证分为：

1. BDA 头部、校验和、重定位、源码构建身份与产物 SHA-256；
2. PC 协议、MIPS 和 USB 控制器模型；这些测试不需要私有固件；
3. 可选的本地固件机器码检查，只由拥有相应 ROM 的开发者运行；
4. 独立的真机验收记录，以具体芯片、固件和完全相同的 BDA SHA-256 为边界。

CI 只依赖公开源码和固定工具链，明确要求两个机型都构建和离线验证；缺少任一源码目标即失败。
它通过前两层也不会扩大真机覆盖。9588 V3.30/JZ4740 尚未真机验证，发布清单中的
`hardware_verified` 固定为 `false`；不能继承 9688 的硬件记录。9688/JZ4730 只有模型验证；
9688/JZ4740 的既有实测记录仅在本次产物与记录的 SHA-256 完全一致时有效。
SDK 版本、编译器或源码改变后若二进制改变，应重新做真机验收，再更新记录。

## 本地打包

先提交所有计划发布的源码，再重新构建，避免清单记录的是上一次 HEAD。SDK 子模块必须
干净且指向主仓库已提交的 gitlink。以下命令创建包并独立验证：

```powershell
$releaseCommit = git rev-parse HEAD
python build.py --models all --verify
python package_release.py --commit $releaseCommit
python package_release.py --verify --commit $releaseCommit
```

首次整理、尚未提交的工作区允许不带 `--commit` 生成准备包。它明确标为
`preparation_only: true`，不能通过官方发布验证。不把准备包手动上传为版本资产。

默认输出 `dist/release/`；本地保留上一个版本时，用 `--output dist/v0.1.0-alpha.2` 为打包和
`--verify` 指定同一个新目录，避免混入旧版本资产。CI 使用干净工作区。

输出包括：

| 资产 | 内容 |
|---|---|
| `Debug9588.bda`、`Debug9688.bda` | 与安装 ZIP 内字节完全相同、按机型区分的独立 BDA |
| `BBK9588-9688-debug-vVERSION.zip` | 按机型分开的 `应用/程序`、PC 客户端、依赖文件、说明及验证记录 |
| `BBK9588-9688-debug-vVERSION-source.zip` | 对应提交的公开源码及完整展开的固定 SDK 源码 |
| `release-manifest.json` | 提交、SDK、实际机型、BDA 散列、硬件证据范围及资产散列 |
| `SHA256SUMS.txt` | 除自身外所有资产的 SHA-256 |
| `RELEASE-NOTES.md` | 同次构建产生的发布说明 |

名称中的 `VERSION` 替换为版本号，不包含额外的 `v` 前缀。清单使用仓库相对路径，不能
带开发机绝对路径。脚本拒绝重复文件名、未声明机型、陈旧构建清单、错误散列与额外输出文件。

源码包按已提交 Git 文件清单构建；SDK 单独按其固定提交文件清单展开，保留对应版权和许可。
不包含 `.git`、SDK 的嵌套模拟器子模块、预编译 BDA、工具链、固件、设备磁盘、私有照片或录音。
`SOURCE-PROVENANCE.json` 的 `files_sha256` 是平铺的相对路径到 SHA-256 映射；
`sdk/SDK-SOURCE-COMMIT` 标明展开版本。无 Git 的解压源码可直接安装工具链并运行根构建器；
它先核验完整文件散列。PC 安装包的普通状态和截图命令不需要工具链；EXEC 编译还需要源码包的
SDK 头文件、pyelftools 和固定 MIPS 编译器。

本项目 Debug 来源遵循根 `LICENSE` 的 GPL-3.0-only。SDK 保留自己的双许可与来源，采用其中
Apache-2.0 许可路径与本项目组合；详见根 NOTICE。发布二进制时一并提供对应源码包。

## GitHub Actions 和标签

`main` push、PR 及手动运行执行同一 Windows 构建和打包流程，产出普通 ZIP Actions artifact。
只读构建作业没有 Release 写权限。固定 SDK 以非递归方式检出；不需要下载 emu 子模块。
工具链缓存只加速下载，安装脚本仍检查所需文件，并在下载时校验 SDK 定义的归档 SHA-256。
所有 Actions 采用已核实的官方版本并固定完整提交 SHA。

更新 `VERSION`、说明与验收记录后，提交并推送 main。待 main CI 成功，从**同一提交**创建并
推送与版本严格相同的标签：

```powershell
git tag v0.1.0-alpha.2
git push origin v0.1.0-alpha.2
```

标签工作流重新构建、打包并验证标签所指提交、VERSION 和固定 SDK。独立 Linux 发布作业
下载构建资产（传输散列不符即失败），再次核验来源、所有 ZIP 内容与 SHA256SUMS，最后
用 `gh release create --verify-tag --prerelease` 发布。已有 Release 不会被覆盖。

发布后核实 Actions 成功、Release 为 prerelease、实际机型清单和资产齐全；下载源码 ZIP
验证可独立构建。若因缺少对应机型源码、模型失败或身份不一致而失败，先修正并发新版本，
不能复制其他工作目录的旧 BDA 绕过流程。
