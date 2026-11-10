# GKI_KernelSU_SUSFS 多家族适配报告（2026-11-10 第二/三轮）

## 目标与结论

第一轮已实机验证 `android12-5.10-236`：fastboot 刷 `boot-gz.img` 正常开机，
sukisu 识别到内核、KPM 可用。第二轮按使用者要求把「各家族编译流程」修通，
并按参考 release `ShirkNeko/GKI_KernelSU_SUSFS v2.1.0` 的产物形态定规则。
第三轮专门核查「推送 GitHub Actions 能否编译成功」的环境差异并修复。

**两轮真实构建验证（0 error）**：

## 0. 第三轮：Actions 环境差异修复（推送前必看）

本地是 WSL，Actions 是 `ubuntu-latest`，两者环境不一致。核查后发现并修复：

| 风险 | 后果 | 修复 |
|---|---|---|
| `ubuntu-latest` 只有 `python3`，没有 `python` 命令 | `repo` 工具 shebang 是 `#!/usr/bin/env python` → `env: 'python': No such file or directory`，repo init 直接挂 | 两个 workflow 的 `Install dependencies` 都加 `python-is-python3` |
| 缺 `bc` / `bison` / `flex` / `libssl-dev` / `libelf-dev` | `build/build.sh` 编译内核时缺工具失败 | 两个 workflow 都补齐并加前置 `command -v` 校验 |
| `setup_repo_tool()` 用 `check=False` + `curl > file`，下载失败留 0 字节文件 | 后续报迷惑性错误，浪费半小时 | 改 `check=True` + 大小校验 + 显式探测 `python` 命令 |
| `build-kernels.yml`（全矩阵入口）的 `Install dependencies` 同样缺依赖 | 19 个组合全挂 | 与 `kernel-build.yml` 同步补齐 |

**验证**：
- 两个 workflow YAML `safe_load` 通过，`Install dependencies` 均含 `python-is-python3` / `bc` / `bison`
- `matrix_generator.py` 实际生成 19 条矩阵（5.10×5 + 5.15×6 + 6.1×5 + 6.6×3）
- 19 个 `BuildConfig` 全部构造成功
- workflow 的 `ARGS` 字符串（单版本 + 6.6 `--no-kpm`）能被 `build.py` 正确解析
- 7 个 Python 模块 import 语法全过
- `extract_artifacts.py` / `release_generator.py` 无「必须有 img」的假设，"只 zip" 家族在 release job 里不会被卡

**仍未在真 Actions 环境验证**：以上均为静态/本地等价验证，`ubuntu-latest` 的
实际预装包、`repo init` 网络、Google certified boot 下载、Bazel 构建路径
（android14/15 若走 `tools/bazel`）没有跑过真实 Actions run。推送后建议先用
**Kernel Build 单版本**跑一个 `android12-5.10-236` 验证流水线，再跑
**Build Kernels** 全矩阵。

| 组合 | 结果 | 耗时 |
|---|---|---|
| android12-5.10-236 | ✅ 第一轮实机开机 + 第二轮回归全绿 | 282s |
| android13-5.15-189 | ✅ 完整构建通过（补丁 + KPM + 打包） | 638s |

---

## 一、第二轮修掉的问题

### 1. 5.15 susfs 补丁失败（真实构建暴露）

`android13-5.15-189` 首轮真实构建失败：
```
patch -p1 --fuzz=3 < 50_add_susfs_in_gki-android13-5.15.patch
Hunk #1 FAILED at 32 (fs/namespace.c)
```

**根因**（子代理只读调研 + 本地二进制验证双重确认）：
Google 在较新 GKI sublevel 里删掉了 `#include <trace/hooks/blk.h>` 等 include 行，
而 susfs4ksu 补丁仍把它们当首个 hunk 的上下文。WildKernels 的 pin `d5df1d93`
和 GitHub/GitLab 分支 tip 的同一 patch **上下文逐字相同**，所以：
**换仓库、换分家族 pin、换 commit 全部无效**。

**解法**：移植 WildKernels 的 `susfs_restore_context.py`（通用、幂等、无版本门槛），
在 `patch` 前把缺失的 `<trace/hooks/*.h>` 按补丁期望顺序插回，头文件不存在时
建空 stub（SUSFS 不使用任何 trace hook 符号，纯上下文用）。实测 5.15-189：
```
context restored  fs/namespace.c: #include <trace/hooks/blk.h>
context restored  fs/proc/task_mmu.c: #include <trace/hooks/mm.h>
stub header made  include/trace/hooks/blk.h
patch rc=0, 0 个 .rej
```
新脚本：`.github/workflows/scripts/susfs_restore_context.py`，由
`apply_susfs_patches()` 无条件调用；`patch` 也对齐 WildKernels 改成裸 `patch -p1`
（去掉 `--fuzz=3` / `--forward`，让失配显性暴露）。

### 2. 产物目录硬编码（会导致 android14/15 必挂）

`patch_kpm_image()` / `prepare_boot_images()` 原按 `android_version` 把
android14/15 指向 `bazel-bin/common/kernel_aarch64`，而 `build_kernel()`
实际用 `build/build.sh` 产出在 `out/<av>-<kv>/dist`。新增
`_kernel_dist_dir()` 按实际存在的 Image 探测，兼容两种布局 + 兜底宽搜。

