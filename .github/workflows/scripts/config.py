from dataclasses import dataclass
from typing import Optional
from enum import Enum
import re


# ---------------------------------------------------------------------------
# SUSFS 版本标签：仅用于 Release 显示，不再在 import 时联网获取。
#
# 原实现 get_susfs_version() 在 config.py 被 import 时就发 HTTPS 请求
# (raw.githubusercontent.com)，且关闭了 TLS 校验 (CERT_NONE)；一旦被墙、
# 限流或上游改文件名，就 fallback 成固定的 "v2.1.0"。而该值又被
# matrix_generator.py 当作 GitHub Release 的 tag_name，导致多次构建互相
# 覆盖同一个 tag。矩阵/构建脚本 import config 时都会无辜触发这个请求。
#
# 真正记录了依赖版本的地方是 BUILD_MANIFEST.txt（由 kernel_builder 写出），
# 它包含每个依赖的实际 git commit，比一个联网抓来的字符串可靠得多。
# ---------------------------------------------------------------------------
# 与 PIN_SUSFS_android12_5_10 (b0e4746) 的 susfs.h 实测值一致。
# 2026-08-27 仓库停更时是 v2.1.0；上游已推进到 v2.3.0，补丁链随之变化。
SUSFS_VERSION_LABEL = "v2.3.0"
KERNEL_VERSION = SUSFS_VERSION_LABEL

# 内核侧必须达到的 UAPI 版本。低于此值说明检出的是旧内核
# (例如 2026-08-27 的镜像是 UAPI 2，且没有 KSU_IOCTL_EXTENSION)，
# 新版 manager 会显示「未安装」，模块/KPM/授权全部不可用。
KSU_MIN_UAPI_VERSION = 6


class AndroidVersion(Enum):
    ANDROID12 = "android12"
    ANDROID13 = "android13"
    ANDROID14 = "android14"
    ANDROID15 = "android15"


class KernelVersion(Enum):
    KERNEL_5_10 = "5.10"
    KERNEL_5_15 = "5.15"
    KERNEL_6_1 = "6.1"
    KERNEL_6_6 = "6.6"


class KSUVersion(Enum):
    STABLE = "Stable(标准)"
    DEV = "Dev(开发)"


ANDROID_KERNEL_MAP = {
    AndroidVersion.ANDROID12: [KernelVersion.KERNEL_5_10],
    AndroidVersion.ANDROID13: [KernelVersion.KERNEL_5_10, KernelVersion.KERNEL_5_15],
    AndroidVersion.ANDROID14: [KernelVersion.KERNEL_5_15, KernelVersion.KERNEL_6_1],
    AndroidVersion.ANDROID15: [KernelVersion.KERNEL_6_6],
}

# ---------------------------------------------------------------------------
# 仓库配置
#
# 内核侧真源是 SukiSU-Ultra 的 **builtin** 分支（不是 main）：
#   - main 分支已移除 SUSFS 内核实现，只剩 extension/susfs_extension.c 占位 handler；
#   - builtin 分支带完整 13 条 CONFIG_KSU_SUSFS_*，正好匹配
#     kernel_builder.py 的 KERNEL_CONFIG_TEMPLATE；
#   - builtin 同时含 KERNEL_SU_UAPI_VERSION=6、KSU_IOCTL_EXTENSION (extension hub)、
#     KSU_EXTENSION_API_VERSION=2、config KPM —— 这些是新版 manager 能识别、
#     模块/KPM/App Profile 授权能工作的前提。
#
# KSU_PIN 是精确的 40 位 SHA（2026-11-01 的 builtin HEAD），保证每次构建
# 的内核源码完全一致。留空字符串则退回「分支 HEAD」（不推荐，会漂移）。
# 可通过 workflow 输入 kernelsu_commit 覆盖；但默认值必须是确定值。
# ---------------------------------------------------------------------------
KSU_PIN = "1cfa57340590971cba7521ed536da8ebbedc5f0a"

