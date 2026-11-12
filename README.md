# GKI SukiSU-Ultra + SUSFS 构建系统

> # ⚠️⚠️ 必读：Android 15 / 6.6 内核 **不支持 KPM** ⚠️⚠️
>
> **`android15-6.6` 系列构建会跳过 KPM（KernelPatch）注入，产物中 KPM 不可用。**
>
> - 内核侧 `CONFIG_KPM=y` 桥接层仍会编译进去，但**不会注入 kpimg**，
>   管理器里 KPM 会显示未修补 / 不可用，`PROBE` 返回 `-EOPNOTSUPP`。
> - 原因与社区一致：6.6 上 KernelPatch 有已知的随机重启 / 启动不稳定问题，
>   因此按社区惯例整体跳过，不为 6.6 打 KPM 补丁。
> - 这不是构建失败，也不要通过改配置强行打开 —— 强行注入会导致设备
>   **logo 阶段无限重启**。
> - 需要 KPM 请使用 `android12-5.10` / `android13-5.15` 系列。
>
> 另外：**`android14-6.1` / `android15-6.6` 只提供 `AnyKernel3.zip`，
> 不提供 fastboot 可刷的 `boot*.img`**；只有 `android12-5.10` /
> `android13-5.15` 提供 `boot*.img`（用 Google certified boot 模板打包）。

---

## 🧪 刷机风险提示（务必先读）

