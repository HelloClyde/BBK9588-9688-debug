# 9688 摄像头与录音真机验收

2026-11-01（Asia/Shanghai），在已启动的 9688 V2.32 / JZ4740 Debug 上，经同一
COM5 连接运行两个独立 RAM 模块。BDA SHA256 为
`fe9d5b7ef5aebd6142c4ddb79e0bc721362e49bb1ad3cd8d5349a42a12346765`，
驻留 image_id 为 `49d6b0c7`。串口、图像和录音均来自真机，不是控制器模型。

探针源码为 [camera_probe.c](examples/camera_probe.c) 与
[recording_probe.c](examples/recording_probe.c)。底层 SDK 头文件没有为了测试而跳过
指纹或所有权检查。每个模块的后续操作使用同一镜像 RPC 30；关闭所有资源后才 reset。
原厂媒体任务优先级为 7，Debug 为 6，等待中通过返回或 poll_cancel 让出调度。

## 摄像头

- 打开、传感器识别、DMA 启动、完成查询、关闭均返回 0。
- 取回 320×240 YUYV，153600 字节；关闭原生相机前复制到独立内存，下载时没有访问已释放的 DMA 缓冲。
- 首帧 Y 仅 47/49，近灰。按原厂拍摄函数 `0x80223abc` 的 13 次计数，在同一次打开中丢弃前 12 帧、保存第 13 帧。
- 第 13 帧已拍到暗环境中的物体和线缆，Y 范围 64..155；第一/末帧采样平均亮度约 47.84/86.73。
- 真机调用 SDK YUYV→RGB565，全部 153600 字节与电脑端独立逐像素公式核对一致；PNG 保留传感器 320×240 方向，不套用桌面截图的 180° 旋转。
- raw SHA256：`e38103a8514c6b57bf70e3504f4c5a103501dff6c8c77c4bcdde15c71673e1dd`。
- RGB565 SHA256：`6dd1d9ca1e3f4564db7db3559bc44ea8eaea06bbb01e2f02c955d4df66aeadf5`。

## 录音

- 新文件为设备 `A:\DAE62300.WAV`，开始前只读检查不存在，未覆盖已有文件。
- 经 queued→capturing，原生计数达到 3072 ms；STOP 后收到完成回调，native_error=0，再执行 finish。
- 文件实际读回 98364 字节，RIFF size=98356；`fmt ` 长 32，PCM format=1，mono/16000 Hz/16-bit，data 长 98304 字节，时长 **3.072 秒**。
- 原厂 block_align=256、byte_rate=32000 被原样保留；文件 SHA256 为
  `586fe0397b5c0f0b873b2349a8e30601448d5c83b6c0347e18296a419d3af294`。
- 49152 个采样中非零 41128 个，采样范围 -178..2228，520 个不同值，存在变化的真实音频输入；本次没有验证可辨识语句。
- 初次读回测试错误地按 C fseek 的零返回值判定成功。原厂 seek 实际返回新偏移，探针已修正；直接重开并完整下载同一录音，没有重录或改写原文件。
- 电脑另存 `recording-playback.wav`，只将头部标准化为 44 字节 PCM WAV（block_align=2），PCM 逐字节保持一致，便于播放器试听。原厂 WAV 同时保留。

## 清理与证据

相机 IRQ/设备全局恢复，两个自有图像副本均释放；录音完成回调后收尾、读回 FILE 关闭。
每项测试前后空闲堆均为 **11566556 字节**，未观察到堆空间损失。最后 exec_state=0，
USB fault、last_error、bad_frames 均为 0；调试器继续后台运行，电脑串口已关闭。

原开发机本地保留的证据目录为 `build/9688-media-hardware/20261101/`。以下原始日志、相机图像和录音不随开源仓库或 Release 分发：

- `verification-initial.json`：首帧及初次读回诊断。
- `retry/verification.json`：最终运行日志、模块构建信息、SDK 源码散列、清理状态。
- `retry/camera.png`、`camera.yuyv`、`camera.rgb565`：实际图像及原始数据。
- `retry/recording-native.wav`、`recording-playback.wav`：实际音频。

JZ4730、同时启用相机与录音、长时间采集和异常拔线清理仍未真机验证。