### 3. certified boot 模板 URL 硬编码 android12-5.10

`_prepare_android12_boot_images()` 泛化成 `_prepare_boot_images_with_template()`，
URL 按 `gki-certified-boot-<av>-<kv>-<pat>_<rev>.zip` 拼，revision 缺省回退 `r1`；
模板文件按 `boot-<kv>*.img` 探测并优先精确名（避免 `-lts` 变体误选）。
实测 5.10 / 5.15 各 sub_level 的模板 URL 全部 200。

### 4. 产物规则（按使用者拍板）

| 家族 | 产物 | 说明 |
|---|---|---|
| android12-5.10 | 3 个 boot*.img + AnyKernel3.zip | certified boot 模板 + magiskboot，保留 VBMETA |
| android13-5.15 | 3 个 boot*.img + AnyKernel3.zip | 同上（模板 `boot-5.15.img` 实测可用）|
| android14-6.1 | **只 AnyKernel3.zip** | 通用 mkbootimg 打包器无 ramdisk / 硬编码 os_version 12 / 无 VBMETA，产出残缺镜像，交给 AK3 在设备上重打包 |
| android15-6.6 | **只 AnyKernel3.zip**（KPM 跳过）| 同上 |

判定集中在 `BuildConfig.produces_boot_img()`；`_verify_artifacts()` 按家族条件化
（"只 zip" 家族不再被 `缺少 boot-gz.img` 误杀，且反过来禁止混入 img）；
`_write_manifest()` 的 `recommended_variant` 同步条件化；
workflow 的 AVB 签名步骤限定到产 img 的两个家族。
android16-6.12 按使用者决定**本轮暂缓**（susfs 补丁只在 GitLab 上游有）。

### 5. 模板选择边界 bug

单元测试发现 `boot-5.10.img` 与 `boot-5.10-lts.img` 同时存在时字典序会误选
`-lts`，改成精确名优先。单元验证 5.10 / 5.15 均 PASS。

### 6. 6.6 KPM README 显眼说明

README 顶部加红字级告警块：**android15-6.6 不支持 KPM**（跳过 kpimg 注入、
`CONFIG_KPM=y` 桥接层仍编译、PROBE 返回 `-EOPNOTSUPP`、强行注入会 logo 无限重启），
并说明 6.1 / 6.6 只给 zip。

---

## 二、上游调研结论

- **WildKernels 5.15 主产物其实只有 AnyKernel3.zip**（`build-kernel/action.yml`
  只把 Image 拷进 AK3 目录），但本仓库按使用者规则仍给 5.15 产 img（模板可用）。
- WildKernels 的 `PIN_SUSFS` 是 7 个家族 7 个不同 SHA，用 GitLab 上游；
  但 GitHub/ShirkNeko 镜像与其同步（同分支 tip `fe943d15`），本仓库保持
  **跟随分支 HEAD**（`SUSFS_REPO_CONFIG["pin"] = ""`），不引入分家族 pin 复杂度。
- 5.15 的 `close_fd_compat.sh` 是遗留孤儿脚本（全仓无引用），5.15 自带
  `close_fd`，无需处理。
- 其余无版本门槛脚本中，本仓库已有的 `_fix_assoc_array_header` /
  `_fix_vdso_gettimeofday_include` / VMA_PAD_START / priority_uid_filter 门控
  覆盖了 5.10 的编译问题；5.15 实测完整编译通过，说明现有修复足够。

---

## 三、验证清单

- ✅ 5.10 全量回归：282s，0 error，KPM 自检 / VBMETA / 产物自检全过
  （`context already complete`，restore 脚本幂等，未改动 5.10 源码）
- ✅ 5.15-189 全量：638s，0 error，补丁全过，KPM 自检过，
  `boot-5.15.img` 模板 + VBMETA，2 boot img / 3 zip
- ✅ "只 zip" 路径单元验证：android14-6.1 / android15-6.6 PASS，5.10 PASS
- ✅ 模板探测单元验证：5.10 / 5.15 PASS
- ✅ 全矩阵 manifest 分支 `common-*` 与 `deprecated/*` 存在性（11 个全 OK）
- ✅ 5.10 / 5.15 certified boot URL 全部 200
- ✅ 四个 Python 模块 import 语法检查

## 四、代码状态

改动文件：
- `.github/workflows/scripts/config.py`（KPM 官方流程配置 + `produces_boot_img()`）
- `.github/workflows/scripts/kernel_builder.py`（KPM 校验、dist 探测、打包泛化、
  susfs 上下文恢复、产物规则、自检条件化）
- `.github/workflows/scripts/susfs_restore_context.py`（**新增**）
- `.github/workflows/kernel-build.yml`（AVB 步骤条件化）
- `README.md`（6.6 KPM 显眼告警 + 产物规则）

**未 commit、未 push**（使用者自行提交）。`SukiSU-Ultra` / `BakaSU` 保持 clean。
完整 diff 备份：`D:\LocalProject\GKI_多家族适配_final.patch`（含新脚本本体）。
