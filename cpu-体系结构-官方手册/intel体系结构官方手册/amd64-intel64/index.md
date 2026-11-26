对于现代 **x86-64 架构（即 Intel 64 / AMD64）** 的处理器，**最权威、最全面、且持续更新的官方手册**是由 **Intel** 和 **AMD** 分别发布的开发者手册。其中，**Intel 的文档体系最为庞大和系统化**，是业界公认的**标准参考**。

### 📚 核心权威手册（Intel 阵营）

#### 1. **[Intel® 64 and IA-32 Architectures Software Developer’s Manuals (SDM)](https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html)**

- **这是绝对的黄金标准！** 所有在 x86(-64) 平台上进行系统级编程（操作系统、虚拟机监控程序、驱动、固件）、性能优化或指令集开发的开发者都必须依赖的文档。
- **结构：** 通常合并为一个巨大的 PDF 文件，或按卷分开下载。目前包含 **4 卷 + 1 个附加文档**：
  - **Volume 1: Basic Architecture**
    - 涵盖**基础编程环境**：寄存器（通用、段、控制、调试、MSR）、数据类型、内存寻址模式（实模式、保护模式、兼容模式、**IA-32e 模式（64位模式）**、分页机制（传统分页、PAE、**4级/5级分页**）、中断和异常、任务管理、FPU/x87、MMX、XMM/YMM/ZMM 寄存器（SSE/AVX 基础）、**基本执行环境**。
    - 理解处理器工作模式、内存管理、核心寄存器集的起点。
  - **Volume 2: Instruction Set Reference, A-Z**
    - **指令集的圣经！** 按字母顺序列出**所有指令**的详细说明：
      - 助记符 (Mnemonic) 和操作码 (Opcode)
      - **指令功能描述**
      - **受影响的标志位**
      - **受保护模式/64位模式下的异常行为**
      - **支持的寻址方式**
      - **伪代码 (Op/En) 描述精确操作**
      - **使用示例 (Assembly Example)**
      - **性能考虑提示（部分指令）**
    - 编写汇编代码、编译器后端、反汇编器、模拟器的必备。
  - **Volume 3: System Programming Guide**
    - **系统程序员的核心！** 深入讲解**操作系统和虚拟机监控程序 (Hypervisor)** 所需的关键机制：
      - **保护机制细节**：特权级 (Ring 0-3)、段描述符/门描述符/调用门、访问控制。
      - **高级内存管理**：全局/局部描述符表 (GDT/LDT)、中断描述符表 (IDT)、任务状态段 (TSS)、**分页细节 (PML4, PDPT, PD, PT, EPT/NPT)**、控制寄存器 (CR0-CR4, CR8/TPR)。
      - **中断和异常处理**：APIC (Local APIC, I/O APIC)、x2APIC、中断优先级、NMI、SMI、#MC。
      - **任务管理**：任务切换、任务门。
      - **多处理器管理**：**内存类型 (WB, UC, WT, WC, WP, UC-)**、**内存排序 (Memory Ordering)**、**缓存控制 (CLFLUSH, INVD, WBINVD)**、**原子操作 (LOCK prefix, CMPXCHG, XCHG)**、**MESI 协议基础**。
      - **系统管理模式 (SMM)**。
      - **调试与性能监控**：调试寄存器 (DR0-DR7)、断点、性能监控计数器 (PMC)、Last Branch Record (LBR)。
      - **虚拟机扩展 (VMX)**：**Intel VT-x 技术的权威指南**，包括 VMXON/VMXOFF、VMCS 结构、VM Entry/Exit、EPT、VPID 等。构建 Hypervisor (如 KVM, Xen, Hyper-V) 的基石。
      - **安全特性**：**SMX (TXT)**、**SGX**（部分细节可能在单独文档）、**SMEP/SMAP**、**CET (Control-flow Enforcement Technology)**。
      - **管理模式 (SMEP, SMAP, UMIP 等)**。
    - 操作系统内核、Hypervisor、固件开发者必读。
  - **Volume 3A: System Programming Guide, Part 1** (通常与 Vol 3B 一起包含在 Vol 3 中)
  - **Volume 3B: System Programming Guide, Part 2**
  - **Volume 4: Model-Specific Registers (MSRs)**
    - 列出**所有处理器型号支持的 MSR** 及其位定义。用于访问和控制大量底层硬件特性（性能监控、电源管理、特性控制、调试、VT-x 等）。非常底层，特定于处理器型号/微架构。
- **获取方式：** 直接从 [Intel SDM 官网](https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html) 免费下载 **最新合集 PDF**。**务必下载最新版本**，因为 Intel 会持续添加新指令（如 AVX-512, AMX）和特性（如 CET, SGX, TDX）。

### 🧠 学习建议

- **入门 (x86-64 基础):** 精读 **SDM Volume 1**。理解寄存器、寻址模式、基础指令、内存管理（分段/分页基础）、中断异常概念。
- **系统编程/OS 开发:** 精读 **SDM Volume 3**。深入理解保护模式/长模式、特权级、分页、中断/APIC、任务、关键系统数据结构（GDT, IDT, TSS）、VMX。
- **汇编/指令集参考:** **SDM Volume 2** 是日常查阅必备。Optimization Manual 提供性能数据。
- **性能优化:** **Intel Optimization Reference Manual** + 特定微架构资料 (PPR, 或 Intel 针对该代的深度优化指南)。
- **AMD 平台:** **AMD64 Vol 1, 2, 3** + **PPR for target family**。

<!-- books:downloads:begin （由 books/gbb 自动生成，别手改这块） -->
# 下载链接
1. [325462-087-sdm-vol-1-2abcd-3abcd-4.pdf](https://github.com/allinkernel/wblog-books/releases/tag/amd64-intel64__325462-087-sdm-vol-1-2abcd-3abcd-4__20261126-1411) · 25.9 MB · sha256:2d1534c7
<!-- books:downloads:end -->
