# 9588 固件配置

默认通用构建要求三份独立配置齐全：`9588-jz4740.json`（V3.30）、`9588-jz4730.json`（V1.30）、`9588-jz4720.json`（V3.2）。`profile-template.json` 是不可构建的格式示例。

配置必须明确target、SoC、驱动族、内核身份、加载地址、RAM窗口、五张表和私有RTOS/USB/LCD绑定，以及非空代码CRC。极性与4730外部pullup分别由profile提供；同SoC不代表同一板级配置。verified仅表示静态核对，硬件验证另记。

通用构建拒绝缺少、重复或歧义配置。运行时以五张服务表、关键代码和notice guard选择唯一绑定，不单凭字符串版本或CPU指令集。完整来源见各机型firmware-notes，通用机制见上级README。