# ---------------------------------------------------------------------------
# 互锁依赖 pins（取自 WildKernels/GKI_KernelSU_SUSFS 的 audited verified pins,
# "Refreshed 2026-11-02"，该上游每天全矩阵构建，是这组组合能编译的最强证据）：
#   PIN_SUKISU_ULTRA = 1cfa5734... (builtin)
#   PIN_SUSFS[android12-5.10] = b0e4746... (v2.3.0)
# 其注释明确写 SukiSU-Ultra builtin "defines fake_state itself and calls the
# susfs_def.h no_su API -> OK on tips"，即这组内核+susfs 是配套验证过的。
#
# SukiSU_patch 无官方 pin，取其当前 main HEAD。留空表示跟随分支 HEAD。
# ---------------------------------------------------------------------------
SUSFS_PIN = "b0e474642cf77ee797004a454fa441362723d0ba"
SUKISU_PATCH_PIN = "eec45788c9a115e5498040298133336d6b6071dd"

KSU_REPO_CONFIG = {
    "repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
    "branch": "builtin",
    "pin": KSU_PIN,
    "repo_dir": "KernelSU",
    # setup.sh 在仓库内执行，不再从 main 分支 curl（避免脚本/源码跨分支混用）
    "setup_script_rel": "kernel/setup.sh",
}

# SUSFS 仓库配置（分支由 kernel_branch = gki-{android}-{kernel} 决定）
SUSFS_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/susfs4ksu.git",
                     "pin": ""}

# SukiSU Patch 仓库配置（69_hide_stuff.patch / zram / KPM patch）
SUKISU_PATCH_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/SukiSU_patch.git",
                            "pin": ""}

# AnyKernel3 仓库配置
ANYKERNEL_CONFIG = {"repo_url": "https://github.com/WildPlusKernel/AnyKernel3.git", "branch": "gki-2.0"}

# Kernel Patches 仓库配置
KERNEL_PATCHES_CONFIG = {"repo_url": "https://github.com/Tools-cx-app/kernel_patches.git"}

# Baseband-guard 配置
BBG_CONFIG = {"repo_url": "https://github.com/vc-teahouse/Baseband-guard.git",
              "setup_script": "https://github.com/vc-teahouse/Baseband-guard/raw/main/setup.sh"}

# 工具链配置
TOOLCHAIN_CONFIG = {"aosp_mirror": "https://android.googlesource.com",
                    "build_tools_branch": "main-kernel-build-2024",
                    "mkbootimg_branch": "main-kernel-build-2024"}
LEGACY_FIXES = {
    "android13-5.15-below-123": {"url": "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/legacy/fix_5.15.legacy", "min_sub_level": 123},
    "android12-5.10-below-136": {"url": "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/legacy/fdinfo.c.patch", "min_sub_level": 136},
}
OP8E_PATCH_URL = "https://github.com/zzh20188/GKI_KernelSU_SUSFS/raw/refs/heads/dev/hmbird_patch.c"

# ---------------------------------------------------------------------------
# KPM 补丁：给编译出的 kernel Image 注入 KernelPatch (kpimg)，使得
# SukiSU-Ultra 的 kernel/extension/kpm/bridge.h (Kernel-only ABI v2) 能工作。
#
# 注意三件事：
#   1) CONFIG_KPM=y 只是编译进「桥接层」，运行时还需要内核 Image 被打过补丁，
#      否则 sukisu_kpm_bridge() 的 PROBE 会 -EOPNOTSUPP，manager 显示 KPM 未修补。
#   2) 补丁二进制必须与内核侧 bridge.h 的 ABI v2 同源。8 月之前的旧补丁是旧 ABI，
#      与新版内核不兼容。KPM_PATCH_SHA256 留空表示跳过校验（不推荐），
#      填入后可防止上游被替换/下载到损坏文件。
#   3) 该二进制是预编译 ELF，下载后直接执行；务必保证来源可信。
# ---------------------------------------------------------------------------
KPM_PATCH_URL = "https://raw.githubusercontent.com/ShirkNeko/SukiSU_patch/refs/heads/main/kpm/patch_linux"
# 官方同源补丁仓库（剪裁自 bmax121/KernelPatch，专供 SukiSU-Ultra KPM）：
KPM_PATCH_UPSTREAM = "https://github.com/SukiSU-Ultra/SukiSU_KernelPatch_patch"
KPM_PATCH_SHA256 = ""   # 可选：填 64 位 hex 后构建会强制校验下载文件

