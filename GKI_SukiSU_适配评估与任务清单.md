# SukiSU-Ultra / GKI_KernelSU_SUSFS 适配评估与任务清单

> 日期：2026-11-06　范围：`D:\LocalProject\androidProject\` 下三个仓库，全程只读调查
> 状态：**已完成评估，未改动任何代码/配置/仓库文件**（三个仓库 `git status` 均为干净）
> 补充（同日 17:04）：已纳入真机实测反馈，**目标锁定 `android12-5.10` + `boot-gz.img`（gzip）**，联发科天玑；Android 16 / 6.1+ 降为顺带。根因与收窄方案见 §10。

---

## 10. 【最新，优先级最高】真机实测反馈 → 根因与收窄后的执行方案

### 10.1 用户实测（一手证据，优先级高于静态推断）

| 组合 | 现象 |
|---|---|
| 8 月编的 ShirkNeko GKI img + **最近发布**的几个 SukiSU-Ultra | **显示「未安装」** |
| 同一个 img + **时间更旧**的 SukiSU-Ultra | **显示「已安装」，但只有基础功能**：无模块、无 KPM、设置 root 权限也不行 |
| 刷 `boot.img`（裸）/ `boot-lz4.img` | **无限重启**；只有 **`boot-gz.img`（gzip）能起** |
| 目标机 | **Android 12 / kernel 5.10 / 联发科天玑（MTK Dimensity）** |

### 10.2 根因（用本地 git 历史逐条坐实，不是猜测）

时间线：用户 img 卡在 8/27，而 SukiSU 内核侧在 9~10 月有一串**破坏性变更**：

```
2026-08-27  GKI 仓库末提交 814c5c2   ← 用户 img 的内核从这里编出
2026-08-27  builtin@4b21017a         ← 当时内核 UAPI=2、无 extension hub
2026-09-01  v4.2.0
2026-09-07  dda78702  feat: new version matching detection mechanism  ← 「未安装」
2026-10-19  edf86467  feat: unify kernel extensions under a single hub ← 模块/KPM/授权全废
2026-10-26  3b58d46f  feat(kpm): unify KernelPatch bridge (ABI v2)
2026-10-28  ad73573d  feat(extension): publish capabilities and ABI 2
2026-10-29  v4.3.0                   ← 用户装的「最近发布的几个版本」
```

`git show` 直接取到的硬数据：

| 项 | 8/27 旧内核（用户 img） | 当前 `builtin` | 当前 `main` / 新版 manager |
|---|---|---|---|
| `KERNEL_SU_UAPI_VERSION` | **2** | **6** | 6 |
| `KSU_IOCTL_EXTENSION`（`'K',300`） | **不存在**（`git grep` 空） | 存在 | 存在 |
| `uapi/extension.h` | **不存在** | `kernel/include/uapi/extension.h` 存在 | 根 `uapi/extension.h` 存在 |
| extension hub / `KSU_EXTENSION_API_VERSION` | 无 | 2 | 2 |
| KPM bridge ABI | 旧 | v2 | v2 |

三条现象逐条归因：

1. **「未安装」= 版本判定硬拒**，不是内核没 root。`dda78702`（9/7）把判定改成 `isManager && kernelUAPIVersion == managerUAPIVersion && rootAvailable()`；`Natives.kt` 还有 `MINIMAL_SUPPORTED_KERNEL = 32513`、`MINIMAL_SUPPORTED_KERNEL_FULL = "v4.0.0"`，并用 `isVersionLessThan(getFullVersion(), "v4.0.0")`。旧内核 UAPI=2、`GET_FULL_VERSION` 返回 8 月的 `v3.x/4.1.x` 串 → 新版 manager 直接判「未安装 / 需更新内核」。
2. **「已安装但只有基础功能」= IOCTL 命令空间错配**。旧版 manager 用老命令集能读到 root（所以显示已安装），但**模块、KPM、App Profile / root 授权在新版里已迁到 `KSU_IOCTL_EXTENSION`（'K',300）扩展 hub + 新 profile/feature 语义**；旧内核没有 300 号分发 → 全部 `unsupported command` → 基础 `su` 在、模块/KPM/授权全废。与社区 SukiSU [issue #1001](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/1001) 的 `ksu ioctl: unsupported command 0x80104b02 / 0xc0044b06` 同类。
3. **KPM 额外还差一层**：8/27 的 `patch_linux`（`ShirkNeko/SukiSU_patch` main）对应旧 KPM 接口，而现在 `kernel/extension/kpm/bridge.h` 是 **Kernel-only ABI v2**（三个 magic + op 1..8，含 `LOAD_EVENT=8`）。即使镜像打过补丁，ABI 也对不上。官方 [SukiSU_KernelPatch_patch](https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU_KernelPatch_patch/main/README.md) 明确是「剪裁自 bmax121/KernelPatch，用于给 SukiSU Ultra 提供 KPM 支持」，只支持 arm64、Linux 3.18–6.12（理论值）——**5.10 在支持范围内，这一层不是障碍**。

> **结论**：用户判断「版本不识别 + 新版 SukiSU 有些东西在旧镜像里不兼容」**完全正确**。修复本质是：**用当前 SukiSU-Ultra 内核源码重新编译 5.10 GKI 内核，并同步配套 KPM 补丁 / SUSFS 补丁 / manager APK**。注意这不是「改 manager 让它认旧内核」——那只能骗过显示、功能仍是残的；也不是 `version_spoof` 能解决的（它只能伪造 GET_INFO/GET_FULL_VERSION，造不出 IOCTL 300 分发）。

### 10.3 一个必须澄清的好消息：GKI 仓库其实会自动拉新版内核

`kernel/setup.sh:48` 有 `git pull`，`kernel_builder.py:253` 每次都从 `raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/kernel/setup.sh` 拉脚本再 `bash -s builtin`。所以**现在重新跑一次 workflow，理论上编出来的就是带 UAPI 6 + extension hub 的新内核**。

但这里有坑，正是「不能只靠重跑」的原因：
- `setup.sh` 拉的是 **main 分支的 setup.sh**，内部却 `git checkout builtin` —— 两分支混用，`builtin` 与 `main` 分叉 855/141 提交，行为随上游漂移，**不可复现**；
- `setup.sh:50` 无参数时会 `git checkout "$(git describe --abbrev=0 --tags)"`，与 `bash -s builtin` 语义叠加更乱；
- 全仓库 `check=False`，patch/curl 失败静默吞掉，可能出现「重跑了但仍旧是旧行为」的假成功；
- 用户手上的是 8 月的**产物**，不是 8 月的**仓库** —— **重新编译**才是关键，仓库代码本身不需要「换上游」。

### 10.4 收窄后的执行方案（只做 android12-5.10 + gz，其余顺带）

**主目标**：`android12-5.10`，产出**可直接 `fastboot flash boot` 的 `boot-gz.img`**（gzip kernel），刷入后：不无限重启 / 新版 manager 显示已安装 / 模块、App Profile 授权、SUSFS、KPM 全正常。

| 优先 | 任务 | 具体改动 | 为什么必须做 |
|---|---|---|---|
| **P0** | **把内核源码钉死到确定的新 commit** | `config.py:67-69` 加 `setup_branch=builtin` + `setup_commit=<40位 SHA>`；`kernel_builder.py:248-259` 改为 `git clone --depth 1 -b builtin … KernelSU` → `git checkout <pinned>` → 执行仓库内 `kernel/setup.sh`，不再 `curl main 的 setup.sh` | 保证编出来的是 UAPI 6 + extension hub；消除「main 脚本 / builtin 源码」混用 |
| **P0** | **同源配套三件套** | 同一次构建里锁定 `SukiSU_patch`（`kpm/patch_linux` + `69_hide_stuff.patch`）、`susfs4ksu` 的 `gki-android12-5.10` 分支到确定 commit；Release 同时发布**与该 commit 同源的官方 manager APK**（README:135 说「会生成 manager APK」是假的，全库无 gradle/apk 代码） | 「内核 UAPI 6」+「manager 同版本」+「ksud 同版本」三者缺一即显示未安装或功能残缺 |
| **P0** | **KPM 补丁换到 ABI v2 同源** | `config.py:96` `KPM_PATCH_URL` 改为 `SukiSU-Ultra/SukiSU_KernelPatch_patch` 或 `ShirkNeko/SukiSU_patch` 里与 `bridge.h` ABI v2 对齐的 `patch_linux`/`kpimg`，带 commit pin + sha256；`patch_kpm_image()`（`:629-645`）对 5.10 保持启用（原本就启用，无需动 6.6 硬跳过） | 8 月补丁是旧 ABI；新版 KPM 必须同源才 `kpm version` 成功 |
| **P0** | **只产/主推 `boot-gz.img`，并保住 ramdisk** | `_create_boot_image_variants():689-705` 保留 `Image.gz → boot-gz.img`；`_prepare_android12_boot_images():673-683` 的 `--ramdisk out/ramdisk` + Google 认证 boot 复用**必须保持**；默认产物把 `-gz` 标为推荐，`boot.img`/`boot-lz4.img` 标「本机型禁用」 | 天玑 5.10 实测只有 gz 能起；android12 是唯一带 ramdisk 的路径，别动它 |
| **P1** | **补签名 + 让失败显性化** | 把 `build-kernels.yml:121-127` 的 `BOOT_SIGN_KEY` 步骤复制到 `kernel-build.yml`（单版本 workflow 目前无 key → `avbtool` 静默失败 → 无 AVB footer）；关键 `_run_cmd` 改 `check=True` | 避免「构建成功但镜像无签名/无补丁」；AVB 自签需文档写明 `--disable-verification` 前置 |
| **P1** | **产物自检（针对 gz）** | 新增：`boot-gz.img` 的 kernel 确实是 gzip（`gzip -t`）、`unpack_bootimg` 回读有 ramdisk、AVB footer 存在（`avbtool info_image`）、KPM 补丁痕迹、`BUILD_MANIFEST.txt` 记录内核/manager/SUSFS/KPM 的 commit | 构建失败要比刷机变砖好 |
| **P1** | **去掉 config.py 的 import 联网** | `config.py:9-37` `get_susfs_version()` 改本地常量，避免 Release tag fallback 成固定 `v2.1.0` 覆盖 | 可复现 |
| **P2（顺带）** | 6.1 / 6.6 / Android 16 | 只做**通用修复**（版本 pin、签名、`check=True`、Release tag 唯一化、6.6 删 KPM 硬跳过），**不做** init_boot/vendor_boot/vendor_dlkm 等机型化完整镜像 | 用户明确「安卓 16 可先不处理」；通用改动本就要动同一批文件，成本近乎为零，但不为它承担真机验证责任 |

**明确不做**（收窄后）：不做 A′（WildKernels 重新 fork）；不做 Android 13+ 的 init_boot/vendor_boot 完整镜像链（阶段 2 的大改动对 android12-5.10 用不上，`_prepare_android12_boot_images` 已带 ramdisk）；不改 SukiSU-Ultra 本体 / BakaSU / KPatch-Next-Module；不靠 `version_spoof` 伪造版本骗 manager。

### 10.5 真机验收（5 条，缺一不可）

1. `fastboot flash boot android12-5.10.<sub>-<patch>-boot-gz.img` 后能进系统，不无限重启（保留 stock boot.img、确认 A/B 槽位与 vbmeta 状态）；
2. 安装**同源** manager APK → 显示「已安装 / 工作中」，版本号与 `BUILD_MANIFEST.txt` 一致，无「需更新内核」提示；
3. 模块页面可安装/挂载模块，App Profile 里给指定 App 设置 root 权限生效（专治「设置 root 权限也不可以」）；
4. `ksud susfs status` 返回版本、隐藏项齐全；
5. `ksud kpm version` 成功且能加载一个测试 KPM；常规 root 隐藏（检测类 App / Play 完整性）通过。

> 排障指引：若第 2 条仍「未安装」→ 读 `BUILD_MANIFEST.txt` 里内核 `KSU_VERSION_FULL` 与 manager 版本是否同源；若第 3 条仍残缺 → `dmesg | grep -i ksu` 看是否 `unsupported command`（说明内核 pin 没生效，仍编出了旧 UAPI 2 的内核）。

---

## 0. 一句话决断

**走「方案 A」：以你本地这份 `ShirkNeko/GKI_KernelSU_SUSFS` 为基线做修复式改造，不要去改 WildKernels r22 / KPatch-Next-Module（方案 B），也不要去魔改 SukiSU-Ultra 本体。**

但方案 A 的目标要重定义 —— 不是「让它接入新的 SukiSU-Ultra」，而是 **「把已有的 SukiSU-Ultra 接入做对：版本锁定 + fastboot img 完整性 + 6.6 的 KPM」**。原因见 §2。

**另一个需要你拍板的岔路（方案 A′）**：`ShirkNeko/GKI_KernelSU_SUSFS` 其实是 `WildKernels/GKI_KernelSU_SUSFS` 的 fork。WildKernels 上游 **2026-11-06 仍在活跃更新**，且已有一套明显更成熟的机制：verified commit pins（`PIN_SUKISU_ULTRA = builtin@1cfa5734` 等精确 40 字符 SHA）、`root-setup` action 拒绝混合多 root 实现、`trim_sukisu_managers.py` 裁证书表、16k/4k page size、`allow_version_mismatch` 开关、Samsung 6.6 KDP 符号补丁、按 flavor 分别配 manager APK。所以严格说存在两条子路线：

- **A（保守）**：就地修 ShirkNeko 这份 —— 改动面小、KPM 与 fastboot img 已有骨架、风险可控；
- **A′（激进）**：以 WildKernels 最新 main 重新 fork，把 KPM 补丁链 + fastboot img 产出「加回去」 —— 可复现性和机型适配更好，但**上游明确不产直接可刷 img、且完全不设 `CONFIG_KPM`/不集成 KernelPatch**，等于要在更干净也更复杂的流水线上重做两大块，工作量最大。

本清单按 **A** 编排；A′ 仅登记为备选（§7）。若后续发现 ShirkNeko 这套的 SUSFS 补丁链已随 SukiSU `builtin` 分支漂移到修不动，再切 A′。

---

## 1. 先纠正三个前提（实测与描述不符）

| 你的描述 | 仓库实际状态 | 证据 |
|---|---|---|
| GKI_KernelSU_SUSFS 几个月没更新，需要适配到新的 SukiSU-Ultra | **确实停更（末提交 2026-08-27），但它 2026-05-29「完全重构」时已切到 SukiSU-Ultra**，并非还在用老 KernelSU；且 `setup.sh` 每次构建会 `git pull`，理论上会自动拉新内核 | `config.py:67-69`；`README.md:1`；SukiSU `setup.sh:48`；详见 §10.3 |
| 需要额外产出 fastboot 刷的 img | **已经产出 3 个 boot.img 变体**（`boot.img` / `boot-gz.img` / `boot-lz4.img`）+ AnyKernel3.zip，且都进 Release | `kernel_builder.py:689-705` `_create_boot_image_variants()`；`README.md:104-110` |
| 需要额外让内核支持 KPM | **已经内置**：`CONFIG_KPM=y` 默认开、`use_kpm=True` 默认开、编译后对 Image 跑 KernelPatch 补丁 | `kernel_builder.py:64`、`config.py:109`、`kernel_builder.py:629-645` `patch_kpm_image()` |

结论：这不是「从零适配」，而是「修一套已经跑通但锁死在旧状态、且有几处静默失效的流水线」。这反而降低工作量，但**风险点更隐蔽**。

### 一个此前没人注意到的关键事实

`kernel_builder.py:253` 执行的是：

```python
self._run_cmd(f"curl -LSs {setup_url} | bash -s builtin", check=False)
```

`setup.sh:49-53` 把参数当 `git checkout <arg>` 用，所以 **GKI 构建实际检出的是 SukiSU-Ultra 的 `builtin` 分支，不是 `main`**。该分支与 `main` 分叉 855 / 141 个提交，目录结构都不同（`builtin` 无 `kernel/Kbuild`、无根 `uapi/`，UAPI 头在 `kernel/include/uapi/`），但它：

- 最近仍在更新（末提交 2026-11-01），不是死分支；
- `KERNEL_SU_UAPI_VERSION = 6`，与 `main` 一致 → **当前 HEAD 的内核↔manager 协议层兼容**（但 8/27 编出的旧镜像是 UAPI=2，见 §10，这正是用户实测故障的根因）；
- 版本号公式与 `main` 完全相同（`40000 + commit数 - 2815`，`REPO_BRANCH := main`）→ 数字版本同源；
- 带完整 13 条 `CONFIG_KSU_SUSFS_*` 选项 → **正好匹配 GKI 仓库写死的 `KERNEL_CONFIG_TEMPLATE`**。

也就是说：这套构建链是**故意**用 `builtin` 分支来叠 SUSFS 的（`main` 分支已移除 SUSFS 实现，只剩占位 handler）。这条路本身成立，问题在于「没有 pin」——`setup.sh` 里 `git checkout "$(git describe --abbrev=0 --tags)"` 与 `bash -s builtin` 混用，实际版本随上游 tag 漂移，不可复现。

---

## 2. 为什么方案 B（改 WildKernels r22 让它支持 KPM）走不通

你实测「BakaSU + WildKernels r22 的 AnyKernel3.zip + KPatch-Next-Module，刷入成功、正常使用，但 KPM 功能无法用」——原因已经查清，是**四层缺失叠加**，补一层没用：

1. **BakaSU 主动删掉了 KPM**。commit `faac3098`（2026-06-06）「kernel, ksud, manager, docs: remove KPM support (#226)」把内核 KPM/KernelPatch 代码、`ksud` 的 KPM 逻辑、manager 的 KPM UI、`kpimg`/`kptools` assets 全部移除。现在只剩 `kernel/compat/apatch_conflict.c` 里的探测常量和 uapi 注释（`102 = ENABLE_KPM deprecated`）。**它的内核里根本没有 KPM 桥接层**。
2. **KPM 不是「刷个模块就能开」的功能**。SukiSU-Ultra 的 `kernel/extension/kpm/kpm.c:24-58` `sukisu_kpm_bridge()` 是靠 KernelPatch 把 `__arm64_sys_truncate` 替换成 supercall 分发器来工作的；源码注释写明：「*Without KP this is a real truncate handler ... yields an error*」。所以必须同时满足：`CONFIG_KPM=y`（编译进桥接层）**且** 用 `kptools -p -s 123 -i Image -k kpimg -o oImage` 给内核镜像注入 KernelPatch 内核态。缺任一个，`ksud kpm version` 都返回 `-EOPNOTSUPP`。
3. **KPatch-Next-Module 是 KernelSU-Next 系的**。其 README 明确写：KPatch-Next binaries 来自 `KernelSU-Next/KPatch-Next`，补丁脚本来自 APatch。它给内核打的是 KernelPatch-Next 的 `kpimg`，ABI 与 SukiSU-Ultra `kernel/extension/kpm/bridge.h` 的 **ABI v2**（三个 framing magic + op 1..8）并不同源。而且它只是「给现有 boot.img 打补丁」的模块，**不会凭空给内核补上 `CONFIG_KPM` 桥接层**。
4. **WildKernels r22 的 AK3 内核侧没有 SukiSU 的 KPM 桥**。它是 KernelSU-Next / SUSFS 系构建，与 BakaSU 组合后 UAPI 与签名信任表也不通用（BakaSU 无 `uapi/extension.h`、`manager.h`，IOCTL 102 已 deprecated，而 SukiSU 把 102 用作 `SET_SPOOF_VERSION`）。

补 KPM 模块 ≠ 补内核 KPM 桥 + 同源 KernelPatch 补丁。方案 B 需要从内核构建层重做，等于重写方案 A 的一大半，还丢掉了 SukiSU-Ultra 官方认可的内核基线。**弃。**

**联网证据（逐条可查）**：

- `KPatch-Next-Module` 是**用户态 root 模块 zip**，不是内核模块。`module/service.sh` 第一行就是 `if [ -z "$(kpatch hello)" ]; then touch "$MODDIR/unresolved"; exit 0; fi` —— 探测不到内核里的 KernelPatch 就直接退出，后面的 `kpatch kpm load` 全不执行。它 `build.sh` 只是把 `KernelSU-Next/KPatch-Next` 的 `kpatch`/`kpimg`/`kptools` 二进制塞进模块，`customize.sh` 还明确 `APatch is unsupported`。→ 你「刷了模块但 KPM 不能用」的直接原因就是 `kpatch hello` 失败。[README](https://raw.githubusercontent.com/KernelSU-Next/KPatch-Next-Module/main/README.md) · [service.sh](https://raw.githubusercontent.com/KernelSU-Next/KPatch-Next-Module/main/module/service.sh) · [build.sh](https://raw.githubusercontent.com/KernelSU-Next/KPatch-Next-Module/main/build.sh)
- **WildKernels 当前 main 既不开 `CONFIG_KPM`、也不集成 KernelPatch、更不产直接可刷的 boot.img**。它的 `main.yml` 里 `root_flavor` 可选 SukiSU-Ultra 等，但 `root-setup` 只做 `CONFIG_KSU=y`；产物只有 `AnyKernel3/**` + `BuildInfo`，官方文档要求用户自己拿**本机 stock boot.img** 用 magiskboot 替换 `Image` 后重打包。[main.yml](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/.github/workflows/main.yml) · [build.yml](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/.github/workflows/build.yml) · [magiskboot.md](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/docs/magiskboot.md)
- 你用的 **r22 属于旧的 v1.5.9 世代**（tag `v1.5.9-r22` → commit `9e0fc06b`，2025-09-07），那代集成的是 `WildKernels/Wild_KSU` 的 `wild` 分支（WKSU），**当时确实直接产 `boot.img`/`boot-gz.img`/`boot-lz4.img`**（`mkbootimg.py --header_version 4` + `avbtool`），但既不是 SukiSU-Ultra 也不带 KPM。[v1.5.9-r22 build.yml](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/v1.5.9-r22/.github/workflows/build.yml)
- 社区一手故障样本（全部印证「`CONFIG_KPM=y` ≠ 内核已打 KP 补丁」）：SukiSU [issue #29](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/29)、[#239](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/239)、[#758](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/758)（`/proc/config.gz` 显示 `CONFIG_KPM=y` 但 manager 报未配置）、[#37](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/37)（贴出完整 `patch_linux` 输出：`kpimg version: b03` / `superkey: 123` / `patch done: oImage`）、[#643](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/643)。
- **KPM 补丁本身有稳定性代价**：SukiSU [issue #362](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/362)（android14-6.1 打 patch 后开 App 黑屏重启）、WildKernels [#126](https://github.com/WildKernels/GKI_KernelSU_SUSFS/issues/126)（6.6 开 KPM 随机重启/冻结、Actions 构建失败）、官方 `docs/README.md:54`「may affect system stability」。这给任务 0.4 / 3.1 增加了实测必要性：6.6 上「删掉硬跳过」之前必须先确认补丁源可用且稳定。

> 顺带：SukiSU-Ultra 官方 `installation.md:17-18` 推荐两个配套内核 —— `ShirkNeko/GKI_KernelSU_SUSFS`（你手上这份）与 `MiRinFork/GKI_SukiSU_SUSFS`（「最接近 GKI 的内核，适用于大多数设备」），并明确标注二者「add susfs, KPM」。API 确认 ShirkNeko 这份是 WildKernels 的 fork、`pushed_at 2026-08-27`，比 WildKernels 上游（2026-11-06）落后约两个月。若后续发现 ShirkNeko 这套修不动，`MiRinFork` 是更干净的替代基线，仍属于方案 A 范畴。

---

## 3. 为什么「不改 SukiSU-Ultra 本体」

`kernel/Kbuild:202-219` 把 manager 包名（`com.sukisu.ultra`）、APK 签名期望 `KSU_EXPECTED_SIZE=0x35c`、`KSU_EXPECTED_HASH=947ae944...eef` 编译进内核，决定 `isManager` 判定；`uapi/supercall.h:15` 的 `KERNEL_SU_UAPI_VERSION=6` 被 `ksud/src/ksucalls.rs:224-233` 强校验，不等就直接 bail「UAPI version mismatch」并阻断 init/module/soft-reboot。

魔改内核侧源码 → UAPI / 数字版本 / 签名表漂移 → 官方 manager 认不出内核。内核侧必须保持与 manager/ksud **同源同 commit**，改动只允许落在 GKI 构建仓库（怎么拉、怎么配、怎么打补丁、怎么打包）。

---

## 4. 三条硬需求的逐条归因

### 需求 1：刷入的 gki 文件不能导致无限重启

当前产物的**四个真实缺陷**（这才是「fastboot 直刷」的重点，而不是「img vs zip」）：

| # | 缺陷 | 位置 | 后果 |
|---|---|---|---|
| 1 | **android13/14/15 的 boot.img 不含 ramdisk** | `kernel_builder.py:685-687` `_prepare_boot_images_generic(has_ramdisk=False)`；只有 android12 走 `:673-683` 下载 Google 认证 boot 解包取 ramdisk | 裸 kernel + header 的镜像通常不是完整可启动镜像 |
| 2 | **单版本 `kernel-build.yml` 没有 `BOOT_SIGN_KEY` 步骤** | 只有 `build-kernels.yml:121-127` 设置 `BOOT_SIGN_KEY_PATH`；`clone_toolchain():174-175` 仅在环境变量已存在时透传 | `$BOOT_SIGN_KEY_PATH` 为空 → `avbtool add_hash_footer` 失败，且 `check=False` 静默吞掉 → 产出**无 AVB footer** 的 boot.img |
| 3 | **header/分区/算法全硬编码** | `kernel_builder.py:698-702`：`--header_version 4`、`--partition_size 64MB`、`--partition_name boot`、`SHA256_RSA2048` | 与机型实际 boot 分区布局不符 |
| 4 | **完全没有 init_boot / vendor_boot / dtb / vendor_dlkm 路径** | 全仓库 grep 无命中；`:607` `BUILD_SYSTEM_DLKM=0` 且删 `MODULES_ORDER` | Android 13+ GKI 的 generic ramdisk 在 `init_boot`、vendor ramdisk + dtb 在 `vendor_boot`，缺这些会起不来或功能不全 |

**AVB 是最大的隐性坑**：CI 用自定义 `testkey.pem`（GitHub Secret）签 boot.img，而设备 `vbmeta` 里存的是 OEM key 的 hash。AOSP 明确写 boot v4 的 `boot_signature` **只用于 VTS 完整性检查，不参与设备端 verified boot**，OEM 必须用设备专属 AVB key 对 prebuilt boot.img 签名；`init_boot` / `vendor_boot` 各自还有 chained vbmeta 与 rollback index。刷进去校验不过 → 无限重启。必须用 `fastboot flash vbmeta --disable-verity --disable-verification`（或设备已处于该状态）才能用自签镜像；README 的「救援 `fastboot flash boot`」在没禁用校验的设备上根本救不了，这点必须在文档里讲清楚。[generic-boot](https://source.android.com/docs/core/architecture/partitions/generic-boot) · [vendor-boot-partitions](https://source.android.com/docs/core/architecture/partitions/vendor-boot-partitions) · [AVB](https://source.android.com/docs/security/features/verifiedboot/avb)

**AOSP 一手分区事实（决定阶段 2 怎么做）**：

- Android 12 的 `boot` = generic ramdisk + GKI kernel；**Android 13 首发设备把 generic ramdisk 移到独立的 `init_boot`，`boot` 只剩 GKI kernel**；`vendor_boot`（v3/v4）承载 vendor ramdisk + `lib/modules` + **DTB** + bootconfig，且 bootloader「必须同时访问 boot 和 vendor_boot 才有足够数据完成启动」。→ 只换 kernel 不处理配套分区必挂。
- **GKI 用 lz4 压缩的 generic ramdisk，合规设备的 vendor ramdisk 也必须 lz4**，两段 cpio 拼接不能留空隙；boot header v4 的 `os_version` 必须为 0（版本信息改从 AVB properties 取），DTB 自 v3 起已从 boot header 移到 vendor_boot。
- first-stage init 按 `modules.load` 顺序从 ramdisk `/lib/modules` 加载厂商 `.ko`（还依赖 `modules.dep`/`softdep`/`alias`/`options`）；未签名模块只能用 allowlist 上的符号。**换内核导致 KMI/vermagic/符号 CRC 不匹配 → 模块加载失败 → 挂不上分区 → bootloop**；反证是 WildKernels 流水线里有专门的 `allow_version_mismatch` 开关和 Samsung 6.6 KDP 符号补丁（否则 `rust_binder.ko` 会 `put_ipc_ns undefined`）。
- KernelSU 官方：KMI 是 `w.x-androidNN-k` 格式（**SubLevel 不属于 KMI**），KMI 不同「flashing a kernel image with a different KMI may cause a bootloop」；安全补丁级别低于已烧录值会因 anti-rollback 导致 bootloop；压缩格式错（Pixel 常用 `lz4_legacy`）也直接 bootloop。[installation](https://kernelsu.org/guide/installation.html)
- 真实样本：SukiSU [issue #683](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/683) Pixel6 刷 ShirkNeko 预编译 boot.img 反复进 bootloader，且 `fastboot -w` 后又挂；[issue #1001](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/1001) 的 `ksu ioctl: unsupported command 0x80104b02` 是内核/用户态 ABI 错配导致「root 可用但模块/超用户菜单不可用」。

> 关于「安卓 6 开始只能用 AnyKernel3.zip」：GKI 设备（Android 12+）用 `fastboot flash boot` 刷**完整**的 boot.img 是可行的。AK3 更稳是因为它在设备上**就地解包原 boot.img → 替换 kernel → 重打包**，自动保留原 ramdisk / dtb / 签名链。社区标准动作（KernelSU 与 WildKernels 文档一致）：**拿本机 stock boot.img → `magiskboot unpack` → 只替换 `Image` → `repack` → 先 `fastboot boot new-boot.img` 临时试启 → 通过后再 `fastboot flash boot`**，并保留 stock 镜像可回滚。[magiskboot.md](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/docs/magiskboot.md)
>
> 所以「重点处理 img」的正解有两层，按优先级：**① 强烈建议在 CI 里直接产出「基于设备 stock boot.img 重打包」的完整镜像**（把「提供 stock boot.img」做成 workflow 输入/base64 secret，`magiskboot` 只替换 kernel）；**② 退路是 CI 产裸 Image + 一套一键重打包脚本 + 明确 AVB 前置条件**。仅靠 `mkbootimg --kernel Image` 拼出来的裸 boot.img（当前做法）在 Android 13+ 上不具备可启动性。无论哪条，都必须显式处理 KMI 完整匹配、压缩格式、page size 4k/16k、header version、init_boot/vendor_boot、vbmeta、A/B 槽位。

### 需求 2：SukiSU-Ultra 能识别刷入的 gki 内核

协议层其实**已经兼容**（builtin 分支 UAPI=6、版本公式同源、包名/签名常量同源），真正的问题是：

- **README 说谎**：`README.md:135-139`「编译完成后会生成最新的管理器 APK」，但 GKI 仓库全库 grep 不到 `manager` / `.apk` / `gradle` / `assemble` —— **manager APK 根本没被构建**。你必须自己装与内核同源的官方 manager APK。
- **版本数字会漂移**：内核 `KSU_VERSION = 40000 + (GitHub API 查 main commit 数) - 2815`，而 manager APK 是另一个时间点构建的 → 两者数字版本可能不等，`HomeUiState.kt:92-93` 会报「版本不匹配」（UAPI 相同仍能工作，且有 `version_spoof` 扩展可伪造，但「功能正常无异常」不该靠这个）。
- **构建不可复现**：`setup.sh` 固定 `main` 却检出 `builtin`、无 commit pin、`--depth=1 --no-tags`、内核源码分支由 `os_patch_level` 决定且内容随时间变。同一份 workflow 今天和上周编出来的内核不是同一个东西。
- **`config.py` 在 import 时联网**且关闭 TLS 校验（`:9-37`），失败 fallback `"v2.1.0"` → 被 `matrix_generator.py:77` 当成 **Release tag** → 多次构建互相覆盖。

### 需求 3：sukisu 全部功能正常（SUSFS / KPM / 常规隐藏）

| 功能 | 现状 | 缺口 |
|---|---|---|
| SUSFS4KSU | `gki-{android}-{kernel}` 分支补丁 + `cp fs/ include/linux/` + `patch -p1 --fuzz=3` + 13 条 `CONFIG_KSU_SUSFS_*` | 分支无 pin；`6.6` 强制 `CONFIG_KSU_SUSFS_SUS_PATH=n`（`:414-417`）；`69_hide_stuff.patch` 是按旧目录结构打的老补丁；需核对 manager 侧 `MAX_SUSFS_VERSION` |
| KPM | `CONFIG_KPM=y` + 编译后 `curl .../kpm/patch_linux` 给 Image 打 KernelPatch 补丁 | **头号硬伤：`:630` `if not use_kpm or kernel_version == "6.6": return` —— android15-6.6 三个组合默认 `use_kpm=true` 却完全不打补丁**；`patch_linux` 是下载的预编译 ELF、无 pin、无 sha256、`chmod 777` 直接执行、与换上游不同步；`--no-kpm` 只跳过打补丁但 `CONFIG_KPM=y` 仍写死（`:64`）；`image_dir` 靠 android 版本猜路径（`:635-638`），路径一变静默 `return` |
| 常规 root / 隐藏 | `CONFIG_KSU=y` + `69_hide_stuff.patch` + SUSFS | 依赖上面两项链路完整 |

另外：**全仓库几乎所有 `_run_cmd(..., check=False)`**（clone 失败、curl 404、patch 冲突、avbtool 失败全被吞掉），只有 `build_kernel():620` 检查 returncode。这是「不能出奇怪异常」的最大敌人——失败会静默产出一个「看起来成功但功能残缺」的镜像。

---

## 5. 任务清单

> 约定：路径相对 `D:\LocalProject\androidProject\GKI_KernelSU_SUSFS\`。每项都标了「改动文件 / 验收标准」。
> **本轮不执行任何一项，仅输出清单。**

### 阶段 0 · 固化基线（0 代码改动，先做）

| # | 任务 | 改动文件 | 验收标准 |
|---|---|---|---|
| 0.1 | 记录三仓库当前 HEAD / 分支 / tag / remote，形成 `pins.json` 草案 | 无（产出笔记） | 一份可复用的 commit 清单，含 SukiSU-Ultra `main`/`builtin` 各自 HEAD、`susfs4ksu` 各 `gki-*` 分支 HEAD、`SukiSU_patch`、`AnyKernel3 gki-2.0`、AOSP build-tools / mkbootimg 分支 HEAD |
| 0.2 | 明确内核侧真源 = `SukiSU-Ultra/builtin` 分支，并确认这一选择是有意的（因为它带 `CONFIG_KSU_SUSFS_*`） | 无 | 书面确认；若改用 `main`，则 `KERNEL_CONFIG_TEMPLATE` 的 13 条 SUSFS 配置与 `50_add_susfs_in_gki-*.patch` 全部要重写 |
| 0.3 | 确认目标机型 / Android 版本 / 内核版本 / sub_level / os_patch / 当前 AVB 与 vbmeta 状态 / 是否已刷 `vbmeta --disable-verification` | 无 | 拿到确切值替换 `[DEVICE]` `[ANDROID]` `[KERNEL]` `[SUB_LEVEL]` `[OS_PATCH]`；未提供前所有任务用占位符 |
| 0.4 | 确认 `ShirkNeko/SukiSU_patch` 是否提供 **6.6 可用**的 `kpm/patch_linux`（当前 `patch_linux` 与 `kpimg` 是否随 SukiSU-Ultra bridge.h ABI v2 同步） | 无（联网核对） | 明确 6.6 KPM 是「删掉硬跳过即可」还是「需要另找补丁源」 |

### 阶段 1 · 可复现性与版本锁定（**最高优先**，直接决定需求 2）

| # | 任务 | 改动文件 | 验收标准 |
|---|---|---|---|
| 1.1 | 去掉 `config.py` import 时的联网副作用（`get_susfs_version()`），改为本地常量或独立 CI step 输出 | `scripts/config.py:9-37` | `python -c "import config"` 在断网下 <1s 完成且无 HTTP 请求；Release tag 不再可能因 fallback 固定成 `v2.1.0` |
| 1.2 | 给 SukiSU-Ultra 加 `setup_branch` / `setup_commit` / `setup_tag` 字段，统一 `repo_url`（目前是死配置）与 `setup_script` | `scripts/config.py:67-69` | 单一真源，改一处即可切版本 |
| 1.3 | 重写 `add_kernelsu()`：改为 `git clone --depth 1 -b <branch> <repo_url> KernelSU` → `git checkout <pinned_commit>` → 执行仓库内 `kernel/setup.sh <arg>`；去掉「拼 commit 到 raw URL + 再 checkout」的双路径 | `scripts/kernel_builder.py:248-259` | 指定 `--ksu-commit` 时内核源码版本确定且可复现；不再依赖 raw.githubusercontent 的 setup.sh |
| 1.4 | 让 `kernelsu_version`（Stable/Dev，目前是死参数，只在 `telegram_notify.py:158` 显示文字）真正映射到 tag / 分支 | `scripts/config.py:54-57`、`scripts/build.py:61,90`、`scripts/kernel_builder.py` | 选 Stable 走最新 tag、选 Dev 走 `dev`/`main` HEAD，行为可验证 |
| 1.5 | 合并两份已漂移的矩阵（`matrix.json` 与 `build.py:22-51` 的 `DEFAULT_BUILD_MATRIX`，后者多一条 `android13-5.15 / 189 / 2025-09`）为单一真源 | `workflows/config/matrix.json`、`scripts/build.py:22-51`、`scripts/matrix_generator.py` | `--list-configs` 与 `--list-matrix` 输出一致，19 个组合 |
| 1.6 | 给所有外部依赖加 commit pin + sha256：SUSFS、`SukiSU_patch`（含 `patch_linux`、`69_hide_stuff.patch`）、`AnyKernel3 gki-2.0`、build-tools、mkbootimg、`patch_linux` 二进制 | `scripts/config.py`、`scripts/kernel_builder.py` | 产物附 `BUILD_MANIFEST.txt`，含每个依赖的 commit / URL / sha256 |
| 1.7 | `build-kernels.yml:287` 的 Release `tag_name` 改成带日期 + build id 的唯一值 | `workflows/build-kernels.yml` | 连续两次全量构建不会覆盖同一 tag |
| 1.8 | 随 Release 发布「内核侧版本指纹」：`KSU_VERSION_FULL`、`KERNEL_SU_UAPI_VERSION`、SukiSU commit、SUSFS commit，供与 manager 版本核对 | `scripts/release_generator.py`、`workflows/build-kernels.yml` | Release body 里能直接读到内核版本指纹 |

### 阶段 2 · fastboot img 完整性（**直接决定需求 1，最高风险**）

| # | 任务 | 改动文件 | 验收标准 |
|---|---|---|---|
| 2.1 | 把 `_create_boot_image_variants()` 参数化：`header_version` / `partition_size` / `partition_name` / `ramdisk` / `dtb` / `AVB 开关与算法` 全部进 `BuildConfig`，不再硬编码 | `scripts/kernel_builder.py:689-705`、`scripts/config.py:99-116` | 同一函数可产 boot / init_boot / vendor_boot，参数由配置驱动 |
| 2.2 | **新增「stock boot.img 基底复用」路径（首选）**：workflow 增加 `stock_boot` 输入（上传设备原版 boot.img / init_boot.img / vendor_boot.img，或 base64 secret），用 `magiskboot unpack` 只替换 `kernel` 后 `repack`，自动继承原 ramdisk / dtb / header / 压缩格式 / 签名链；把 android12 的「下载 GKI 认证 boot → 复用 ramdisk → 重打包」模板（`:673-683`）泛化为这个统一入口 | `scripts/kernel_builder.py:667-687`、两个 workflow、新增 `scripts/repack_from_stock.py` | 产物用 `unpack_bootimg` / `magiskboot` 回读，ramdisk + DTB 字段与原机一致；**不依赖硬编码 header/分区** |
| 2.3 | 按目标机型补 `init_boot.img`（Android 13+ generic ramdisk，LKM 模式 patch 这里）、`vendor_boot.img`（vendor ramdisk + `lib/modules` + DTB + bootconfig）、必要时 `dtbo.img` / `vendor_dlkm`；v4 header `os_version=0`、generic 与 vendor ramdisk 压缩格式一致（GKI 为 lz4）、page size 4k/16k 可选 | `scripts/kernel_builder.py`、`scripts/config.py`、两个 workflow | 产物清单覆盖目标机型的实际分区布局（由任务 0.3 的机型信息决定）；AOSP generic-boot / vendor-boot-partitions / boot-image-header 三项对照通过 |
| 2.4 | 把 `BUILD_SYSTEM_DLKM=0` / 删 `MODULES_ORDER`（`:607-608`）做成开关，按需产 `vendor_dlkm` | `scripts/kernel_builder.py:603-610` | 需要 GKI 模块的设备能拿到对应模块，缺模块不会导致驱动加载失败 |
| 2.5 | 修 AnyKernel3 后缀 bug：`:713-714` 用 `["", "-lz4", "-gz"]` 拼出 `Image-lz4`/`Image-gz`，实际文件是 `Image.lz4`/`Image.gz` → lz4/gz 两个包永远是死分支 | `scripts/kernel_builder.py:707-725` | 确实产出 3 个 AK3 包，或明确只产 1 个并删掉死分支 |
| 2.6 | **给 `kernel-build.yml` 补 `Setup AVB signing key` 步骤**（复制 `build-kernels.yml:121-127`） | `workflows/kernel-build.yml` | 单版本 workflow 产出的 boot.img 带 AVB footer |
| 2.7 | 明确 AVB 策略并写进产物说明：默认自签（`testkey.pem`）→ 必须先 `fastboot flash vbmeta --disable-verity --disable-verification`；同时提供「不签 AVB footer」的裸镜像变体供已禁用校验的设备 | `scripts/kernel_builder.py`、`README.md` | 每个 Release 附刷入前置条件与救援步骤（备份原 boot / A/B 双槽） |
| 2.8 | 放宽 `is_release_file()` 过滤器（目前只放行「以 `android` 开头 + 含 `boot` 的 .img」），让新增的 `init_boot` / `vendor_boot` / `vendor_dlkm` / `dtbo` 能进 Release | `scripts/extract_artifacts.py:9-18` | 新增产物出现在 `release-files/` |
| 2.9 | 给产物加静态自检：`unpack_bootimg` 回读、AVB footer 校验（`avbtool info_image`）、镜像大小上限、kernel 压缩格式识别（lz4 vs lz4_legacy vs gz）、header version / `os_version`、page size、KMI 字符串与 `modules.load` 存在性、KPM 补丁痕迹检查 | 新增 `scripts/verify_artifacts.py` + workflow step | 自检失败 → **构建失败**，不产出残缺镜像 |
| 2.10 | 修 `find ... -exec cp {} artifacts/ \;` 扁平化覆盖同名文件的问题，产物加唯一前缀 | `workflows/build-kernels.yml:179-180`、`workflows/kernel-build.yml:179-180` | 多组合构建不会互相覆盖 |
| 2.11 | 产出「KMI 匹配卡」：从 `Image` 提取完整 `uname -r`（`w.x-androidNN-k-sub`），与目标机 `uname -r` 比对；不匹配则在 Release 里红色告警而非静默给包 | `scripts/verify_artifacts.py`、`scripts/release_generator.py` | 用户能一眼看出选错包；避免 KMI/压缩格式/anti-rollback 导致的 bootloop |
| 2.12 | Release 附刷入手册：备份 stock boot/init_boot/vendor_boot、`fastboot boot <img>` 先临时试启、确认 A/B 当前槽位、`fastboot flash vbmeta --disable-verity --disable-verification` 前置条件、`fastboot -w` 的已知风险（#683）、回滚步骤 | `README.md`、`scripts/release_generator.py` | 文档与社区标准动作一致；「救援」章节不再只写 `fastboot flash boot` |

### 阶段 3 · KPM 全功能（直接决定需求 3）

| # | 任务 | 改动文件 | 验收标准 |
|---|---|---|---|
| 3.1 | **删掉 `:630` 的 `or kernel_version == "6.6"` 硬跳过**，让 6.6 也走 KPM 补丁（前提是任务 0.4 确认补丁可用） | `scripts/kernel_builder.py:629-645` | android15-6.6 产物确实带 KernelPatch 补丁 |
| 3.2 | 让 `CONFIG_KPM=y`（`:64`）受 `config.use_kpm` 控制，`--no-kpm` 时不再写入 | `scripts/kernel_builder.py:61-98,404-418`、`scripts/config.py` | `--no-kpm` 产出的内核编译期就不带 KPM 桥 |
| 3.3 | `KPM_PATCH_URL` 换成与 SukiSU-Ultra `kernel/extension/kpm/bridge.h` **ABI v2 同源**且带 commit pin 的地址，下载的 `patch` 做 sha256 校验后再 `chmod` 执行 | `scripts/config.py:96`、`scripts/kernel_builder.py:643` | 补丁二进制可追溯、可校验，不再 `chmod 777` 盲跑 |
| 3.4 | `patch_kpm_image()` 的 `image_dir` 由「构建产物清单」驱动，不再靠 android 版本猜 `out/.../dist` vs `bazel-bin/...`（`:635-638`） | `scripts/kernel_builder.py:635-641` | 构建方式变化时能定位到真实 Image，路径失效不再静默 `return` |
| 3.5 | 校验补丁结果：`oImage` 存在、大小合理、能被 `kptools` 识别、再 `mv` 覆盖 `Image`；失败即中止 | `scripts/kernel_builder.py:644-645` | 不会出现「补丁没打上但打包照样成功」 |
| 3.6 | 保证**每个产物变体**（`Image` / `Image.gz` / `Image.lz4` → 3 个 boot.img + 3 个 AK3）都基于打过 KPM 补丁的内核；必要时重排 `build()` 中 `patch_kpm_image()` 与 `prepare_boot_images()` / `create_anykernel_zips()` 的顺序 | `scripts/kernel_builder.py:727-763` | 任一产物都能加载 KPM |
| 3.7 | Release 附 KPM 使用说明：superkey `123`（`KpmPatchCommand.kt:21-30` 硬编码）、`/data/adb/kpm/*.kpm` 加载路径、`ksud kpm version` 自检命令、KPM 模块需与 ABI v2 匹配 | `README.md`、`scripts/release_generator.py` | 用户能按文档自检 KPM 是否真的 working（`CONFIG_KPM` 只代表编译开关，`userspace/ksud/src/extension.rs:193-215` 明确要 live `kpm version` 成功才算数） |

### 阶段 4 · SUSFS 与常规隐藏

| # | 任务 | 改动文件 | 验收标准 |
|---|---|---|---|
| 4.1 | 核对 `gki-{android}-{kernel}` 分支的 susfs4ksu 补丁与 13 条 `CONFIG_KSU_SUSFS_*` 一一对应，patch 冲突不再被 `check=False` 吞掉（`--fuzz=3` 风险） | `scripts/kernel_builder.py:284-302,61-98` | patch 失败 → 构建失败；`ksud susfs status` 返回以 `v` 开头的版本 |
| 4.2 | 核对 manager 侧 `MAX_SUSFS_VERSION`（当前 `v2.1.0` 系列）与内核补丁版本、`reboot(2)` 命令号 `0x555xx` 对齐 | `scripts/config.py:9-37`、Release body | manager SuSFS 页面显示全部特性开关 |
| 4.3 | 确认 `CONFIG_KSU_SUSFS_SUS_PATH=n`（6.6，`:414-417`）是内核限制还是可放开，并记录原因 | `scripts/kernel_builder.py:414-417` | 有明确结论，不留「静默关掉一个功能」 |
| 4.4 | 核对 `69_hide_stuff.patch` 与 SukiSU `builtin` 分支当前目录结构是否仍匹配 | `scripts/kernel_builder.py:304-309` | patch 无 rejected hunk |
| 4.5 | 给 `apply_susfs_patches` / `apply_sukisu_patches` / `apply_zram_patches` / `apply_task_mmu_fixes` 的关键步骤加 `check=True`，失败显性化 | `scripts/kernel_builder.py` 全文 | 任一补丁失败 → 构建失败并打印原因 |

### 阶段 5 · 通用健壮性（防止「奇怪的异常和 bug」）

| # | 任务 | 改动文件 | 验收标准 |
|---|---|---|---|
| 5.1 | **系统性清理 `check=False`**：clone / curl / patch / mkbootimg / avbtool / zip / KPM patch 全部改为 `check=True`（或显式判断返回值 + 抛错），保留少数确实允许失败的降级路径并写注释 | `scripts/kernel_builder.py` 全文 | 失败不会再产出「看起来成功」的残缺镜像 |
| 5.2 | 清理死代码：`kernel_patches` 克隆（`:81` 配了但全程未用）、本地 `hmbird_patch.c`（实际 `curl` 远端 `:243`）、`--ksu-version` 旧逻辑、`origin/dev` 陈旧分支警告 | `scripts/config.py`、`scripts/kernel_builder.py`、`README.md` | 无死配置、无过时文档 |
| 5.3 | 修 README 的谎言：`cache_manager.py` / `CacheManager` 不存在、「生成最新管理器 APK」不存在（需改为「请安装与内核同源的官方 manager APK」） | `README.md:135-139,190-215` | 文档与实际一致 |
| 5.4 | 给 `sha256sum *` / `Prepare artifacts` 加判空（单版本 workflow `:191` 目前无判空） | `workflows/kernel-build.yml:176-192` | 空产物时明确失败 |
| 5.5 | 统一两个 workflow 的签名 key / 缓存 / 产物收集逻辑，避免行为分叉 | `workflows/kernel-build.yml`、`workflows/build-kernels.yml` | 单版本与全量构建产物格式一致 |

### 阶段 6 · 验证与灰度（**必须最后做，且先在非主力机验证**）

| # | 任务 | 验收标准 |
|---|---|---|
| 6.1 | `python build.py --android [ANDROID] --kernel [KERNEL] --sub-level [SUB_LEVEL] --os-patch [OS_PATCH] --dry-run` 验配置（`:131-134` 不联网构建） | 配置校验通过，无异常 |
| 6.2 | 跑一次**单版本** workflow，检查 `artifacts/` 同时出现：AK3 zip、3 个带 AVB footer 的 boot.img、必要的 init_boot/vendor_boot、BUILD_MANIFEST.txt、SHA256SUMS | 产物齐全且自检脚本全绿 |
| 6.3 | 静态核对：内核 `KSU_VERSION_FULL` / UAPI 与所用 manager APK 的版本指纹一致 | 不出现「版本不匹配」提示 |
| 6.4 | 真机灰度（先备份原 boot / init_boot / vendor_boot，确认 A/B 双槽可回退，确认 vbmeta 状态，**先 `fastboot boot` 临时试启再 flash**）：① 能进系统不无限重启 ② manager 识别内核 + root 可用 ③ 模块/超用户菜单可用 ④ WiFi/蓝牙/触控/相机等厂商 `.ko` 驱动正常（`lsmod` / `dmesg` 无 undefined symbol）⑤ `ksud susfs status` 正常 ⑥ `ksud kpm version` 成功且能加载一个测试 KPM ⑦ 常规 root 隐藏（Play 完整性 / 检测 App）通过 | 七条全过才算完成；区分「能开机」与「功能齐全」（社区 #683 / #1001 / #758 都是反例） |
| 6.5 | 全量矩阵构建（19 组合）并逐个跑静态自检；对 6.6 重点验证 KPM 补丁已生效且无随机重启/冻结（WildKernels #126、SukiSU #362 的已知风险） | 全绿后再推 GitHub Release |
| 6.6 | 稳定性抽测：KPM 补丁后的内核在常用 App（抖音/酷安/银行类）下连续运行无黑屏重启；开机后 `dmesg` 无 KernelPatch 相关 panic | 至少一轮真机长稳验证 |

---

## 6. 风险登记与待确认项

| 风险 | 影响 | 处理 |
|---|---|---|
| **AVB 自签 key 与设备 vbmeta 不匹配** | 无限重启（需求 1 头号风险） | 任务 2.7：文档前置条件 + 提供未签变体；目标 0.3 确认设备状态 |
| **6.6 是否有可用 KPM 补丁** | 需求 3 在 android15 上落空 | 任务 0.4 联网核对；若 `ShirkNeko/SukiSU_patch` 无 6.6 版，需找 `SukiSU_KernelPatch_patch` 或 `KPM-Build-Anywhere` 同源补丁 |
| **android13+ 是否有对应 GKI 认证 boot 可复用 ramdisk** | 任务 2.2 的降级路径可能无基底 | 已改为首选「用户提供本机 stock boot.img 由 CI 重打包」（任务 2.2），不再依赖 Google 认证 boot 覆盖范围；用户不提供时退路是「CI 产裸 Image + 一键重打包脚本」，即放弃纯 fastboot 直刷 |
| **KMI / 压缩格式 / page size / anti-rollback 不匹配** | bootloop（AOSP + KernelSU 官方明确） | 任务 2.11 KMI 匹配卡、2.9 静态自检、2.3 page size/格式参数化；Release 强制要求按完整 `uname -r` 选包 |
| **KPM 补丁自身稳定性（6.6 尤其）** | 随机重启/冻结/黑屏 | WildKernels [#126](https://github.com/WildKernels/GKI_KernelSU_SUSFS/issues/126)、SukiSU [#362](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/362) 已证实；任务 0.4 先核实补丁源，任务 6.6 做长稳抽测，不通过则不发 6.6 的 KPM 版本 |
| **`builtin` 分支与 `main` 分叉 855 提交** | 内核功能落后于 main 的新特性 | 任务 0.2 确认选择；**WildKernels 上游同样把 SukiSU-Ultra pin 在 `builtin@1cfa5734`**（[main.yml](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/.github/workflows/main.yml)），说明这是社区默认选择；若要跟进 main 需连带重写 SUSFS 补丁链（成本高） |
| **KSU_VERSION 依赖 GitHub API 数 commit** | 离线/限流时版本数字漂移 | 任务 1.6/1.8：改成基于 pinned commit 的确定性值，并随产物发布指纹 |
| **机型差异（一加 ColorOS 14/15 明确不支持）** | 刷入不起 | 任务 0.3 拿机型；README:5 已声明，需在 Release 里重申 |
| **未拿到你的具体机型 / 内核版本 / AVB 状态 / stock boot.img** | 阶段 2、6 的验收无法具体化 | 用占位符 `[DEVICE]` `[ANDROID]` `[KERNEL]` `[SUB_LEVEL]` `[OS_PATCH]` `[STOCK_BOOT]`，开工前补齐 |
| **BakaSU 与 SukiSU 的 uapi / manager 签名表已分叉** | 混用 ksud/manager 会「root 可用但模块菜单不可用」 | 已确认不通用；本项目只用 SukiSU-Ultra 一套，内核/ksud/manager 一一配对（任务 1.8 发布版本指纹供核对） |

---

## 7. 备选基线（仅登记，不纳入本轮）

- **A′：`WildKernels/GKI_KernelSU_SUSFS` 最新 main 重新 fork**（2026-11-06 仍活跃）。优势：verified commit pins（`PIN_SUKISU_ULTRA = builtin@1cfa5734` 等精确 SHA）、`root-setup` 拒绝混合多 root 实现、`trim_sukisu_managers.py` 裁证书表、16k/4k page size、Samsung 6.6 KDP 符号补丁、按 flavor 配 manager APK、`allow_version_mismatch` 开关。代价：**上游完全不设 `CONFIG_KPM`、不集成 KernelPatch、不产可直接 fastboot 刷的 img**（只给 AnyKernel3 `Image`，要求用户自己拿 stock boot.img 经 magiskboot 重打包）。选它等于在更干净也更复杂的流水线上重做「KPM 补丁链 + fastboot img 产出」两大块，工作量最大，但可复现性与机型适配最好。[repo](https://api.github.com/repos/WildKernels/GKI_KernelSU_SUSFS) · [main.yml](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/.github/workflows/main.yml) · [build-from-fork.md](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/docs/build-from-fork.md)
- `MiRinFork/GKI_SukiSU_SUSFS`：SukiSU-Ultra 官方 `installation.md:18` 称其「最接近 GKI 的内核，适用于大多数设备」，且标注带 susfs + KPM。若 ShirkNeko 这套的 `builtin` 分支 + 老 SUSFS 补丁链修不动，换它是更干净的方案 A 变体。
- 完全放弃纯 fastboot 直刷，改用 AK3（设备端就地保留原 boot）：最稳，但与你的偏好冲突，仅在任务 2.2 用户不愿提供 stock boot.img 时作为兜底。
- **BakaSU / WildKernels 的 BakaSU flavor**：适合「省心 + SUSFS + 轻维护」，但必须放弃 KPM（`faac3098` 已从源码层删除），且 uapi / manager 签名表与 SukiSU 分叉、ksud/manager 必须一一配对。

## 9. 调查来源（外部结论均附 URL）

联网核对由子代理完成，`web_search` 因宿主未配置 `DEEPSEEK_API_KEY` 不可用，改用 GitHub REST API（含 `search/issues`）、`raw.githubusercontent.com`、AOSP `source.android.com`、`kernelsu.org` 直取。主要来源：

- AOSP：[GKI](https://source.android.com/docs/core/architecture/kernel/generic-kernel-image) · [generic boot 分区](https://source.android.com/docs/core/architecture/partitions/generic-boot) · [vendor boot 分区](https://source.android.com/docs/core/architecture/partitions/vendor-boot-partitions) · [boot image header](https://source.android.com/docs/core/architecture/bootloader/boot-image-header) · [kernel module support](https://source.android.com/docs/core/architecture/kernel/kernel-module-support) · [modules](https://source.android.com/docs/core/architecture/kernel/modules) · [AVB](https://source.android.com/docs/security/features/verifiedboot/avb)
- KernelSU 官方：[installation](https://kernelsu.org/guide/installation.html)
- WildKernels：[main.yml](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/.github/workflows/main.yml) · [build.yml](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/.github/workflows/build.yml) · [root-setup action](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/.github/actions/root-setup/action.yml) · [build-from-fork.md](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/docs/build-from-fork.md) · [magiskboot.md](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/docs/magiskboot.md) · [installation.md](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/main/docs/installation.md) · [v1.5.9-r22 build.yml](https://raw.githubusercontent.com/WildKernels/GKI_KernelSU_SUSFS/v1.5.9-r22/.github/workflows/build.yml)
- SukiSU-Ultra：[README](https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/docs/README.md) · [how-to-integrate](https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/docs/guide/how-to-integrate.md) · [installation](https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/docs/guide/installation.md) · [SukiSU_KernelPatch_patch](https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU_KernelPatch_patch/main/README.md) · issue [#29](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/29) / [#37](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/37) / [#239](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/239) / [#362](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/362) / [#643](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/643) / [#683](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/683) / [#758](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/758) / [#1001](https://github.com/SukiSU-Ultra/SukiSU-Ultra/issues/1001)
- KPatch-Next：[module README](https://raw.githubusercontent.com/KernelSU-Next/KPatch-Next-Module/main/README.md) · [service.sh](https://raw.githubusercontent.com/KernelSU-Next/KPatch-Next-Module/main/module/service.sh) · [build.sh](https://raw.githubusercontent.com/KernelSU-Next/KPatch-Next-Module/main/build.sh) · [customize.sh](https://raw.githubusercontent.com/KernelSU-Next/KPatch-Next-Module/main/module/customize.sh) · [KPatch-Next README](https://raw.githubusercontent.com/KernelSU-Next/KPatch-Next/main/README.md)

> 限制：GitHub REST API 后段 403 限流，issue comments 未展开，但 issue body 已提供充分证据；所有外部内容按不可信数据处理，仅用于事实核对。

---

## 8. 本轮交付边界

- ✅ 已完成：三仓库只读调查、SukiSU-Ultra 内核侧机制分析、KPM/SUSFS/版本识别链路核实、`builtin` 分支真相、WildKernels r22 + KPatch-Next-Module 失败归因、AOSP GKI 分区/AVB 一手资料核对、路线决断（A 与备选 A′）、分阶段任务清单。
- ❌ 未执行：任何**仓库文件**修改、任何 git 提交/推送、任何构建、任何真机刷入。
- 三个仓库 `git status --short` 均为空；本评估文档写在 `D:\LocalProject\` 根目录，不在任何仓库内。
