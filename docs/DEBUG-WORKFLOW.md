# Debug 部署与测试流程

此文说明 `0.2.0-alpha.1` 开发版。旧 Release BDA 不包含这里的管理命令；源码、BDA 和相邻的 `client.py`、`workflow.py` 需要配套更新。驻留 ABI 已升级到 v2，更新前重启设备。

## 启动与能力

```powershell
python 9588-debug/client.py firmware
python 9588-debug/client.py management
```

9688 将路径换成 `9688-debug/client.py`。两套设备仍各用一个通用 BDA，分别覆盖自己的芯片分支。启动时先精确匹配，失败后扫描已知内核实现的地址归一特征与引用图；`firmware` 报告 exact/scanned、芯片、绑定 ID 和地址。扫描不会尝试执行未知目标。

核心识别与管理能力分别认证。文件、CRC、窗口、刷新、启动、输入以 `management.supported` 为准；地址移动但其他特征尚未认证的新固件可能只有基础调试可用。代码重写、ABI 或主板接线改变仍需适配，不能保证所有版本。详见 [固件兼容机制](FIRMWARE-COMPATIBILITY.md)。

## 一次连接完成整条流程

准备 `MyApp.bda`、对应的 `MyApp.elf`，以及同目录的 `MyApp.test.json`：

```json
{
  "files": [
    {"source": "MyApp.bda", "target": "A:/应用/程序/MyApp.bda"},
    {"source": "assets/data.dat", "target": "A:/应用/数据/MyApp/data.dat"}
  ],
  "launch": "A:/应用/程序/MyApp.bda",
  "image_elf": "MyApp.elf",
  "steps": [
    {"action": "input", "target": "$launched", "kind": "tap", "key": "enter"},
    {"action": "screenshot", "output": "panel.png", "roi": [10, 20, 100, 80]}
  ]
}
```

资源目录须预先存在；可用 `fs mkdir` 创建。相对本地路径按清单所在目录解析。

```powershell
python 9588-debug/client.py run-test MyApp.test.json --directory build/MyApp-test
```

程序与资源先全部暂存和校验，再替换目标。随后认证前台原厂桌面，刷新、启动，比较 ELF 的全部可执行节与设备 RAM，并核对前台 handler。`$launched` 只引用本次连接已验证的窗口；发送输入时还核对句柄、handler、消息队列归属。运行版本验证失败就停止后续输入。整数句柄保留给手动诊断，设备也检查前台目标。

`report.json` 保存设备身份、固件模式与绑定、文件 SHA-256、设备回读 CRC32、运行镜像检查、各阶段结果和耗时、事件日志。健康连接上的失败还保存窗口和截图；结果不明时记录失联和事务路径，CLI 返回非零。

`session plan.json` 接受任意顺序的步骤，也可用 `session` 从 stdin 逐行读取 JSONL。Python API 支持 `with Client.connect() as client:`，一次连接内串行化 RPC；down/up 应放在同一连接中。退出、断线、前台变化和持键租期到期都会清理本调试器注入的输入。

## 独立命令

```powershell
python 9588-debug/client.py fs list 'A:/应用/程序'
python 9588-debug/client.py fs get 'A:/应用/程序/MyApp.bda' build/MyApp.bda
python 9588-debug/client.py deploy build/MyApp.bda 'A:/应用/程序/MyApp.bda'
python 9588-debug/client.py apps list
python 9588-debug/client.py apps refresh
python 9588-debug/client.py windows
python 9588-debug/client.py apps launch 'A:/应用/程序/MyApp.bda' --image-elf build/MyApp.elf
python 9588-debug/client.py jobs status
python 9588-debug/client.py fs io
python 9588-debug/client.py logs
python 9588-debug/client.py screenshot build/region.png --roi 10,20,100,80
python 9588-debug/client.py screenshot build/current.png --baseline build/baseline.png
```

路径统一为绝对 `A:/`、`B:/`，无损编码 GBK 中文。`fs put` 仅创建不存在的文件。`deploy` 生成同目录 8.3 临时名、读回 CRC，通过后先保留旧文件，再重命名替换；bundle 发生可判定失败时逆序回滚。备份保留，整个 bundle 不具备掉电原子性。

通信短写、超时、坏回执后不重试写入、rename、输入或 EXEC。可重新连接，用 `fs io` 查看活动文件 token/offset/path，或 `jobs status` 查看异步 CRC；明确需要放弃时用 `fs abort TOKEN` 或 `jobs cancel ID`，部分文件保留供检查。DTR 切换仅重置协议会话和释放输入，读状态不取消其他 EXEC；USB 总线复位、取消配置、显式 cancel/stop 和模块预算仍可取消模块。真正 stop 会关闭/刷新文件，再交还 USB。

刷新使用原厂重置分类、扫描和选择重置流程，避免追加重复图标及删除后的越界选项。刷新、启动只接受经过代码指纹确认的原厂根菜单或程序网格，不能凭标题中的“桌面”判断。工作由原厂 GUI 任务通过队列执行。`apps list` 是磁盘 BDA 清单，不冒称桌面注册表；`windows.queue_owner` 是消息队列归属，不等于应用名称。所属程序通过 handler 和对应 ELF 内容确认。

输入采用原厂窗口消息，支持触摸点击、down/up、tap/hold；不会伪造物理键轮询状态。只轮询按键数据包的应用可能不响应消息注入。

区域截图坐标按输出显示方向定义，转换成 LCD 原生矩形，设备只复制并传回该区域。基准图比较在 PC 上完成，报告变化像素和包围框。文件和截图返回耗时、分块/RPC 数及载荷吞吐；这些值取决于实际连接，模型和 GDB 的速度不能代表实机 USB。

## 验证范围

验证分三层记录：PC 协议与回滚模型、真实重定位 MIPS/控制器/原始固件指纹测试、完整原厂内核中的运行诊断。每个新 BDA 的确切散列和结果保存在其构建目录。历史 alpha.3 或更早版本的真机/模拟器证据只属于原记录中的文件，不自动延续到新构建。

本次完整内核运行诊断若使用 GDB 注入驻留入口和 RPC，会明确标记它没有 USB 主机收发。9588 三种芯片与 9688 两种芯片的新增功能仍需逐一进行真机 USB 验收。