# 推荐产物：本机 (android12-5.10 / MTK Dimensity) 实测只有 gzip 内核能启动，
# 裸 Image 与 lz4 都会无限重启。该值仅用于 README / Release 说明与产物标注。
RECOMMENDED_BOOT_VARIANT = "boot-gz.img"
DISABLED_BOOT_VARIANTS = ("boot.img", "boot-lz4.img")


@dataclass
class BuildConfig:
    android_version: str
    kernel_version: str
    sub_level: str
    os_patch_level: str
    kernelsu_version: str = "Stable(标准)"
    # 默认绑定到 KSU_PIN，保证不指定时仍是可复现的确定 commit；
    # workflow 的 kernelsu_commit 输入可覆盖。
    kernelsu_commit: Optional[str] = KSU_PIN
    susfs_commit: Optional[str] = None
    use_zram: bool = False
    use_kpm: bool = True
    use_bbg: bool = False
    support_op8e: bool = False
    set_default_bbr: bool = False
    make_release: bool = True
    custom_version: Optional[str] = None
    revision: Optional[str] = None
    build_id: Optional[str] = None

    def __post_init__(self):
        self._validate_android_version()
        self._validate_kernel_version()
        self._validate_kernel_android_compat()
        self._validate_sub_level()
        self._set_build_id()

    def _validate_android_version(self):
        valid = [v.value for v in AndroidVersion]
        if self.android_version not in valid:
            raise ValueError(f"无效的 Android 版本: {self.android_version}. 支持: {', '.join(valid)}")

    def _validate_kernel_version(self):
        valid = [v.value for v in KernelVersion]
        if self.kernel_version not in valid:
            raise ValueError(f"无效的 Kernel 版本: {self.kernel_version}. 支持: {', '.join(valid)}")

    def _validate_kernel_android_compat(self):
        av = AndroidVersion(self.android_version)
        kv = KernelVersion(self.kernel_version)
        if kv not in ANDROID_KERNEL_MAP.get(av, []):
            raise ValueError(f"Android {self.android_version} 不支持 Kernel {self.kernel_version}")

    def _validate_sub_level(self):
        if self.sub_level != "X" and not self.sub_level.isdigit():
            raise ValueError(f"无效的 sub_level: {self.sub_level}")

    def _set_build_id(self):
        if self.build_id is None:
            self.build_id = f"{self.android_version}-{self.kernel_version}-{self.sub_level}-{self.os_patch_level}"

    @property
    def config_name(self) -> str:
        return f"{self.android_version}-{self.kernel_version}-{self.sub_level}"

    @property
    def formatted_branch(self) -> str:
        return f"{self.android_version}-{self.kernel_version}-{self.os_patch_level}"

    @property
    def kernel_branch(self) -> str:
        return f"gki-{self.android_version}-{self.kernel_version}"

    def get_susfs_patch_filename(self) -> str:
        return f"50_add_susfs_in_gki-{self.android_version}-{self.kernel_version}.patch"

    def is_lts(self) -> bool:
        return self.sub_level == "X"

    def get_sub_level_int(self) -> Optional[int]:
        return None if self.sub_level == "X" else int(self.sub_level)

    def to_dict(self) -> dict:
        return {
            "android_version": self.android_version,
            "kernel_version": self.kernel_version,
            "sub_level": self.sub_level,
            "os_patch_level": self.os_patch_level,
            "kernelsu_version": self.kernelsu_version,
            "kernelsu_commit": self.kernelsu_commit,
            "use_zram": self.use_zram,
            "use_kpm": self.use_kpm,
            "use_bbg": self.use_bbg,
            "support_op8e": self.support_op8e,
            "set_default_bbr": self.set_default_bbr,
            "make_release": self.make_release,
            "custom_version": self.custom_version,
            "revision": self.revision,
            "build_id": self.build_id,
        }


def validate_commit_hash(commit_hash: str) -> bool:
    return bool(re.match(r'^[0-9a-f]{7,40}$', commit_hash, re.IGNORECASE))