> **本仓库的构建产物只经过「编译层面」的验证，绝大多数变体没有经过真机实刷验证。**
>
> 刷 GKI 内核属于**高风险操作**：刷错内核轻则开不了机（卡 logo / 无限重启），
> 重则需要解锁 bootloader、fastboot 恢复、甚至重新刷写全部分区才能救回。
>
> | 家族 | 验证状态 |
> |---|---|
> | `android12-5.10` | ✅ **已实机验证**（fastboot 刷 `boot-gz.img` 可开机，sukisu 可识别）|
> | `android13-5.15` | ⚠️ 已验证**编译通过**，**未实机刷过** |
> | `android14-6.1` | ⚠️ 已验证**编译通过**，**未实机刷过** |
> | `android15-6.6` | ⚠️ 已验证**编译通过**，**未实机刷过**（且本就不支持 KPM）|
>
> **刷之前请务必：**
>
> 1. **先备份原厂 boot 分区**（fastboot 环境）：
>    ```bash
>    fastboot fetch boot boot_orig.img
>    ```
>    出问题用 `fastboot flash boot boot_orig.img` 恢复。
> 2. **确认设备已解锁 bootloader** —— 未解锁的设备刷 GKI 内核必然失败，
>    有些机型还会在校验失败后直接变砖。
> 3. **确认机型与内核家族匹配**：GKI 内核按 `android版本-内核版本` 分支，
>    刷错家族（例如给 5.10 设备刷 6.1 内核）**必定无法启动**。
>    对照方法：
>    ```bash
>    adb shell uname -r
>    # 例：5.10.236-android12-9-xxx  ->  5.10 系列 + android12 家族
>    ```
> 4. **优先用 `AnyKernel3.zip` 在 Recovery 里刷**（比 fastboot 刷 img 风险低，
>    失败时 Recovery 通常还在，可重新刷回）。fastboot 刷 `boot*.img` 时
>    **只刷 `boot-gz.img`**（本机 MTK 5.10 实测：裸 `boot.img` 和
>    `boot-lz4.img` 会导致无限重启）。
> 5. **先备份数据**，刷内核失败可能需要恢复出厂设置。
>
> 各内核版本对应的刷机方法、机型适配与已知问题，请以
> [SukiSU-Ultra 官方文档](https://sukisu.org) 和对应设备的社区说明为准。

---

### 这是一个自动构建 GKI 内核的仓库

> 不支持一加 ColorOS14、15 和非 GKI 

> 第一次使用务必 **详细阅读** 以下内容，不要因为懒惰而占用他人时间！

> 使用 Python 辅助构建系统，支持指定 SukiSU-Ultra/SUSFS commit 版本构建


---

## 快速开始

### GitHub Actions

#### 方式一：构建单个版本
1. 进入 **Actions** 页面
2. 选择 **Kernel Build**
3. 点击 **Run workflow**
4. 选择 Android 版本、Kernel 版本和构建选项
5. 可选：指定 SukiSU-Ultra 或 SUSFS 的 commit hash

##### 参数怎么填（重要）

**`Android 版本` + `Kernel 版本` + `Sub Level` + `OS Patch Level` 必须配套使用**，
内核源码分支名是 `common-<android>-<kernel>-<os_patch_level>`，填错组合
这个分支不存在。仓库里新增了 `validate_params.py` 前置校验：组合非法时会
**在下载源码之前**就报错并列出该家族所有合法取值，不会让你白等半小时。

| Android | Kernel | Sub Level | OS Patch Level |
|---|---|---|---|
| android12 | 5.10 | 136 | 2022-11 |
| android12 | 5.10 | 198 | 2024-01 |
| android12 | 5.10 | 209 | 2024-05 |
| android12 | 5.10 | **236** | **2025-05** ← 默认值（Redmi Note 11 Pro 5G 实机验证）|
| android12 | 5.10 | X | lts |
| android13 | 5.15 | 74 | 2023-01 |
| android13 | 5.15 | 123 | 2023-11 |
| android13 | 5.15 | 148 | 2024-05 |
| android13 | 5.15 | 170 | 2025-01 |
| android13 | 5.15 | 178 | 2025-03 |
| android13 | 5.15 | 180 | 2025-05 |
| android14 | 6.1 | 78 | 2024-06 |
| android14 | 6.1 | 90 | 2024-08 |
| android14 | 6.1 | 99 | 2024-10 |
| android14 | 6.1 | 124 | 2025-02 |
| android14 | 6.1 | 145 | 2025-09 |
| android15 | 6.6 | 50 | 2024-10 |
| android15 | 6.6 | 66 | 2025-02 |
| android15 | 6.6 | 102 | 2025-10 |

几个容易搞混的概念：

- **Sub Level**（如 `236`、`124`）：内核的小版本号，**数值越大越新**。
  它和 OS Patch Level 是一一对应的，单独填没有意义。
- **OS Patch Level**（如 `2025-05`、`2025-02`）：内核源码分支的**发布日期**，
  不是你手机的安全补丁日期。GKI 的每个发布月对应一个分支。
- **`X` + `lts`**：长期支持分支，只在 android12-5.10 下存在。
- **`Revision`**：只有 android12-5.10 需要填（`r1`/`r13`/`r15`/`r17`），
  用来定位 Google 官方 certified boot 模板；5.15 / 6.1 / 6.6 **留空即可**。

**其它开关怎么选**：

| 开关 | 默认 | 说明 |
|---|---|---|
| `use_kpm` | ✅ 开 | 启用 KPM（KernelPatch），可加载 `.kpm` 模块。**android15-6.6 会自动跳过**（社区已知不稳定）|
| `use_zram` | ❌ 关 | ZRAM 内存压缩（LZ4KD），只影响内存占用，**与 root / KPM / SUSFS 无关**，按需开 |
| `BBG` | ❌ 关 | Baseband-guard，仅基带相关机型需要 |
| `supp_op` | ❌ 关 | OnePlus 8E 专用支持 |
| `set_defbbr` | ❌ 关 | 把 BBR 设为默认拥塞算法（改善跨境/弱网吞吐）|
| `custom_version` | 空 | 自定义版本名后缀，留空则用 `android12-5.10.236-2025-05` 这种默认名 |
| `kernelsu_commit` / `susfs_commit` | 空 | 指定具体 commit 复现构建，留空用默认（KSU 已钉在 `builtin@1cfa5734`）|

**产物**：
- `android12-5.10` / `android13-5.15` → `boot.img` + `boot-gz.img` + `boot-lz4.img` + 3 个 AnyKernel3.zip
- `android14-6.1` / `android15-6.6` → 只有 3 个 AnyKernel3.zip

> 刷机提示：本机（MTK 5.10）**只刷 `boot-gz.img`**，
> 裸 `boot.img` 和 `boot-lz4.img` 会导致无限重启。

#### 方式二：构建所有版本
1. 选择 **Build Kernels**
2. 点击 **Run workflow**
3. 设置全局选项（KSU 版本、ZRAM、KPM 等）
4. 可选：指定 commit 版本

### 命令行本地构建

```bash
# 进入构建目录
cd .github/workflows/scripts

# 安装依赖
pip install PyYAML

# 构建单个版本
python build.py --android android14 --kernel 6.1 --sub-level 124 --os-patch 2025-02

# 构建整个矩阵
python build.py --matrix android14-6.1

# 构建所有版本
python build.py --all

# 指定 commit 版本
python build.py --all --ksu-commit abc1234 --susfs-commit HEAD~1

# 列出所有支持的配置
python build.py --list-configs

# 列出预定义构建矩阵
python build.py --list-matrix
```

---

## 构建矩阵

从 `matrix.json` 加载：

| Android | Kernel | Sub Levels | OS Patch |
|---------|--------|------------|----------|
| 12 | 5.10 | 136, 198, 209, 236, X (LTS) | 2022-11 ~ 2025-05 |
| 13 | 5.15 | 74, 123, 148, 170, 178, 180 | 2023-01 ~ 2025-05 |
| 14 | 6.1 | 78, 90, 99, 124, 145 | 2024-06 ~ 2025-09 |
| 15 | 6.6 | 50, 66, 102 | 2024-10 ~ 2025-10 |

总计 **19 个版本组合**

---

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--android`, `-a` | Android 版本 (android12/13/14/15) | android14 |
| `--kernel`, `-k` | Kernel 版本 (5.10/5.15/6.1/6.6) | 6.1 |
| `--sub-level`, `-s` | Sub level 版本或 X (LTS) | 124 |
| `--os-patch` | OS Patch Level | 2025-02 |
| `--revision` | Android 12 Revision | - |
| `--ksu-version` | SukiSU-Ultra 版本 (Stable/Dev) | Stable(标准) |
| `--ksu-commit` | 指定 SukiSU-Ultra commit hash | latest |
| `--susfs-commit` | 指定 SUSFS commit (hash 或 HEAD~N) | latest |
| `--zram` | 启用 ZRAM (LZ4KD) | False |
| `--no-kpm` | 禁用 KPM | False |
| `--bbg` | 启用 Baseband-guard | False |
| `--op8e` | 启用 OnePlus 8E 支持 | False |
| `--bbr` | 设置 BBR 为默认拥塞算法 | False |
| `--no-release` | 不创建 GitHub Release | False |
| `--custom-version` | 自定义版本名称 | - |
| `--matrix`, `-m` | 使用预定义矩阵 | - |
| `--all` | 构建所有配置 | - |
| `--list-configs` | 列出所有支持的配置 | - |
| `--list-matrix` | 列出所有预定义矩阵 | - |
| `--dry-run` | 仅验证配置 | - |
| `--workspace`, `-w` | 工作目录 | /tmp/gki-build |

---

## 下载与刷入（重要）

1. **推荐：`*-boot-gz.img`**（gzip 压缩内核）
   - 用 `fastboot flash boot <文件名>` 直接刷入。
   - **联发科天玑 / android12-5.10 实测：只有 gzip 变体能正常启动**，裸 `boot.img` 与 `boot-lz4.img` 会导致无限重启，请勿刷入。
2. AnyKernel3.zip - 用 [HorizonKernelFlasher](https://github.com/libxzr/HorizonKernelFlasher/releases) 等工具刷入。

刷入前请**备份原厂 boot.img**（`fastboot` 可直接回刷恢复）。本 CI 用自签 `testkey.pem` 给 boot.img 加 AVB footer；若你的设备此前已能刷入本仓库自签的 boot.img 并正常开机，说明校验链已放行，**无需刷 vbmeta**。若刷入后无限重启且已排除压缩格式/ramdisk/KMI 因素，才把 `fastboot --disable-verity --disable-verification flash vbmeta vbmeta.img` 作为排障手段（会削弱 Play 完整性认证，且 MTK 多 vbmeta 槽较敏感，操作前备份原厂 vbmeta.img）。

`BUILD_MANIFEST.txt` 记录了本次内核 / SUSFS / SukiSU_patch / KPM 补丁的真实 commit 与内核 `KSU_VERSION`，**安装 manager 时请核对版本是否同源**。

---

## 支持的功能

| 功能 | 说明 |
|------|------|
| [KernelSU](https://kernelsu.org/zh_CN/) | SukiSU 内核Root方案 |
| [SUSFS4](https://gitlab.com/simonpunk/susfs4ksu) | 内核层面辅助 KSU 隐藏的功能补丁 |
| [BBR](https://blog.thinkin.top/archives/ke-pu-bbrdao-di-shi-shi-me) | TCP 拥塞控制算法 |
| [LZ4KD](https://github.com/ShirkNeko/SukiSU_patch/tree/main/other) | 来自华为源码的 ZRAM 算法 |
| [KPM](https://github.com/bmax121/KernelPatch) | 内核模块支持 |
| [Baseband-guard](https://github.com/vc-teahouse/Baseband-guard) | 基带安全防护 |

<details>

<summary>支持的 ZRAM 算法（可在 Scene 切换）</summary>

LZ4K、LZ4HC、deflate、842、lz4k_oplus

</details>

---

## KSU 管理器（重要）

**本仓库不构建 manager APK**（README 旧版曾误称「编译完成后会生成最新的管理器 APK」，实际构建链里没有任何 gradle/APK 步骤）。

请自行安装 **与本内核同源（同 commit / 同 KSU_VERSION）的官方 SukiSU-Ultra manager APK**。内核侧 `KERNEL_SU_UAPI_VERSION`、`KSU_VERSION` 数字、manager 包名与签名信任表三者必须一致，否则 manager 会显示「未安装」，或显示已安装但模块 / KPM / App Profile 授权全部不可用。版本指纹见产物里的 `BUILD_MANIFEST.txt`。

---

## 紧急救援指南

> **触发条件**
> 当设备因刷入错误/不兼容的内核无法启动时需执行救援

1. 进入 Fastboot 模式
   - 物理键组合：电源+音量-
   - 或 ADB 命令：`adb reboot bootloader`

2. 执行刷写命令
```bash
fastboot flash boot <boot.img文件全称>
```

---

## 内核版本兼容性说明

### 1. 跨子版本刷机规则

当手机 GKI 主版本为 5.10.x 时（如 5.10.168），可刷写同主版本更高子版本的内核（如 5.10.198）。

关于 **X-lts** 版本，以 `android12-5.10.X-lts-AnyKernel3.zip` 为例：
- **X-lts** 表示长期支持版（子版本号最大，当前示例为 5.10.236）
- LTS 随着 GKI 源码更新，编译版本号将持续递增
- ⚠️ 注意：LTS 虽为最新，但最新版 ≠ 最稳定（如 6.6.x 存在自动重启 BUG）

### 2. 内核版本伪装方法

在 MT 管理器终端执行：
```bash
uname -r | sed 's/^[^-]*//'
```
获取后复制版本号，填入 Action 编译面板即可实现内核版本伪装。

### 3. 定制构建矩阵

编辑 `.github/workflows/config/matrix.json` 添加或修改构建版本：
```json
{
  "android14-6.1": [
    {"sub_level": "124", "os_patch_level": "2025-02"},
    {"sub_level": "145", "os_patch_level": "2025-09"}
  ]
}
```

---

## 构建系统架构

```
.github/workflows/
├── config/
│   └── matrix.json          # 构建矩阵配置
├── scripts/
│   ├── build.py             # 主构建脚本（CLI 入口）
│   ├── kernel_builder.py    # 内核构建核心类
│   ├── config.py            # 配置定义和验证
│   ├── matrix_generator.py  # GitHub Actions 矩阵生成
│   ├── release_generator.py # Release 说明生成
│   └── cache_manager.py     # 构建缓存管理
├── kernel-build.yml         # 单版本构建工作流
└── build-kernels.yml        # 全量构建工作流
```

### 核心组件

| 组件 | 功能 |
|------|------|
| `KernelBuilder` | 内核构建核心类，负责克隆源码、应用补丁、编译、打包 |
| `BuildConfig` | 构建配置数据类，包含所有构建参数 |
| `CacheManager` | 管理 ccache 和构建缓存，支持跨分支复用 |
| `matrix_generator.py` | 为 GitHub Actions 生成构建矩阵 |
| `release_generator.py` | 自动生成 Release 说明 |

### 仓库依赖

| 仓库 | 用途 |
|------|------|
| [SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) |  SukiSU-Ultra 源码和安装脚本 |
| [susfs4ksu](https://github.com/ShirkNeko/susfs4ksu) | SUSFS 内核补丁 |
| [SukiSU_patch](https://github.com/ShirkNeko/SukiSU_patch) | SukiSU-Ultra 附加补丁（ZRAM 等） |
| [AnyKernel3](https://github.com/WildPlusKernel/AnyKernel3) | 通用刷机包模板 |
| [kernel_patches](https://github.com/Tools-cx-app/kernel_patches) | 内核补丁合集 |
| [Baseband-guard](https://github.com/vc-teahouse/Baseband-guard) | 基带安全防护 |

---

## 更多内容

可以提及您的意见...我会尝试！
