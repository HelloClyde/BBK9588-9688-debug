# 9588 Debug BDA

这是从 GPL-3.0-only 的 9688 Debug 移植而来的 **9588 / JZ4740** 后台 USB 调试器。已完成独立源码、PC 客户端、交叉构建和 MIPS/USB/RTOS 模型验证。当前仅支持已核对的 V3.30 C200 固件；JZ4730、JZ4720 和其他固件尚未启用。

**尚未完成 9588 真机验收。** 模型通过、静态固件指纹匹配和完整固件启动分别记录，不能互相替代。原厂固件、其他项目的构建产物、相机图像和录音均未包含在此目录。

## 构建

从工程根目录运行：

    python -m pip install -r 9588-debug/requirements.txt
    python 9588-debug/build.py --sdk-root PATH/TO/SDK --toolchain PATH/TO/bin

使用公开 SDK 固定版本 ef3bf3b7328a3a8fa9ba48593c63285300725fb7。集成仓库默认寻找根目录 sdk 子模块；独立开发目录可用 --sdk-root 或环境变量 BDA_SDK_ROOT 指向同一 SDK checkout。SDK 内的头文件目录是 sdk/include，编译器可由 BDA_TOOLCHAIN_PREFIX、SDK/.toolchain/bin 或 PATH 指定。

默认生成 build/9588-debug/Debug9588.bda，菜单名为“9588调试”；--output-dir 可修改输出目录。SDK 给出的 BDA entry file offset 是 0x95f8，入口运行地址是 0x81c00020，动态服务表头基址是 0x81c00000，菜单 category=9；打包及图标由 SDK 生成，不依赖原厂 BDA 模板。

固件配置位于 profiles/9588-jz4740.json，原始 C200 SHA256 为：

    02a16107b11a3281067871c6fe3d4c289c910d8dfa9924573dd87f00351d6525

配置的 verified=true 仅表示静态地址、代码 CRC 和文件身份已独立核对，不代表真机通过。构建器会拒绝缺失、未核对、字段不全或目标为 9688/H1 的配置，不会从其他设备配置推断地址。所有屏幕尺寸、服务表调用范围、RTOS 与 USB 绑定都必须显式提供。

## 验证

    python 9588-debug/verify.py --sdk-root PATH/TO/SDK
    python 9588-debug/verify.py --sdk-root PATH/TO/SDK --firmware PATH/TO/C200.bin
    python 9588-debug/verify.py --sdk-root PATH/TO/SDK --firmware PATH/TO/C200.bin --foreign-firmware PATH/TO/OTHER.bin

--build-dir 可指定待验收输出目录。--firmware 只读核对本地原始或带 64 字节包装头的固件；--foreign-firmware 确认其他固件被当前 9588 SHA256/CRC 配置拒绝。

本次完整模型共 58 项通过，覆盖真实驻留镜像的三个独立重链接目标、重定位、USB FIFO/描述符/复位/所有权恢复、协议和 CRC、RAM 保护、EXEC 计算模块、PC 到 MIPS 端到端、LCD DMA 格式以及 RTOS O32 调用边界。提供本地 C200 时无跳过；未提供 ROM 时，依赖原始固件的检查明确记录为跳过。生成 verification.json 和详细日志，hardware_verified 与 full_firmware_verified 保持 false。

## 安装

确认是 9588 V3.30 / JZ4740，将 Debug9588.bda 复制到设备的 应用/程序/。
安全移除并拔掉 USB 后运行“9588调试”，看到后台启动提示后返回菜单，再接 USB。
运行客户端 status 检查握手；不支持的固件会在指纹检查阶段拒绝。
再次打开同一 BDA 或运行 client.py stop 请求停止。更换构建前重启，驻留堆块在停止后保留供同版复用。

## PC 客户端

使用实验性 VID:PID **07C4:A4A9**，精确握手为：

    9588DEBUG/1 JZ4740 RESIDENT

客户端只枚举这个 PID，不能选择 9688 的 A4A7/A4A8；握手不符时立即关闭，超时或坏 CRC 后不自动重发写入或执行请求。启动调试器前须断开 USB，避免接管已有原生磁盘会话。

    python 9588-debug/client.py list
    python 9588-debug/client.py ping
    python 9588-debug/client.py screenshot build/9588-screen.png
    python 9588-debug/client.py screenshot build/9588-native.png --native-orientation

屏幕尺寸从设备 CAPS 获取。未修改 C200 的 QEMU 原始桌面像素被观察为倒置 180°，客户端默认旋转 180° 显示；--native-orientation 保留原始扫描方向，--rotate 0/90/180/270 可显式覆盖。9588 真机方向仍待验证。

96DB 协议帧格式及 EXEC ABI v1 的字段布局沿用原实现，原生模块类型及入口改名为 debug9588_*。这不保证固件专用模块跨设备可用；只提供独立的 compute 算术示例，不包含 9688 媒体探针。

## 来源与许可

本目录使用 GPL-3.0-only，保留 H1Debug 与 9688 Debug 的实现来源。原 9688 源码和发布目录不受此移植修改。新的驻留身份由带 9588 目标域的运行时代码指纹派生，不复用 9688 已实测镜像 ID。
