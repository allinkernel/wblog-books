**ARM 官方提供了一套极为详尽的文档体系**，其地位和权威性完全等同于 Intel 的 SDM（Software Developer's Manual），只是组织方式更为模块化。
针对 **AArch64**（ARM64）处理器，最核心的权威手册如下：

------

### 📚 **1. [Arm Architecture Reference Manual (ARM ARM)](https://developer.arm.com/documentation/ddi0487/latest)**

- **这是 ARM 世界的“圣经”！地位 = Intel SDM Volumes 1 + 2 + 3**
- **内容覆盖：**
  - **AArch64 & AArch32 指令集详解**：每条指令的编码、行为、异常、寄存器影响（类似 Intel SDM Volume 2）。
  - **系统级架构**：异常模型（Exception Levels - EL0-EL3）、虚拟内存系统（MMU、页表格式 - VMSAv8-64）、缓存模型、原子操作模型。
  - **寄存器参考**：通用寄存器、系统寄存器（如 `SCTLR_EL1`, `TCR_EL1`, `VBAR_EL1`）的完整位定义。
  - **安全扩展 (TrustZone)**：安全世界与非安全世界的切换机制（Monitor Mode, SMC 指令）。
  - **虚拟化支持 (ARMv8.1-VHE)**：虚拟异常级别（vEL2）、Hypervisor 接口。
  - **调试与性能监控**：Breakpoint/Watchpoint 寄存器（`DBGBCR_EL1`, `DBGWCR_EL1`）、性能计数器（`PMCCNTR_EL0`）。
- **版本划分**（按架构版本迭代）：
  - **Armv8-A**：初版 64 位架构（当前广泛使用）
  - **Armv9-A**：最新架构（增加 SVE2、Realm 管理扩展 RME、内存标签扩展 MTE）
  - **文档号**：`DDI0487`（Armv8-A） → `[最新版查官网](https://developer.arm.com/architectures/cpu-architecture/a-profile/docs)`
- **重要性**：任何涉及 AArch64 汇编编写、操作系统内核开发、Hypervisor 实现、安全固件开发的必备参考。

### 📌 **对比 Intel SDM 的关键差异**

| **需求**            | **Intel 解决方案**  | **Arm 解决方案**                |
| :------------------ | :------------------ | :------------------------------ |
| **指令集/基础架构** | SDM Volume 1 + 2    | **Arm ARM (DDI0487)**           |
| **系统编程/OS开发** | SDM Volume 3        | **Arm ARM 系统章节**            |
| **具体CPU优化**     | Optimization Manual | **Cortex-AXX TRM**              |
| **芯片组/外设规范** | 各芯片组Datasheet   | **AMBA/GIC/SMMU 规范**          |
| **CPU勘误**         | SDM Volume 3 附录   | **Cortex-AXX TRM 的Errata章节** |
