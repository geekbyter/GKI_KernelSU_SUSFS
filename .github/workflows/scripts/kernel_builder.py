import os
import subprocess
import logging
import re
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field

from config import (BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG, SUKISU_PATCH_REPO_CONFIG,
                   ANYKERNEL_CONFIG, KERNEL_PATCHES_CONFIG, BBG_CONFIG, TOOLCHAIN_CONFIG,
                   LEGACY_FIXES, OP8E_PATCH_URL, KPM_PATCH_URL, KPM_PATCH_SHA256,
                   KPM_PATCH_UPSTREAM, KPM_TOOLS_VERSION, KPM_KPTOOLS_URL,
                   KPM_KPIMG_URL, KPM_KPTOOLS_SHA256, KPM_KPIMG_SHA256, KPM_SUPERKEY,
                   KSU_MIN_UAPI_VERSION, RECOMMENDED_BOOT_VARIANT,
                   DISABLED_BOOT_VARIANTS, SUSFS_VERSION_LABEL, KSU_PIN,
                   SUSFS_PIN, SUKISU_PATCH_PIN, KSU_VERSION_PIN,
                   KSU_MANAGER_TAG, KSU_MANAGER_BASE_COMMIT,
                   MAGISKBOOT_URL, MAGISKBOOT_LIB, MAGISKBOOT_APK_SHA256)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _read_git_head(repo_dir: Path) -> str:
    """返回仓库当前 HEAD 的 40 位 SHA（失败返回空串）。"""
    try:
        r = subprocess.run("git rev-parse HEAD", shell=True, cwd=str(repo_dir),
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _uapi_version(ksu_tree: Path) -> Optional[int]:
    """读取 KERNEL_SU_UAPI_VERSION，兼容 builtin (DECLARE) / main (static const)。"""
    pats = [re.compile(r'DECLARE\s*\(\s*__u32\s*,\s*KERNEL_SU_UAPI_VERSION\s*,\s*(\d+)'),
            re.compile(r'KERNEL_SU_UAPI_VERSION\s*=\s*(\d+)')]
    for c in [ksu_tree / "kernel/include/uapi/supercall.h", ksu_tree / "uapi/supercall.h"]:
        try:
            if c.exists():
                txt = c.read_text(errors="ignore")
                for p in pats:
                    m = p.search(txt)
                    if m:
                        return int(m.group(1))
        except Exception:
            pass
    return None


def _has_extension_hub(ksu_tree: Path) -> bool:
    """新版内核必须有 KSU_IOCTL_EXTENSION ('K',300) 扩展 hub。"""
    for c in [ksu_tree / "kernel/include/uapi/extension.h", ksu_tree / "uapi/extension.h",
              ksu_tree / "kernel/include/uapi/supercall.h", ksu_tree / "uapi/supercall.h"]:
        try:
            if c.exists() and "KSU_IOCTL_EXTENSION" in c.read_text(errors="ignore"):
                return True
        except Exception:
            pass
    return False


@dataclass
class BuildResult:
    success: bool
    config: BuildConfig
    message: str = ""
    artifacts: list = field(default_factory=list)
    build_time: Optional[float] = None


class ShellCommand:
    def __init__(self, cwd: Optional[str] = None, env: Optional[dict] = None):
        self.cwd = cwd
        self.env = env or os.environ.copy()

    def run(self, cmd: str, check: bool = True, capture_output: bool = False,
            shell: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        logger.info(f"执行命令: {cmd}")
        try:
            return subprocess.run(cmd, shell=shell, cwd=self.cwd, env=self.env,
                                capture_output=capture_output, text=True, timeout=timeout, check=check)
        except subprocess.CalledProcessError as e:
            logger.error(f"命令执行失败: {e.stderr or str(e)}")
            raise
        except subprocess.TimeoutExpired:
            logger.error(f"命令执行超时: {cmd}")
            raise

    def run_with_callback(self, cmd: str, callback: Optional[Callable] = None) -> str:
        logger.info(f"执行命令: {cmd}")
        process = subprocess.Popen(cmd, shell=True, cwd=self.cwd, env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        output_lines = []
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            if callback:
                callback(line)
        process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"命令执行失败")
        return "\n".join(output_lines)


class KernelBuilder:
    KERNEL_CONFIG_TEMPLATE = """
# === KernelSU Config ===
CONFIG_KSU=y
CONFIG_KPM=y
CONFIG_KSU_SUSFS_SUS_SU=n

# === 基础依赖（必须显式开启） ===
# deprecated/android12-5.10-2025-05 的 gki_defconfig 未显式设置
# CONFIG_KEYS；而 include/linux/key.h 里的 struct key 含成员
# `struct assoc_array keys;`，assoc_array.h 又用
# #ifdef CONFIG_ASSOCIATIVE_ARRAY 保护该结构体定义。
# 一旦 CONFIG_KEYS 打开但 CONFIG_ASSOCIATIVE_ARRAY 没被 select，
# arch/arm64/kernel/asm-offsets.s（prepare0 阶段）就会报
# "field has incomplete type 'struct assoc_array'"。
# 显式写 =y 与 CONFIG_KEYS 的 select 等价，无副作用。
CONFIG_KEYS=y
CONFIG_ASSOCIATIVE_ARRAY=y

# === TMPFS Config ===
CONFIG_TMPFS_XATTR=y
CONFIG_TMPFS_POSIX_ACL=y

# === Network Config ===
CONFIG_IP_NF_TARGET_TTL=y
CONFIG_IP6_NF_TARGET_HL=y
CONFIG_IP6_NF_MATCH_HL=y

# === BBR Config ===
CONFIG_TCP_CONG_ADVANCED=y
CONFIG_TCP_CONG_BBR=y
CONFIG_NET_SCH_FQ=y
CONFIG_TCP_CONG_BIC=n
CONFIG_TCP_CONG_WESTWOOD=n
CONFIG_TCP_CONG_HTCP=n

# === SUSFS Config ===
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_SUS_MAP=y
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_AUTO_ADD_SUS_KSU_DEFAULT_MOUNT=y
CONFIG_KSU_SUSFS_AUTO_ADD_SUS_BIND_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_TRY_UMOUNT=y
CONFIG_KSU_SUSFS_AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y
# builtin 分支无条件调用 susfs 的 priority_uid_filter 函数，而上游没给
# 它 Kconfig 定义（见 _ensure_susfs_priority_uid_filter_kconfig）。
# 这里显式开启，语义与 susfs 上游默认一致（default y）。
# builtin 分支无条件调用 susfs 的 priority_uid_filter 函数，而上游没给
# 它 Kconfig 定义（见 _ensure_susfs_priority_uid_filter_kconfig）。
#
# 2026-11-09 实测：默认**关闭**（=n）。原因：
#   这个特性会让 susfs 在 kthread 上下文里 commit_creds() 并切换
#   SELinux 域到 u:r:ksu:s0（fs/susfs.c 的 susfs_switch_to_ksu_domain），
#   而 SukiSU builtin 的 pkg_observer.c 是**无条件**调用
#   susfs_parse_priority_app_strings() 的 —— 没有 CONFIG 门控，
#   所以只要这个 CONFIG 打开，ksu 的 fsnotify/task_work 路径就会触发
#   改 creds + 切域，在 early 阶段有 oops 风险。
#   WildKernels 上游对同类问题（older branches 没有该符号）的处理是
#   把调用点编译掉（见其 susfs_guard_priority_helpers.py 注释）。
#
#   保留 =y 的能力（Kconfig 定义照旧补上，用户可自行在 defconfig 打开），
#   但默认不启用，与 WildKernels 的实际行为一致。
CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER=n
"""

    ZRAM_CONFIG_5_10 = "CONFIG_ZSMALLOC=y\nCONFIG_ZRAM=y\nCONFIG_MODULE_SIG=n\nCONFIG_CRYPTO_LZO=y\nCONFIG_ZRAM_DEF_COMP_LZ4KD=y\n"
    ZRAM_CONFIG_COMMON = "CONFIG_CRYPTO_LZ4HC=y\nCONFIG_CRYPTO_LZ4K=y\nCONFIG_CRYPTO_LZ4KD=y\nCONFIG_CRYPTO_842=y\nCONFIG_CRYPTO_LZ4K_OPLUS=y\nCONFIG_ZRAM_WRITEBACK=y\n"

    def __init__(self, config: BuildConfig, workspace: str):
        self.config = config
        self.workspace = Path(workspace)
        self.shell = ShellCommand(cwd=workspace)
        self.env = os.environ.copy()
        self.work_dir = self.workspace / config.config_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.susfs_dir = self.workspace / "susfs4ksu"
        self.sukisu_patch_dir = self.workspace / "SukiSU_patch"
        self.anykernel_dir = self.workspace / "AnyKernel3"
        self.kernel_patches_dir = self.workspace / "kernel_patches"
        self.toolchain_dir = self.workspace / "toolchain"
        self.mkbootimg_dir = self.workspace / "mkbootimg"
        self.manifest = {
            "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kernel": f"{config.android_version}-{config.kernel_version}.{config.sub_level}-{config.os_patch_level}",
            "susfs_version_label": SUSFS_VERSION_LABEL,
            "ksu_pin_requested": KSU_PIN,
            "susfs_pin_requested": SUSFS_PIN,
            "sukisu_patch_pin_requested": SUKISU_PATCH_PIN,
        }
        self._setup_env()

    def _setup_env(self):
        self.env["CONFIG"] = self.config.config_name
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env["CCACHE_HARDLINK"] = "true"
        self.shell.env = self.env

    def _run_cmd(self, cmd: str, **kwargs) -> subprocess.CompletedProcess:
        return self.shell.run(cmd, **kwargs)

    def _chdir(self, path: Path):
        os.chdir(path)
        self.shell.cwd = str(path)

    def _apply_susfs_commit(self):
        if not self.config.susfs_commit or not self.susfs_dir.exists():
            return
        self._chdir(self.susfs_dir)
        if self.config.susfs_commit.startswith("HEAD~"):
            self._run_cmd("git fetch origin", check=False)
            self._run_cmd(f"git reset --hard {self.config.susfs_commit}", check=False)
        else:
            self._run_cmd("git fetch origin", check=False)
            self._run_cmd(f"git checkout {self.config.susfs_commit}", check=False)
        self._chdir(self.workspace)

    def clone_repositories(self):
        logger.info("=== 开始克隆仓库 ===")
        # pin 为空表示跟随分支 HEAD（不推荐）。默认全部钉到上游已验证互锁的
        # 一组 commit：susfs v2.3.0 / SukiSU_patch 当前 main。
        susfs_pin = SUSFS_REPO_CONFIG.get("pin") or ""
        patch_pin = SUKISU_PATCH_REPO_CONFIG.get("pin") or ""

        for name, repo_dir, url, branch, pin in [
            ("SUSFS", self.susfs_dir, SUSFS_REPO_CONFIG['repo_url'], self.config.kernel_branch, susfs_pin),
            ("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG['repo_url'], None, patch_pin),
            ("AnyKernel3", self.anykernel_dir, ANYKERNEL_CONFIG['repo_url'], ANYKERNEL_CONFIG['branch'], ""),
            ("Kernel Patches", self.kernel_patches_dir, KERNEL_PATCHES_CONFIG['repo_url'], None, ""),
        ]:
            if not repo_dir.exists():
                cmd = f"git clone {url}"
                if branch:
                    cmd += f" -b {branch}"
                logger.info(f"克隆 {name}...")
                # 关键：clone / pin 失败必须显性报错，不能静默继续（原 check=False）
                self._run_cmd(cmd, check=True)
            else:
                logger.info(f"{name} 已存在，跳过")
            if pin:
                self._chdir(repo_dir)
                r = self._run_cmd(f"git checkout --detach {pin}", check=False)
                if r.returncode != 0:
                    self._run_cmd(f"git fetch --no-tags origin {pin}", check=True)
                    self._run_cmd("git checkout --detach FETCH_HEAD", check=True)
                self._chdir(self.workspace)
        self._apply_susfs_commit()
        self.manifest["susfs_commit"] = _read_git_head(self.susfs_dir) or "unknown"
        self.manifest["susfs_branch"] = self.config.kernel_branch
        self.manifest["sukisu_patch_commit"] = _read_git_head(self.sukisu_patch_dir) or "unknown"
        logger.info(f"SUSFS HEAD={self.manifest['susfs_commit']}, "
                    f"SukiSU_patch HEAD={self.manifest['sukisu_patch_commit']}")
        logger.info("=== 仓库克隆完成 ===")

    def clone_toolchain(self):
        logger.info("=== 克隆工具链 ===")
        if not self.toolchain_dir.exists():
            self._run_cmd(f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/kernel/prebuilts/build-tools "
                         f"-b {TOOLCHAIN_CONFIG['build_tools_branch']} --depth 1 {self.toolchain_dir}", check=False)
        if not self.mkbootimg_dir.exists():
            self._run_cmd(f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/platform/system/tools/mkbootimg "
                         f"-b {TOOLCHAIN_CONFIG['mkbootimg_branch']} --depth 1 {self.mkbootimg_dir}", check=False)
        self.env["AVBTOOL"] = str(self.toolchain_dir / "linux-x86/bin/avbtool")
        self.env["MKBOOTIMG"] = str(self.mkbootimg_dir / "mkbootimg.py")
        self.env["UNPACK_BOOTIMG"] = str(self.mkbootimg_dir / "unpack_bootimg.py")
        if "BOOT_SIGN_KEY_PATH" in os.environ:
            self.env["BOOT_SIGN_KEY_PATH"] = os.environ["BOOT_SIGN_KEY_PATH"]
        self.shell.env = self.env
        logger.info("=== 工具链准备完成 ===")

    def setup_repo_tool(self):
        logger.info("=== 安装 repo 工具 ===")
        repo_dir = self.workspace / "git-repo"
        repo_dir.mkdir(exist_ok=True)
        repo_path = repo_dir / "repo"
        if not repo_path.exists() or repo_path.stat().st_size < 1024:
            # 2026-11-10：原来 check=False 且无大小校验，curl 失败会留下 0 字节
            # 文件、后续 $REPO init 报 "python: not found" 之类的迷惑错误。
            self._run_cmd(
                f"curl -fL --retry 3 --connect-timeout 30 --max-time 120 "
                f"https://storage.googleapis.com/git-repo-downloads/repo -o {repo_path}",
                check=True)
            self._run_cmd(f"chmod a+rx {repo_path}", check=True)
        if not repo_path.exists() or repo_path.stat().st_size < 1024:
            raise RuntimeError(f"repo 工具下载失败或异常小: {repo_path}")
        # repo 的 shebang 是 `#!/usr/bin/env python`，ubuntu-latest 上由
        # python-is-python3 提供；这里显式探测，缺失时给出可操作的报错。
        if self._run_cmd("command -v python >/dev/null 2>&1", check=False).returncode != 0:
            raise RuntimeError(
                "缺少 `python` 命令：repo 工具的 shebang 需要它。"
                "请在 workflow 的 Install dependencies 里安装 python-is-python3")
        self.env["REPO"] = str(repo_path)
        self.shell.env = self.env

    def init_and_sync_kernel(self):
        logger.info("=== 初始化和同步内核源代码 ===")
        self._chdir(self.work_dir)
        formatted_branch = self.config.formatted_branch

        self._run_cmd(f"$REPO init --depth=1 -u https://android.googlesource.com/kernel/manifest "
                     f"-b common-{formatted_branch} --repo-rev=v2.16", check=False)

        remote = subprocess.run(f"git ls-remote https://android.googlesource.com/kernel/common {formatted_branch}",
                               shell=True, capture_output=True, text=True).stdout.strip()
        if "deprecated" in remote:
            manifest_path = self.work_dir / ".repo/manifests/default.xml"
            # 2026-11-11 加固：repo init 在已有 .repo 时会重写 default.xml，
            # 可能与本代码的读取撞上（读到了写了一半的文件）。
            # 所以改写前先确认文件完整、且含我们要替换的字符串；
            # 改写后再校验结果。若不匹配就报错，而不是让 repo sync
            # 去报 "revision xxx not found" 那种看不懂的错。
            if not manifest_path.exists():
                raise RuntimeError(f"manifest 不存在: {manifest_path}")
            content = manifest_path.read_text(errors="ignore")
            plain = f'"{formatted_branch}"'
            deprecated = f'"deprecated/{formatted_branch}"'
            if deprecated in content:
                # 已经是 deprecated 形式（repo init 保留了上一次的改写）
                logger.info(f"manifest 已是 deprecated/{formatted_branch}，无需改写")
            elif plain in content:
                content = content.replace(plain, deprecated)
                manifest_path.write_text(content)
                # 校验改写生效
                verify = manifest_path.read_text(errors="ignore")
                if deprecated not in verify:
                    raise RuntimeError("manifest 的 deprecated 前缀改写未生效")
                logger.info(f"manifest 已改写为 deprecated/{formatted_branch}")
            else:
                raise RuntimeError(
                    f"manifest 里既没有 {plain} 也没有 {deprecated}，"
                    f"无法确认 kernel/common 的 revision（文件长度 {len(content)} 字节，"
                    "可能被 repo init 截断）")

        self.env["REMOTE_BRANCH"] = remote
        logger.info("同步内核源代码...")
        self._run_cmd("$REPO --trace sync -c -j$(nproc --all) --no-tags --fail-fast", check=False)

        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            raise RuntimeError("repo sync 失败，common 目录不存在")
        self._apply_legacy_fixes(remote)
        logger.info("=== 内核源代码同步完成 ===")

    def _apply_legacy_fixes(self, remote_branch: str = ""):
        av, kv = self.config.android_version, self.config.kernel_version
        sub = self.config.get_sub_level_int()
        is_deprecated = "deprecated" in remote_branch

        if is_deprecated and av == "android13" and kv == "5.15" and sub and sub < 123:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android13-5.15-below-123']['url']} -o fix.patch && patch -p1 < fix.patch", check=False)
            self._chdir(self.work_dir)

        if av == "android12" and kv == "5.10" and sub and sub < 136:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android12-5.10-below-136']['url']} | patch -p1", check=False)
            self._chdir(self.work_dir)

    def add_kernel_supatch(self):
        if not self.config.support_op8e:
            return
        logger.info("=== 添加 OnePlus 8E 支持补丁 ===")
        drivers_dir = self.work_dir / "common/drivers"
        if not drivers_dir.exists():
            return
        self._chdir(drivers_dir)
        self._run_cmd(f"curl -LSs {OP8E_PATCH_URL} -o hmbird_patch.c", check=False)
        if (drivers_dir / "hmbird_patch.c").exists():
            with open(drivers_dir / "Makefile", "a") as f:
                f.write("obj-y += hmbird_patch.o\n")

    def add_kernelsu(self):
        """检出确定 commit 的 SukiSU-Ultra builtin 内核源码并做协议层硬校验。

        原实现从 main 分支 curl setup.sh 再 `bash -s builtin`，脚本与源码跨
        分支混用、且 setup.sh 无参时会 checkout 最新 tag 脱离分支；加上
        check=False，clone/checkout/patch 失败全被吞掉，最后只在 make 阶段
        报一个无关的顶层错误。这里全部显性化。
        """
        logger.info("=== 添加 KernelSU (SukiSU-Ultra builtin, pinned) ===")
        self._chdir(self.work_dir)
        ksu_dir = self.work_dir / KSU_REPO_CONFIG["repo_dir"]
        branch = KSU_REPO_CONFIG["branch"]
        pin = self.config.kernelsu_commit or KSU_REPO_CONFIG["pin"]

        if not ksu_dir.exists():
            self._run_cmd(f"git clone --no-checkout {KSU_REPO_CONFIG['repo_url']} "
                          f"{KSU_REPO_CONFIG['repo_dir']}", check=True)
        self._chdir(ksu_dir)
        self._run_cmd(f"git fetch --no-tags origin {branch}", check=True)
        if pin:
            r = self._run_cmd(f"git checkout --detach {pin}", check=False)
            if r.returncode != 0:
                self._run_cmd(f"git fetch --no-tags origin {pin}", check=True)
                self._run_cmd("git checkout --detach FETCH_HEAD", check=True)
        else:
            self._run_cmd(f"git checkout -B {branch} origin/{branch}", check=True)

        head = _read_git_head(ksu_dir)
        if not head:
            raise RuntimeError("无法确定 KernelSU 源码 HEAD，构建中止")
        logger.info(f"KernelSU HEAD: {head} (branch={branch}, pin={pin or 'HEAD'})")

        # detached HEAD 会让 kernel/Kbuild 的 git 版本启发式失效：
        # `rev-parse --abbrev-ref HEAD` 返回字面 "HEAD"，merge-base 于是落到
        # origin/HEAD，KSU_VERSION 随 clone 布局漂移 -> 内核数字版本与
        # manager/ksud 不一致 -> manager 显示「未安装 / 需更新内核」。
        # 按 WildKernels root-setup 的做法：unshallow 后显式算 base commit，
        # 把确定值追加进 Kbuild（只覆盖含该启发式的文件）。
        self._pin_ksu_version(ksu_dir, head, branch)

        uapi = _uapi_version(ksu_dir)
        ext = _has_extension_hub(ksu_dir)
        logger.info(f"KSU UAPI={uapi}, extension_hub={ext}")
        if uapi is None:
            raise RuntimeError("读不到 KERNEL_SU_UAPI_VERSION：内核源码布局可能已变化")
        if uapi < KSU_MIN_UAPI_VERSION or not ext:
            raise RuntimeError(
                f"内核过旧或不兼容 (UAPI={uapi}, extension_hub={ext})："
                f"需要 UAPI>={KSU_MIN_UAPI_VERSION} 且含 KSU_IOCTL_EXTENSION，"
                f"否则新版 manager 会显示「未安装」且模块/KPM/授权全废。pin={pin}")

        self.manifest.update({
            "ksu_commit": head, "ksu_branch": branch, "ksu_pin": pin or "HEAD",
            "ksu_uapi_version": uapi, "ksu_extension_hub": ext,
        })

        self._chdir(self.work_dir)
        setup_sh = ksu_dir / KSU_REPO_CONFIG["setup_script_rel"]
        if not setup_sh.exists():
            raise RuntimeError(f"缺少 {setup_sh}：无法集成内核源码")
        # 绝不执行 setup.sh：builtin 版内部会 `git checkout main` + `git pull`
        # + `git checkout 最新 tag`，把上面刚 pin 好的源码覆盖掉；--cleanup
        # 还会直接 `rm -rf KernelSU/`。这里只复用它的三个集成动作（软链、
        # Makefile、Kconfig），由 _setup_kernelsu_symlink 完成。
        self._setup_kernelsu_symlink(ksu_dir)

    def _pin_ksu_version(self, ksu_dir: Path, head: str, branch: str):
        """保证内核的 KSU_VERSION 与官方 manager/ksud 同源，不擅自改公式。

        关键事实（实测 git 历史）：
          - builtin:kernel/Makefile 写死 REPO_BRANCH := main，
            KSU_VERSION = 40000 + count(main) - 2815；
          - main:kernel/Kbuild 与 userspace/ksud/build.rs 用完全相同的
            40000 - 2815 + rev-list --count HEAD(main)。
        三者同源。

        2026-11-09 本地实测的版本不匹配问题：
          内核侧 count(main) 取的是**构建时刻** main 分支 HEAD，
          manager 侧 count(HEAD) 取的是**构建 manager APK 时刻**的 main。
          官方 release 资产名直接带版本号，实测最新为
            v4.3.0-r2 -> SukiSU_v4.3.0-r2_41150-release.apk
          而 tag v4.3.0-r2 (645ef986) 的 count=3965 -> 41150。
          若不锚定，构建时 main 已前进到 count=3980 -> 41165，
          manager 侧
            HomeUiState.isVersionMismatch =
              isManager && (kernelUAPIVersion != managerUAPIVersion ||
                            ksuVersion != currentManagerVersionCode)
          就会判定版本不匹配并弹警告。

        对策（与 WildKernels/GKI_KernelSU_SUSFS 的 root-setup action 同一思路）：
        把内核侧 KSU_VERSION 显式 override 成配套 manager 的 versionCode，
        并把 KSU_VERSION_FULL 钉成配套 tag，标签化写入便于溯源。

        绝不改公式本身：override 由 _override_ksu_version() 插入到
        `ccflags-y += -DKSU_VERSION=` 之前（`:=` 立即展开，晚插无效），
        且带幂等标记。
        """
        shallow = ksu_dir / ".git" / "shallow"
        if shallow.exists():
            self._run_cmd("git fetch --unshallow --tags origin", check=False)
        if shallow.exists():
            logger.warning("仓库仍是 shallow clone，KSU_VERSION 可能不准确")
        # 显式把 main 取成本地可数 ref，供 kernel/Makefile 的
        # `rev-list --count main` 使用（与官方 manager 同源）。
        self._run_cmd("git fetch --no-tags origin main:refs/remotes/origin/main", check=False)
        self._run_cmd("git fetch --no-tags origin main:refs/heads/main", check=False)
        r = self._run_cmd("git rev-list --count refs/remotes/origin/main",
                          check=False, capture_output=True)
        count = int((r.stdout or "0").strip() or 0) if r.returncode == 0 else 0
        version = 40000 + count - 2815
        self.manifest.update(ksu_version_expected=version,
                             ksu_version_main_commit_count=count,
                             ksu_version_formula="40000 + count(main) - 2815 (unchanged)")
        if count:
            logger.info(f"内核 KSU_VERSION 预期={version} (40000 + {count} - 2815, main)")
        else:
            logger.warning("无法数 main 提交数；若 GitHub API 也限流，KSU_VERSION 可能掉到 13000，"
                           "manager 会报版本不匹配 —— 请检查 BUILD_MANIFEST.txt")
        self._override_ksu_version(ksu_dir)

    def _override_ksu_version(self, ksu_dir: Path):
        """把内核 KSU_VERSION 显式钉成配套 manager 的 versionCode。

        与 WildKernels 的 root-setup action 同思路：不动上游公式，
        只在 kernel/Makefile 的 `ccflags-y += -DKSU_VERSION=` 之前插入
        `KSU_VERSION := <n>`，并写明来源，便于事后溯源。

        幂等：已打过标记则跳过。
        """
        if not KSU_VERSION_PIN:
            logger.info("未设置 KSU_VERSION_PIN，沿用内核自带版本启发式")
            self.manifest["ksu_version_override"] = "disabled"
            return
        makefile = ksu_dir / "kernel" / "Makefile"
        if not makefile.exists():
            logger.warning(f"未找到 {makefile}，跳过 KSU_VERSION 锚定")
            self.manifest["ksu_version_override"] = "makefile-missing"
            return
        content = makefile.read_text(errors="ignore")
        marker = "# CI: deterministic KSU_VERSION (GKI_KernelSU_SUSFS)"
        if marker in content:
            logger.info("KSU_VERSION 已锚定，跳过")
            self.manifest["ksu_version_override"] = "already-pinned"
            return
        # 必须确认上游确实有这两个变量，避免改名后盲目追加。
        if "KSU_VERSION" not in content or "KSU_VERSION_FULL" not in content:
            raise RuntimeError("kernel/Makefile 缺少 KSU_VERSION / KSU_VERSION_FULL，"
                               "上游结构可能已变化，中止以免产出版本错乱的内核")
        tag = KSU_MANAGER_TAG or "unknown"
        # tag 自带 "v" 前缀（v4.3.0-r2），不要再叠一个 v
        full = tag if tag.startswith("v") else f"v{tag}"
        block_lines = [
            marker,
            "# 内核与 manager 的数字版本都来自 40000 - 2815 + rev-list --count，",
            "# 但内核取构建时刻的 main HEAD、manager 取构建 APK 时刻的 HEAD，",
            f"# 两者会随 main 前进而错位（实测 41165 vs {KSU_VERSION_PIN}）。",
            "# 这里锚定到配套 manager release，使 kernelUAPIVersion/版本码一致。",
            f"KSU_VERSION := {KSU_VERSION_PIN}",
            f"KSU_VERSION_FULL := \"{full}\"",
        ]
        # 关键：`KSU_VERSION := ...` 是立即展开的；ccflags-y += -DKSU_VERSION=$(KSU_VERSION)
        # 那一行一旦被解析，值就固定了。所以 override 必须插在它**之前**，
        # 追加到文件末尾无效（2026-11-09 实测内核里仍是 41165）。
        lines_mk = content.split("\n")
        anchor_idx = None
        for i, line in enumerate(lines_mk):
            if line.strip().startswith("ccflags-y") and "-DKSU_VERSION=" in line:
                anchor_idx = i
                break
        if anchor_idx is None:
            raise RuntimeError(
                "kernel/Makefile 里找不到 'ccflags-y += -DKSU_VERSION=' 锚点，"
                "上游结构可能已变化，中止以免产出版本错乱的内核")
        lines_mk[anchor_idx:anchor_idx] = block_lines + [""]
        makefile.write_text("\n".join(lines_mk))
        self.manifest.update(ksu_version_override="pinned",
                             ksu_version_pinned=KSU_VERSION_PIN,
                             ksu_version_manager_tag=tag,
                             ksu_version_manager_base_commit=KSU_MANAGER_BASE_COMMIT)
        logger.info(f"已把内核 KSU_VERSION 锚定为 {KSU_VERSION_PIN}（配套 manager {tag}）")

    def _setup_kernelsu_symlink(self, ksu_dir: Path):
        """等价于 setup.sh 的集成动作，但不做 clone/pull/checkout。"""
        common = self.work_dir / "common"
        driver_dir = common / "drivers" if (common / "drivers").is_dir() else self.work_dir / "drivers"
        if not driver_dir.is_dir():
            raise RuntimeError(f"找不到 drivers/ 目录: {driver_dir}")
        link = driver_dir / "kernelsu"
        if link.is_symlink() or link.exists():
            link.unlink()
        rel = os.path.relpath(ksu_dir / "kernel", driver_dir)
        link.symlink_to(rel)
        mk = driver_dir / "Makefile"
        content = mk.read_text(errors="ignore")
        if "kernelsu" not in content:
            with open(mk, "a") as f:
                f.write("\nobj-$(CONFIG_KSU) += kernelsu/\n")
        kc = driver_dir / "Kconfig"
        content = kc.read_text(errors="ignore")
        if 'source "drivers/kernelsu/Kconfig"' not in content:
            lines = content.split("\n")
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip() == "endmenu":
                    lines.insert(i, 'source "drivers/kernelsu/Kconfig"')
                    break
            kc.write_text("\n".join(lines))
        logger.info(f"已软链 drivers/kernelsu -> {rel}")

    def add_bbg(self):
        if not self.config.use_bbg:
            return
        logger.info("=== 添加 Baseband-guard ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return
        self._chdir(common_dir)
        self._run_cmd(f"wget -O- {BBG_CONFIG['setup_script']} | bash", check=False)
        config_file = common_dir / "arch/arm64/configs/gki_defconfig"
        if config_file.exists():
            with open(config_file, "a") as f:
                f.write("CONFIG_BBG=y\n")
        kconfig_file = common_dir / "security/Kconfig"
        if kconfig_file.exists():
            with open(kconfig_file, "r") as f:
                content = f.read()
            content = re.sub(r'(config LSM.*?)(default .*)(\n.*?help)',
                           lambda m: m.group(1) + ('lockdown,baseband_guard' if 'lockdown' in m.group(2) and 'baseband_guard' not in m.group(2) else m.group(2)) + m.group(3),
                           content, flags=re.DOTALL)
            with open(kconfig_file, "w") as f:
                f.write(content)

    def apply_susfs_patches(self):
        logger.info("=== 应用 SUSFS 补丁 ===")
        self._chdir(self.work_dir)
        common_dir = self.work_dir / "common"
        susfs_patch = self.susfs_dir / "kernel_patches" / self.config.get_susfs_patch_filename()
        if susfs_patch.exists():
            self._run_cmd(f"cp {susfs_patch} {common_dir}/", check=False)
        for src, dst in [
            (self.susfs_dir / "kernel_patches/fs", common_dir / "fs/"),
            (self.susfs_dir / "kernel_patches/include/linux", common_dir / "include/linux/"),
        ]:
            if src.exists():
                self._run_cmd(f"cp -r {src}/* {dst}", check=False)
        if susfs_patch.exists():
            patch_file = common_dir / self.config.get_susfs_patch_filename()
            if patch_file.exists():
                self._chdir(common_dir)

                # 2026-11-10 实测（android13-5.15-189 真实构建失败）：
                # Google 在较新 GKI sublevel 里删掉了 `<trace/hooks/*.h>` include
                # 行，而 susfs4ksu 的补丁仍把它们当首个 hunk 的上下文，导致
                #   fs/namespace.c Hunk #1 FAILED at 32
                # 换 susfs 仓库 / 换分家族 pin 都无效（WildKernels 同 pin 的
                # patch 上下文逐字相同）。WildKernels 的解法是打补丁前用
                # susfs_restore_context.py 把缺失的 include 按补丁期望顺序补回，
                # 头文件不存在时建空 stub（SUSFS 不用这些 trace hook 符号）。
                # 实测：补 3 行后 patch rc=0、0 个 .rej。该脚本通用、幂等、无版本门槛。
                # 脚本与 kernel_builder.py 同目录（.github/workflows/scripts/），
                # 用 __file__ 定位，避免依赖 workspace 布局。
                restore = Path(__file__).resolve().parent / "susfs_restore_context.py"
                if restore.exists():
                    self._run_cmd(
                        f"python3 {restore} {patch_file.name} {common_dir}", check=True)
                    self.manifest["susfs_context_restored"] = True
                else:
                    logger.warning("未找到 susfs_restore_context.py，跳过上下文恢复（新 sublevel 可能补丁失败）")

                # 补丁应用（不再用 --fuzz=3：WildKernels 用裸 patch -p1，且
                # 明确不用 --forward，避免 reversed/already-applied hunk 被跳过）。
                r = self._run_cmd(f"patch -p1 < {patch_file.name}",
                                  check=False, capture_output=True)
                out = (r.stdout or "") + (r.stderr or "")
                for line in out.splitlines():
                    logger.info(line)
                if r.returncode != 0:
                    rejects = list(common_dir.rglob("*.rej"))
                    logger.error(
                        f"SUSFS 补丁应用失败（rc={r.returncode}），"
                        f"失败的 hunk 文件: {[str(x.relative_to(common_dir)) for x in rejects]}")
                    for rej in rejects[:5]:
                        logger.error(f"--- {rej.relative_to(common_dir)} ---")
                        for line in rej.read_text(errors="ignore").splitlines()[:24]:
                            logger.error(f"    {line}")
                    raise RuntimeError(
                        "SUSFS 补丁应用失败（上下文恢复后仍不匹配），中止构建。"
                        "请查看上方 .rej 内容定位具体 hunk")
                self._chdir(self.work_dir)

    def apply_sukisu_patches(self):
        # 69_hide_stuff.patch（2024-12 老补丁）改的是 fs/proc/task_mmu.c 的
        # show_map_vma / show_vma_header_prefix，与 susfs v2.3.0 的 task_mmu.c
        # 补丁改的是同一段。两者叠加时 --fuzz=3 可能「蒙混过关」但留下悬空
        # label / 未声明变量（例如它插入的 `goto done;` 在 v2.3.0 里已无对应
        # 标签），编译阶段报一堆难定位的错。
        # 它只是 maps 隐藏加固，不提供 root / KPM / SUSFS 核心能力，故默认跳过。
        # 需要时把 APPLY_HIDE_STUFF_PATCH 设为 True 再显式启用。
        APPLY_HIDE_STUFF_PATCH = False
        if not APPLY_HIDE_STUFF_PATCH:
            logger.info("=== 跳过 69_hide_stuff.patch（与 susfs v2.3.0 的 task_mmu.c 冲突） ===")
            self.manifest["hide_stuff_patch_applied"] = False
            return
        logger.info("=== 应用 SukiSU 补丁 ===")
        self._chdir(self.work_dir / "common")
        hooks_patch = self.sukisu_patch_dir / "69_hide_stuff.patch"
        if hooks_patch.exists():
            self._run_cmd(f"cp {hooks_patch} . && patch -p1 -F 3 < 69_hide_stuff.patch", check=True)
            self.manifest["hide_stuff_patch_applied"] = True

    def apply_zram_patches(self):
        if not self.config.use_zram:
            return
        logger.info("=== 应用 ZRAM (LZ4KD) 补丁 ===")
        self._chdir(self.work_dir / "common")
        for src in [
            (self.sukisu_patch_dir / "other/zram/lz4k/include/linux", "include/linux/"),
            (self.sukisu_patch_dir / "other/zram/lz4k/lib", "lib/"),
            (self.sukisu_patch_dir / "other/zram/lz4k/crypto", "crypto/"),
            (self.sukisu_patch_dir / "other/zram/lz4k_oplus", "lib/"),
        ]:
            if src[0].exists():
                self._run_cmd(f"cp -r {src[0]}/* {src[1]}", check=False)
        zram_patch_dir = self.sukisu_patch_dir / f"other/zram/zram_patch/{self.config.kernel_version}"
        for patch in ["lz4kd.patch", "lz4k_oplus.patch"]:
            p = zram_patch_dir / patch
            if p.exists():
                self._run_cmd(f"patch -p1 -F 3 < {p}", check=False)

    def apply_task_mmu_fixes(self):
        logger.info("=== 应用 task_mmu.c 修复 ===")
        self._chdir(self.work_dir / "common")
        task_mmu = Path("fs/proc/task_mmu.c")
        if not task_mmu.exists():
            return

        # 读取文件内容
        with open(task_mmu, "r") as f:
            content = f.read()

        # ===== 修复 VMA_PAD_START 未定义 =====
        # susfs v2.3.0 的 task_mmu.c hunk 用了 `end = VMA_PAD_START(vma);`。
        #
        # 2026-11-09 本地实测纠正：deprecated/android12-5.10-2025-05 树里
        # **本来就有** 这个宏：
        #   include/linux/pgsize_migration_inline.h:45
        #   #define VMA_PAD_START(vma) (vma->vm_end - (vma_pad_pages(vma) << PAGE_SHIFT))
        # 它是 16KB page size 支持的后移植（mm/pgsize_migration.c 在用），
        # 由 include/linux/pgsize_migration.h 引入。
        #
        # 上一版无条件往 task_mmu.c 里插一份自己的定义，于是同一个 TU 里
        # 出现两份 VMA_PAD_START -> 编译报：
        #   include/linux/pgsize_migration_inline.h:45:9: error:
        #   'VMA_PAD_START' macro redefined [-Werror,-Wmacro-redefined]
        #
        # 正确做法：只在整棵树里都找不到该宏时才补兜底定义；
        # 已经存在就什么都不做（不能覆盖内核自带的 16KB 语义）。
        if "VMA_PAD_START" in content and "#define VMA_PAD_START" not in content:
            kernel_has_macro = self._tree_defines_vma_pad_start()
            if kernel_has_macro:
                logger.info("内核树已自带 VMA_PAD_START（16KB page support），不再注入兜底定义")
                self.manifest["vma_pad_start_macro"] = "kernel-provided"
            else:
                logger.info("检测到 VMA_PAD_START 未定义，正在添加宏定义...")
                fix = ("// VMA_PAD_START fix for SUSFS (5.10 has no such macro)\n"
                       "#ifndef VMA_PAD_START\n"
                       "#define VMA_PAD_START(vma) ((vma)->vm_end)\n"
                       "#endif\n")
                lines = content.split('\n')
                insert_at = next((i for i, l in enumerate(lines)
                                  if l.lstrip().startswith('#include')), 0)
                lines.insert(insert_at, fix)
                content = '\n'.join(lines)
                logger.info(f"已在第 {insert_at + 1} 行前插入 VMA_PAD_START 宏定义")
                self.manifest["vma_pad_start_macro"] = "injected-fallback"
        elif "#define VMA_PAD_START" in content:
            self.manifest["vma_pad_start_macro"] = "already-defined"

        # ===== 修复 dentry 未初始化（如果还存在） =====
        if "struct dentry *dentry;" in content:
            content = content.replace("struct dentry *dentry;", "struct dentry *dentry = NULL;")
            logger.info("已修复 dentry 未初始化问题")

        # ===== 写入修改 =====
        with open(task_mmu, "w") as f:
            f.write(content)

        # ===== 原有的修复逻辑 =====
        fb = f"{self.config.android_version}-{self.config.kernel_version}"
        
        if fb == "android15-6.6" and "unsigned int nr_subpages" not in content:
            self._fix_base_c_header()
        elif fb == "android14-6.1" and "if (!vma_pages(vma))" not in content:
            self._fix_base_c_header()
            # 重新读取内容，因为可能已被修改
            with open(task_mmu, "r") as f:
                content = f.read()
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)
        elif fb in ["android12-5.10", "android13-5.10", "android13-5.15"] and "if (!vma_pages(vma))" not in content:
            with open(task_mmu, "r") as f:
                content = f.read()
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)

    def _tree_defines_vma_pad_start(self) -> bool:
        """整棵内核树里是否已经有 VMA_PAD_START 的定义。

        2026-11-09 实测：deprecated/android12-5.10-2025-05 已经有
        include/linux/pgsize_migration_inline.h 提供该宏（16KB page 支持），
        此时再往 task_mmu.c 注入一份会触发 macro-redefined 编译错误。
        """
        common = self.work_dir / "common"
        if not common.is_dir():
            return False
        for rel in ("include/linux/pgsize_migration_inline.h",
                    "include/linux/pgsize_migration.h",
                    "include/linux/mm_types.h",
                    "include/linux/mm.h"):
            p = common / rel
            if not p.exists():
                continue
            try:
                if "#define VMA_PAD_START" in p.read_text(errors="ignore"):
                    return True
            except OSError:
                continue
        return False

    def _ensure_susfs_priority_uid_filter_kconfig(self):
        """补上 CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER 的 Kconfig 定义。

        2026-11-09 本地实测发现的真实上游缺口：
          - susfs v2.3.0 (b0e4746) 的 fs/susfs.c 里实现了
            susfs_set_priority_uid_filter() / susfs_parse_priority_app_strings()，
            include/linux/susfs.h 里的声明被
                #ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER
            包着；
          - 该 Kconfig 定义**只**存在于 susfs 的
            kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch
            （那是给「独立 KernelSU 仓库」路线用的），GKI 路线用的
            50_add_susfs_in_gki-*.patch 里没有；
          - SukiSU-Ultra 的 builtin 分支在提交 a864d15a
            ("susfs: Add priority uid filter & kernel & KernelSU: Sync with upstream")
            里**无条件**调用了这两个函数，却同样没加 Kconfig 定义 ——
            全分支扫描确认 builtin 是唯一「有调用、无定义」的组合，
            也就是说 builtin 分支自身在未提供该 CONFIG 时编译不过：

                drivers/kernelsu/manager/pkg_observer.c:24:5: error:
                  implicit declaration of function 'susfs_parse_priority_app_strings'
                drivers/kernelsu/supercall/dispatch.c:478:13: error:
                  implicit declaration of function 'susfs_set_priority_uid_filter'

        修法：把上游在 10_enable_susfs_for_ksu.patch 里用的那段 Kconfig
        原文照搬到 KSU 的 Kconfig（同一个 menu 内、同样的 depends on 与
        default），这样该功能变成可配置项，语义与 susfs 上游完全一致。
        幂等：已存在则跳过。
        """
        kconfig = self.work_dir / "common" / "drivers" / "kernelsu" / "Kconfig"
        if not kconfig.exists():
            logger.warning(f"未找到 {kconfig}，跳过 PRIORITY_UID_FILTER Kconfig 修复")
            self.manifest["susfs_priority_uid_filter_kconfig"] = "kconfig-missing"
            return
        content = kconfig.read_text(errors="ignore")
        if "config KSU_SUSFS_PRIORITY_UID_FILTER" in content:
            logger.info("Kconfig 已有 KSU_SUSFS_PRIORITY_UID_FILTER 定义，跳过")
            self.manifest["susfs_priority_uid_filter_kconfig"] = "already-present"
            return
        block = (
            "config KSU_SUSFS_PRIORITY_UID_FILTER\n"
            "\tbool \"Enable to allow adding user-defined uids that will be hidden from getpriority/setpriority/ioprio_get/ioprio_set syscall\"\n"
            "\tdepends on KSU_SUSFS\n"
            "\tdefault y\n"
            "\thelp\n"
            "\t  - Allow hiding user-defined uids from syscalls like getpriority/setpriority/ioprio_get/ioprio_set.\n"
            "\t  - Effective on all processes with uid >= 10000.\n"
            "\n"
        )
        # 插到 KSU_SUSFS_SUS_MAP 之后（最后一个 susfs 子选项）的 endmenu 之前，
        # 与 susfs 上游 10_enable_susfs_for_ksu.patch 的插入位置一致。
        anchor = "config KSU_SUSFS_SUS_MAP"
        idx = content.find(anchor)
        if idx < 0:
            logger.warning("Kconfig 里找不到 config KSU_SUSFS_SUS_MAP，未插入 PRIORITY_UID_FILTER")
            self.manifest["susfs_priority_uid_filter_kconfig"] = "anchor-missing"
            return
        # 从 anchor 起找该 config 块的结尾（下一个顶层 config 或 endmenu）
        rest = content[idx:]
        m = re.search(r'\nendmenu\n', rest)
        if not m:
            logger.warning("Kconfig 结构异常（未找到 endmenu），未插入 PRIORITY_UID_FILTER")
            self.manifest["susfs_priority_uid_filter_kconfig"] = "structure-unknown"
            return
        insert_at = idx + m.start() + 1
        new_content = content[:insert_at] + block + content[insert_at:]
        kconfig.write_text(new_content)
        self.manifest["susfs_priority_uid_filter_kconfig"] = "inserted"
        logger.info("已在 KSU Kconfig 中补上 KSU_SUSFS_PRIORITY_UID_FILTER 定义")

    def _guard_susfs_credential_helpers(self):
        """把 susfs.c 里两个 credential helper 的定义也用同一个 CONFIG 门控。

        2026-11-09 实测（与 WildKernels 的 susfs_guard_priority_helpers.py 同一问题）：

          susfs v2.3.0 的 fs/susfs.c 把
              static struct cred *susfs_switch_to_ksu_domain(void)   // 第 183 行
              static void susfs_restore_creds(struct cred *)          // 第 217 行
          的**定义**放在了 `#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER`
          块**外面**，而它们的唯一调用点在块**里面**。
          于是当该 CONFIG 关闭时（我们默认关闭，见 KERNEL_CONFIG_TEMPLATE）：
              fs/susfs.c:183:21: error: unused function 'susfs_switch_to_ksu_domain'
              fs/susfs.c:217:13: error: unused function 'susfs_restore_creds'
          —— GKI 用 -Werror 把死代码变成编译错误。

          修法与 WildKernels 上游一致：把这两个定义包进同一个 #ifdef。
          CONFIG 打开时行为完全不变；关闭时定义随调用点一起消失。

          幂等：已门控则跳过。
        """
        f = self.work_dir / "common" / "fs" / "susfs.c"
        if not f.exists():
            logger.warning("未找到 fs/susfs.c，跳过 credential helper 门控")
            self.manifest["susfs_credential_helpers_guarded"] = "file-missing"
            return
        content = f.read_text(errors="ignore")
        if "credential helpers */" in content:
            logger.info("susfs.c: credential helpers 已门控，跳过")
            self.manifest["susfs_credential_helpers_guarded"] = "already-guarded"
            return

        GUARD = "#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER\n"
        ENDIF = "#endif // #ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER /* credential helpers */\n"
        SWITCH_SIG = "static struct cred *susfs_switch_to_ksu_domain(void) {"
        RESTORE_SIG = "static void susfs_restore_creds(struct cred *orig_cred) {"

        if SWITCH_SIG not in content:
            logger.info("susfs.c: credential helpers 不存在（无需处理）")
            self.manifest["susfs_credential_helpers_guarded"] = "not-present"
            return
        if RESTORE_SIG not in content:
            logger.warning("susfs.c: 找不到 susfs_restore_creds 定义，无法门控")
            self.manifest["susfs_credential_helpers_guarded"] = "restore-sig-missing"
            return

        # 找到 restore 函数定义的右花括号结束位置
        brace = content.find("{", content.find(RESTORE_SIG))
        depth = 0
        i = brace
        end = -1
        while i < len(content):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1
        if end < 0:
            logger.warning("susfs.c: 花括号不配对，无法门控")
            self.manifest["susfs_credential_helpers_guarded"] = "unbalanced-braces"
            return

        # 找到 switch 函数的文档注释起点（紧邻其上方的 /* ... */）
        doc_marker = " * Switches the selinux domain of the current process to the KernelSU one"
        doc = content.find(doc_marker)
        if doc > 0:
            start = content.rfind("/*", 0, doc)
        else:
            start = content.rfind("/*", 0, content.find(SWITCH_SIG))
        if start < 0:
            start = content.find(SWITCH_SIG)

        new_content = (content[:end] + "\n" + ENDIF + content[end:])
        new_content = (new_content[:start] + GUARD + new_content[start:])

        # 校验：guard 必须包住两个定义
        g = new_content.find(GUARD)
        s_i = new_content.find(SWITCH_SIG)
        r_i = new_content.find(RESTORE_SIG)
        e_i = new_content.find(ENDIF)
        if not (0 <= g < s_i < r_i < e_i):
            raise RuntimeError("susfs.c: 门控写入后校验失败，中止以免产出更坏的内核")
        f.write_text(new_content)
        self.manifest["susfs_credential_helpers_guarded"] = True
        logger.info("已把 susfs.c 的 credential helpers 包进 CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER")

    def _guard_ksu_priority_uid_filter_calls(self):
        """把 KSU 侧对 susfs priority_uid_filter 的调用点也用 CONFIG 门控。

        2026-11-09 实测发现的上游缺口（与 WildKernels 的处理一致）：
          SukiSU-Ultra builtin 分支在提交 a864d15a 里**无条件**调用了
          susfs_parse_priority_app_strings()（kernel/manager/pkg_observer.c）
          和 susfs_set_priority_uid_filter()（kernel/supercall/dispatch.c），
          但这两个函数在 susfs 侧被
              #ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER
          包着。于是：
            - CONFIG 打开  -> 特性生效，但 susfs 会在 kthread 上下文
                              commit_creds() 并切 SELinux 域到 u:r:ksu:s0，
                              而 pkg_observer 的调用没有 CONFIG 门控，
                              在 early boot 阶段有 oops 风险；
            - CONFIG 关闭  -> susfs.h 里的声明不可见，
                              编译报 implicit declaration（我们最初撞到的）。

          正确做法（也是 WildKernels susfs_guard_priority_helpers.py 的思路）：
          把 **调用点** 也放进同一个 #ifdef，两边一致地开/关。
          这样 CONFIG=n 时整个特性从编译中消失，行为与 WildKernels 一致；
          CONFIG=y 时功能完整可用。

          幂等：已加过门控则跳过。
        """
        targets = [
            ("drivers/kernelsu/manager/pkg_observer.c",
             "susfs_parse_priority_app_strings();"),
            ("drivers/kernelsu/supercall/dispatch.c",
             "susfs_set_priority_uid_filter(arg);"),
        ]
        changed = []
        for rel, call in targets:
            f = self.work_dir / "common" / rel
            if not f.exists():
                logger.warning(f"未找到 {rel}，跳过 priority_uid_filter 调用门控")
                continue
            content = f.read_text(errors="ignore")
            if call not in content:
                continue
            # 已经门控过？（前一行是 #ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER）
            lines_c = content.split("\n")
            done = False
            for i, l in enumerate(lines_c):
                if l.strip() == call.strip():
                    prev = lines_c[i - 1].strip() if i > 0 else ""
                    if prev.startswith("#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER"):
                        done = True
                    break
            if done:
                continue
            out = []
            for l in lines_c:
                if l.strip() == call.strip():
                    indent = l[:len(l) - len(l.lstrip())]
                    out.append(f"{indent}#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER")
                    out.append(l)
                    out.append(f"{indent}#endif // #ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER")
                else:
                    out.append(l)
            f.write_text("\n".join(out))
            changed.append(rel)
        if changed:
            self.manifest["ksu_priority_uid_filter_calls_guarded"] = changed
            logger.info(f"已给 KSU 侧的 priority_uid_filter 调用加 CONFIG 门控: {changed}")
        else:
            self.manifest["ksu_priority_uid_filter_calls_guarded"] = "already-guarded-or-absent"
            logger.info("KSU 侧 priority_uid_filter 调用门控：无需改动")

    def _fix_assoc_array_header(self):
        """让 struct assoc_array 无条件完整，根治 asm-offsets 的 incomplete type。

        现象（android12-5.10 / prepare0 阶段）：
          include/linux/key.h:259: error: field has incomplete type 'struct assoc_array'
          arch/arm64/kernel/asm-offsets.c 经 <linux/sched.h> -> <linux/cred.h>
          -> <linux/key.h> 引入该头文件。

        根因：include/linux/assoc_array.h 把 struct assoc_array 的定义包在
        #ifdef CONFIG_ASSOCIATIVE_ARRAY 里；而本构建链生成的 .config 里
        CONFIG_KEYS 生效（struct key 被展开）但 CONFIG_ASSOCIATIVE_ARRAY
        没被 select（或 append 到 gki_defconfig 的配置没进最终 .config）。

        上一轮试过在 gki_defconfig 追加 CONFIG_KEYS=y / CONFIG_ASSOCIATIVE_ARRAY=y，
        报错一字不变 -> 说明 defconfig 文本追加对 GKI 的 fragment 拼装无效。
        因此改为直接改头文件：去掉这一对 #ifdef / #endif，让整个头文件
        无条件生效。

        安全性：该头文件只有类型定义、extern 函数声明和一个 static inline，
        没有变量定义；lib/assoc_array.c 是否编译由 Makefile 的
        obj-$(CONFIG_ASSOCIATIVE_ARRAY) 控制，头文件展开不会多出实现。
        key.h 因此永远能拿到完整类型，且不影响任何功能。
        """
        header = self.work_dir / "common" / "include" / "linux" / "assoc_array.h"
        if not header.exists():
            logger.warning(f"未找到 {header}，跳过 assoc_array 修复")
            return
        content = header.read_text(errors="ignore")
        if "CONFIG_ASSOCIATIVE_ARRAY" not in content:
            logger.info("assoc_array.h 已无条件，无需修复")
            return
        lines = content.split("\n")
        out = []
        removed = 0
        for line in lines:
            s = line.strip()
            # 只去掉最外层的条件编译对，保留 include guard 的 #endif
            if s == "#ifdef CONFIG_ASSOCIATIVE_ARRAY":
                out.append("/* GKI_KernelSU_SUSFS: struct assoc_array 定义改为无条件，"
                           "避免 key.h 的 incomplete type（见 _fix_assoc_array_header） */")
                removed += 1
                continue
            if s == "#endif /* CONFIG_ASSOCIATIVE_ARRAY */" or s == "#endif // CONFIG_ASSOCIATIVE_ARRAY":
                removed += 1
                continue
            out.append(line)
        # 必须成对移除：只删到一个会留下孤立的 #ifdef/#endif，反而制造新错误。
        if removed != 2:
            raise RuntimeError(
                f"assoc_array.h 条件编译对不完整（预期移除 2 行，实际 {removed}），"
                "上游头文件格式可能已变化，构建中止以免产出更坏的内核")
        new_content = "\n".join(out)
        if "struct assoc_array" not in new_content or "CONFIG_ASSOCIATIVE_ARRAY" in new_content:
            raise RuntimeError("assoc_array.h 修复后结构异常，构建中止")
        header.write_text(new_content)
        self.manifest["assoc_array_header_unconditional"] = True
        logger.info("已让 assoc_array.h 无条件生效（struct assoc_array 完整可见）")

    def _fix_vdso_gettimeofday_include(self):
        """从源头补齐 c-gettimeofday-y，一次覆盖 vdso 与 vdso32。

        现象（连续两轮）：
          第 5 轮  arch/arm64/kernel/vdso/vgettimeofday.c  报 clockid_t 等不可见
          第 7 轮  arch/arm64/kernel/vdso32/vgettimeofday.c 报同样错误
        两者都是 vgettimeofday.c 自身无 #include，靠各自 Makefile 的
            ifneq ($(c-gettimeofday-y),)
              CFLAGS_vgettimeofday.o += -include $(c-gettimeofday-y)   # vdso
              VDSO_CFLAGS_gettimeofday_o += -include $(c-gettimeofday-y) # vdso32
            endif
        注入类型/函数。而两个 Makefile 都 include 了 lib/vdso/Makefile，其中
            c-gettimeofday-$(CONFIG_GENERIC_GETTIMEOFDAY) := $(GENERIC_VDSO_DIR)gettimeofday.c
        本构建链最终 .config 里 CONFIG_GENERIC_GETTIMEOFDAY 缺失 -> 变量为空
        -> 两个 arch 都缺 -include。

        所以逐个给 vdso/vdso32 打补丁是打地鼠，正解是在**共同源头**
        lib/vdso/Makefile 里让变量在 CONFIG 未生效时回落到同一文件：
            ifeq ($(c-gettimeofday-y),)
            c-gettimeofday-y := $(GENERIC_VDSO_DIR)gettimeofday.c
            endif
        这样 vdso 和 vdso32 的 ifneq 都会自动成立，走 GKI 官方原本的
        -include 路径，与 CONFIG_GENERIC_GETTIMEOFDAY=y 时完全等价。

        幂等：用标记注释防重复追加。
        """
        lib_mk = self.work_dir / "common" / "lib" / "vdso" / "Makefile"
        generic = self.work_dir / "common" / "lib" / "vdso" / "gettimeofday.c"
        if not lib_mk.exists():
            raise RuntimeError(f"缺少 {lib_mk}，无法修复 VDSO")
        if not generic.exists():
            raise RuntimeError(f"缺少 {generic}，无法修复 VDSO")
        content = lib_mk.read_text(errors="ignore")
        if "GKI_KernelSU_SUSFS: c-gettimeofday fallback" in content:
            logger.info("c-gettimeofday-y 回落已补过，跳过")
            return
        # 必须存在官方那行定义，避免上游改名后盲目追加
        if "c-gettimeofday-$(CONFIG_GENERIC_GETTIMEOFDAY)" not in content:
            raise RuntimeError("lib/vdso/Makefile 缺少 c-gettimeofday-$(...) 定义，"
                               "上游可能已变更，中止以免产出更坏的内核")
        with open(lib_mk, "a") as f:
            f.write("\n# GKI_KernelSU_SUSFS: c-gettimeofday fallback\n")
            f.write("# CONFIG_GENERIC_GETTIMEOFDAY 未进最终 .config 时该变量为空，\n")
            f.write("# 会让 vdso 与 vdso32 的 vgettimeofday.c 都拿不到类型/函数声明。\n")
            f.write("# 回落到与官方 CONFIG=y 完全相同的文件，两个 arch 同时覆盖。\n")
            f.write("ifeq ($(c-gettimeofday-y),)\n")
            f.write("c-gettimeofday-y := $(GENERIC_VDSO_DIR)gettimeofday.c\n")
            f.write("endif\n")
        self.manifest["vdso_gettimeofday_var_fixed"] = True
        logger.info("已在 lib/vdso/Makefile 补齐 c-gettimeofday-y 回落（覆盖 vdso + vdso32）")

    def _dump_final_config(self):
        """编译前打印 .config 里关键 CONFIG 的真实值，便于定位配置层问题。

        连续多轮报错都源于「append 到 gki_defconfig 的配置没进最终 .config」，
        有了这段输出，下一轮可以直接看真实值，不必再逐个猜。
        """
        cfg = self.work_dir / "common" / ".config"
        if not cfg.exists():
            # 部分构建方式把 .config 放在 out/ 下
            candidates = list(self.work_dir.glob("**/.config"))
            cfg = candidates[0] if candidates else None
        if not cfg:
            logger.info("（编译前尚未生成 .config，跳过关键项打印）")
            return
        keys = ("CONFIG_KEYS", "CONFIG_ASSOCIATIVE_ARRAY",
                "CONFIG_GENERIC_GETTIMEOFDAY", "CONFIG_KSU", "CONFIG_KPM",
                "CONFIG_KSU_SUSFS", "CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER",
                "CONFIG_HEADERS_INSTALL")
        lines = cfg.read_text(errors="ignore").split("\n")
        logger.info(f"=== 最终 .config 关键项（{cfg}） ===")
        for k in keys:
            hit = next((l.strip() for l in lines if l.strip().startswith(k + "=")), None)
            logger.info(f"  {k} = {hit if hit else '(# not set / 缺失)'}")
        self.manifest["config_dump_path"] = str(cfg)

    def _fix_base_c_header(self):
        base_c = self.work_dir / "common/fs/proc/base.c"
        if not base_c.exists():
            return
        with open(base_c, "r") as f:
            content = f.read()
        if "#include <linux/dma-buf.h>" not in content:
            content = content.replace("#include <linux/cpufreq_times.h>",
                                    "#include <linux/cpufreq_times.h>\n#include <linux/dma-buf.h>")
            with open(base_c, "w") as f:
                f.write(content)

    def configure_kernel(self):
        logger.info("=== 配置内核 ===")
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        if not config_file.exists():
            logger.warning(f"配置文件不存在: {config_file}")
            return

        with open(config_file, "a") as f:
            f.write(self.KERNEL_CONFIG_TEMPLATE)
            if self.config.kernel_version != "6.6":
                f.write("CONFIG_KSU_SUSFS_SUS_PATH=y\n")
            else:
                f.write("CONFIG_KSU_SUSFS_SUS_PATH=n\n")

        if self.config.use_zram:
            self._configure_zram()
            self._configure_bazel()

        if self.config.set_default_bbr:
            with open(config_file, "a") as f:
                f.write("CONFIG_DEFAULT_BBR=y\n")

        # ------------------------------------------------------------------
        # 删除 POST_DEFCONFIG_CMDS="check_defconfig"（与原版一致）。
        #
        # 2026-11-09 实测纠正：check_defconfig 并不会「固化 select 配置」，
        # 它是把 `make savedefconfig` 的结果与 gki_defconfig 做 diff，
        # 不一致就 `return 1` 直接终止构建：
        #   ERROR: savedefconfig does not match gki_defconfig
        # 因为我们往 gki_defconfig 追加了 CONFIG_KSU / CONFIG_KPM /
        # CONFIG_KSU_SUSFS_* / ZRAM 等自定义项，savedefconfig 规范化时会
        # 把它们精简掉，diff 必然不一致 -> 构建必挂。原版删掉它是正确的。
        #
        # 至于 CONFIG_ASSOCIATIVE_ARRAY / CONFIG_GENERIC_GETTIMEOFDAY 缺失，
        # 恢复 check_defconfig 后 .config 里依然缺失（实测），所以那两个
        # 问题与它无关，已由 _fix_assoc_array_header() 和
        # _fix_vdso_gettimeofday_include() 在源码层根治。
        # ------------------------------------------------------------------
        build_config = self.work_dir / "common/build.config.gki"
        if build_config.exists():
            content = build_config.read_text(errors="ignore")
            if "check_defconfig" in content:
                content = content.replace("check_defconfig", "")
                build_config.write_text(content)
                logger.info("已跳过 check_defconfig（避免 savedefconfig diff 终止构建）")

    def _configure_zram(self):
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        with open(config_file, "r") as f:
            content = f.read()
        kv = self.config.kernel_version
        if kv == "5.10":
            with open(config_file, "a") as f:
                f.write(self.ZRAM_CONFIG_5_10)
        else:
            content = content.replace("CONFIG_ZRAM=m", "CONFIG_ZRAM=y")
            with open(config_file, "w") as f:
                f.write(content)
            with open(config_file, "a") as f:
                f.write("CONFIG_ZSMALLOC=y\n")
        with open(config_file, "a") as f:
            f.write(self.ZRAM_CONFIG_COMMON)

    def _configure_bazel(self):
        modules_bzl = self.work_dir / "common/modules.bzl"
        if modules_bzl.exists():
            with open(modules_bzl, "r") as f:
                content = f.read()
            modified = False
            for old in ['"drivers/block/zram/zram.ko",\n', '"drivers/block/zram/zram.ko",',
                       '"mm/zsmalloc.ko",\n', '"mm/zsmalloc.ko",']:
                if old in content:
                    content = content.replace(old, '')
                    modified = True
            if modified:
                with open(modules_bzl, "w") as f:
                    f.write(content)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        with open(config_file, "a") as f:
            f.write("CONFIG_MODULE_SIG_FORCE=n\n")

    def configure_kernel_name(self):
        logger.info("=== 配置内核名称 ===")
        self._chdir(self.work_dir)
        MAX_CUSTOM_LEN = 48
        safe_custom_version = ""
        if self.config.custom_version:
            safe_custom_version = self.config.custom_version.rstrip('-')[:MAX_CUSTOM_LEN]

        setlocalversion = self.work_dir / "common/scripts/setlocalversion"
        if setlocalversion.exists():
            with open(setlocalversion, "r") as f:
                content = f.read()
            if safe_custom_version:
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if 'echo "$res"' in line and not line.strip().startswith('#'):
                        lines[i] = f'\techo "{safe_custom_version}$res"'
                        break
                with open(setlocalversion, "w") as f:
                    f.write('\n'.join(lines))
            if "-dirty" in content:
                content = content.replace("-dirty", "")
                with open(setlocalversion, "w") as f:
                    f.write(content)

        import datetime
        current_time = datetime.datetime.utcnow().strftime("%a %b %d %H:%M:%S UTC %Y")
        mkcompile_h = self.work_dir / "common/scripts/mkcompile_h"
        if mkcompile_h.exists():
            with open(mkcompile_h, "r") as f:
                content = f.read()
            content = content.replace('UTS_VERSION="$(echo $UTS_VERSION $CONFIG_FLAGS $TIMESTAMP | cut -b -$UTS_LEN)"',
                                    f'UTS_VERSION="#1 SMP PREEMPT {current_time}"')
            with open(mkcompile_h, "w") as f:
                f.write(content)

        if self.config.kernel_version in ["6.1", "6.6"]:
            init_makefile = self.work_dir / "common/init/Makefile"
            if init_makefile.exists():
                with open(init_makefile, "r") as f:
                    content = f.read()
                content = content.replace('$(preempt-flag-y) "$(build-timestamp)"', f'$(preempt-flag-y) "{current_time}"')
                with open(init_makefile, "w") as f:
                    f.write(content)

        if not (self.work_dir / "build/build.sh").exists():
            bazel_build = self.work_dir / "common/BUILD.bazel"
            if bazel_build.exists():
                with open(bazel_build, "r") as f:
                    content = f.read()
                lines = [l for l in content.split('\n') if '"protected_exports_list"' not in l or 'android/abi_gki_protected_exports_aarch64' not in l]
                with open(bazel_build, "w") as f:
                    f.write('\n'.join(lines))

            abi_path = self.work_dir / "common/android/abi_gki_protected_exports_aarch64"
            if abi_path.exists():
                import shutil
                try:
                    if abi_path.is_dir():
                        shutil.rmtree(abi_path)
                    else:
                        abi_path.unlink()
                except Exception:
                    pass

            stamp_bzl = self.work_dir / "build/kernel/kleaf/impl/stamp.bzl"
            if stamp_bzl.exists():
                with open(stamp_bzl, "r") as f:
                    content = f.read()
                content = content.replace("-maybe-dirty", "")
                with open(stamp_bzl, "w") as f:
                    f.write(content)

            if self.config.custom_version:
                config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
                if config_file.exists():
                    with open(config_file, "r") as f:
                        content = f.read()
                    content = re.sub(r'^CONFIG_LOCALVERSION=".*"$', f'CONFIG_LOCALVERSION="{self.config.custom_version}"', content, flags=re.MULTILINE)
                    with open(config_file, "w") as f:
                        f.write(content)
                else:
                    logger.warning(f"配置文件不存在，跳过 custom_version 设置: {config_file}")

    def show_kernel_config(self):
        logger.info("=== 显示内核配置列表 ===")
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        
        if not config_file.exists():
            logger.warning(f"配置文件不存在: {config_file}")
            return
        
        with open(config_file, "r") as f:
            lines = f.readlines()
        
        config_lines = [line.strip() for line in lines if line.strip().startswith("CONFIG_")]
        
        key_configs = {
            "CONFIG_KSU": "KernelSU",
            "CONFIG_KPM": "KPM",
            "CONFIG_KSU_SUSFS": "SUSFS",
            "CONFIG_BBG": "Baseband-guard",
            "CONFIG_BBR": "BBR",
            "CONFIG_ZRAM": "ZRAM",
        }
        
        logger.info("关键配置状态:")
        for prefix, name in key_configs.items():
            found = [c for c in config_lines if c.startswith(prefix)]
            if found:
                status = "已启用"
            else:
                status = "未配置"
            logger.info(f"  [{status}] {name}")
            if found:
                for f in sorted(found):
                    logger.info(f"      -> {f}")
        
        # 显示 ZRAM 相关配置
        if self.config.use_zram:
            zram_configs = [c for c in config_lines if any(x in c for x in ["ZRAM", "ZSMALLOC", "LZ4", "LZ4KD", "CRYPTO_LZ4", "MODULE_SIG"])]
            if zram_configs:
                logger.info("ZRAM 相关配置:")
                for zc in sorted(zram_configs):
                    logger.info(f"  -> {zc}")
        
        logger.info("-" * 60)

    def build_kernel(self) -> bool:
        logger.info("=== 开始编译内核 ===")
        self._chdir(self.work_dir)

        build_config = self.work_dir / "common/build.config.gki.aarch64"
        if build_config.exists():
            with open(build_config, "r") as f:
                content = f.read()
            content = content.replace("BUILD_SYSTEM_DLKM=1", "BUILD_SYSTEM_DLKM=0")
            lines = [l for l in content.split('\n') if 'MODULES_ORDER=android/gki_aarch64_modules' not in l and 'KMI_SYMBOL_LIST_STRICT_MODE' not in l]
            with open(build_config, "w") as f:
                f.write('\n'.join(lines))

        try:
            if (self.work_dir / "build/build.sh").exists():
                logger.info("使用旧版构建方式...")
                cmd = "LTO=thin BUILD_CONFIG=common/build.config.gki.aarch64 build/build.sh CC=\"/usr/bin/ccache clang\""
            else:
                logger.info("使用 Bazel 构建方式...")
                cmd = ("tools/bazel build --disk_cache=$HOME/.cache/bazel --config=fast --lto=thin //common:kernel_aarch64_dist")

            # 原实现用 _run_cmd(check=False) 且不 capture_output，失败时
            # result.stderr 恒为 None -> 永远打印 "Unknown error"，真正的
            # `error:` / `undefined reference` 被埋在上千行日志里看不到。
            # 改为边跑边打印，并收集所有含 error/fatal/undefined 的行。
            errors = []

            def _on_line(line):
                # 全部实时打印（Actions 日志能吃下），同时把错误行单独收集。
                # 注意：不要用宽泛的 "not found"——内核 Makefile 的 $(info)
                # 里常有 "xxx not found, feature disabled" 这类无害提示，
                # 会被误收进错误列表干扰判断。
                logger.info(line)
                if re.search(r'(?i)\berror\b|fatal error|undefined reference|'
                             r'undeclared|No such file or directory|Error \d+', line):
                    errors.append(line)

            try:
                # run_with_callback 成功返回输出字符串，失败抛 RuntimeError
                self.shell.run_with_callback(cmd + " 2>&1", callback=_on_line)
            except RuntimeError:
                logger.error("=== 内核编译失败，收集到的错误行 ===")
                for line in errors[-40:]:
                    logger.error(line)
                if not errors:
                    logger.error("未捕获到显式 error 行，请查看上方完整构建日志")
                # 编译跑过 make defconfig，此刻 .config 已生成；把关键项
                # 打出来，直接判断 append 到 gki_defconfig 的配置是否生效，
                # 避免下一轮再靠猜。
                logger.error("=== 最终 .config 关键项（诊断） ===")
                self._dump_final_config()
                return False
            logger.info("=== 内核编译成功 ===")
            return True
        except Exception as e:
            logger.error(f"编译过程出错: {e}")
            return False

    def _kernel_dist_dir(self) -> Path:
        """定位内核 Image 产物目录。

        不再按 android_version 硬编码：build_kernel() 用 build/build.sh 时产物在
        out/<android>-<kernel>/dist，用 Bazel 时在 bazel-bin/common/kernel_aarch64。
        旧实现把 android14/15 一律指向 bazel 路径，与 build_kernel() 的实际分支
        不一致，会导致 patch_kpm_image / prepare_boot_images 找不到 Image。
        这里按实际存在的 Image 探测，兼容两种布局。
        """
        candidates = [
            self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist",
            self.work_dir / "bazel-bin/common/kernel_aarch64",
            self.work_dir / "out" / "dist",
        ]
        for d in candidates:
            if (d / "Image").exists():
                logger.info(f"内核产物目录: {d}")
                return d
        for d in self.work_dir.rglob("Image"):
            if d.is_file() and d.stat().st_size > 1024 * 1024:
                logger.warning(f"内核产物目录由宽搜得到: {d.parent}")
                return d.parent
        raise RuntimeError(
            "找不到内核 Image 产物；build_kernel() 可能未成功产出，"
            "或产物路径与已知布局都不同")

    def patch_kpm_image(self):
        """给 kernel Image 注入 KernelPatch (kpimg)，用官方 kptools + 配对 kpimg。

        2026-11-10 实测根因与修法（设备 logo 阶段无限重启）：
          旧实现下载裸 patch_linux 后无参数执行 ./patch。该二进制内嵌的 kpimg
          资源是坏的（1 字节 -> 补成 16 字节 `aa 00...`），却仍照常改写了
          ARM64 Image 头：把 stext 的 b 分支指向 0x262e000（**越出文件 3952 字节**）、
          把 image_size 头改成 0x262f000、把 tcp_init_sock 区域 NOP 掉 0x2000 字节。
          内核第一条分支就跳到文件外 -> logo 无限重启，而日志一路 [+] 全绿。

          官方 SukiSU_KernelPatch_patch 的正确用法是 kptools 显式带 kpimg：
            kptools-linux -p -i Image -k kpimg -s <superkey> -o out
          本次用 0.14.0 release 的配对资产，产出 version=0xe00 /
          compile_time=19:40:23 Oct 26 2026 / config=android,release，
          实机验证：正常开机且 sukisu 工具识别到 KPM（2026-11-10）。

        硬校验（防止再静默产出坏镜像）：
          1) kptools / kpimg 都从同一 release 下载并做 sha256 校验；
          2) 打补丁后用 kptools -l 读回元信息，version 必须 != 0 且 compile_time 非空；
          3) **解码 ARM64 Image 头的 b 指令**，确认跳转目标落在文件内；
          4) 确认输出比输入大（真的写进了 kpimg，不是只改了头）。
        """
        if not self.config.use_kpm:
            self.manifest.update(kpm_patched=False, kpm_skip_reason="use_kpm=false")
            return
        if self.config.kernel_version == "6.6":
            self.manifest.update(kpm_patched=False,
                                 kpm_skip_reason="6.6 KPM patch skipped (known stability issues)")
            logger.warning("=== 跳过 KPM 补丁 (6.6 已知稳定性问题) ===")
            return

        logger.info("=== 修补 Image 文件 (KPM / 官方 kptools) ===")
        image_dir = self._kernel_dist_dir()

        self._chdir(image_dir)
        image = image_dir / "Image"
        if not image.exists():
            raise RuntimeError(f"KPM: 找不到 {image}")

        kptools = image_dir / "kptools-linux"
        kpimg = image_dir / "kpimg"

        # --- 0) 前置校验（借鉴 MikSU kpm_image.rs 的自检链） ---
        # 基线必须是未打补丁的干净内核，否则重复注入会产出坏镜像。
        base = image.read_bytes()
        if len(base) < 64 or base[56:60] != b"ARMd":
            raise RuntimeError(f"KPM 自检: {image.name} 不是有效的 ARM64 Image")
        b0 = int.from_bytes(base[4:8], "little")
        if (b0 & 0xFC000000) != 0x14000000:
            raise RuntimeError(
                f"KPM 自检: 基线 Image @0x04 不是 b 指令 (0x{b0:08x})，"
                "可能已被打过补丁，拒绝重复注入")
        if b"KP1158" in base:
            raise RuntimeError("KPM 自检: 基线 Image 已含 KP1158 magic，拒绝重复注入")
        self.manifest["kpm_baseline_size"] = len(base)

        # --- 1) 下载官方配对资产并校验 ---
        for url, path, expect in ((KPM_KPTOOLS_URL, kptools, KPM_KPTOOLS_SHA256),
                                  (KPM_KPIMG_URL, kpimg, KPM_KPIMG_SHA256)):
            self._run_cmd(f"curl -fL --retry 3 -Ss {url} -o {path.name}", check=True)
            if not path.exists() or path.stat().st_size < 100 * 1024:
                raise RuntimeError(f"KPM: {path.name} 下载失败或异常小")
            digest = _sha256_file(path)
            logger.info(f"KPM {path.name} sha256: {digest}")
            if expect and digest.lower() != expect.lower():
                raise RuntimeError(f"KPM: {path.name} sha256 不匹配: 期望 {expect} 实际 {digest}")
        self._run_cmd(f"chmod 755 {kptools.name}", check=True)
        self.manifest.update(kpm_tools_version=KPM_TOOLS_VERSION,
                             kpm_kptools_sha256=_sha256_file(kptools),
                             kpm_kpimg_sha256=_sha256_file(kpimg),
                             kpm_patch_upstream=KPM_PATCH_UPSTREAM)
        # 官方 tag 0.14.0 树内 version 其实是 0.13.9（子代理调研确认），
        # 因此版本配对必须用 kptools 实读值，不能按 release tag 名断言。
        for label, args in (("kpm_kptools_version", f"./{kptools.name} -v"),
                            ("kpm_kpimg_version_reported",
                             f"./{kptools.name} -v -k {kpimg.name}")):
            rv = self._run_cmd(args, check=False, capture_output=True)
            val = ((rv.stdout or "") + (rv.stderr or "")).strip().splitlines()
            v = val[-1] if val else ""
            self.manifest[label] = v
            logger.info(f"KPM {label}: {v}")

        # --- 2) 用官方命令打补丁 ---
        orig_size = image.stat().st_size
        patched = image_dir / "Image.kpm"
        superkey = os.environ.get("KPM_SUPERKEY") or KPM_SUPERKEY
        self._run_cmd(
            f"./{kptools.name} -p -i {image.name} -k {kpimg.name} "
            f"-s {superkey} -o {patched.name}", check=True)
        if not patched.exists() or patched.stat().st_size <= orig_size:
            raise RuntimeError("KPM: 打补丁后输出异常（未变大或缺失），中止")
        logger.info(f"KPM 补丁输出 {orig_size} -> {patched.stat().st_size} bytes")

        # --- 3) 读回 kpimg 元信息 ---
        info = self._run_cmd(f"./{kptools.name} -l -i {patched.name}",
                             check=True, capture_output=True)
        text = (info.stdout or "") + (info.stderr or "")
        ver = next((l.split("=", 1)[1] for l in text.splitlines()
                    if l.strip().startswith("version=")), "")
        ctime = next((l.split("=", 1)[1] for l in text.splitlines()
                      if l.strip().startswith("compile_time=")), "")
        patched_flag = "patched=true" in text
        logger.info(f"KPM kpimg: patched={patched_flag} version={ver} compile_time={ctime}")
        if not patched_flag or not ver or ver in ("0", "0x0") or not ctime.strip():
            raise RuntimeError(
                f"KPM: kpimg 元信息异常（version={ver!r}, compile_time={ctime!r}）—— "
                "很可能注入了损坏/空壳 kpimg，中止以免产出无法启动的内核")
        self.manifest.update(kpm_kpimg_version=ver, kpm_kpimg_compile_time=ctime)

        # --- 4) 解码 ARM64 Image 头，确认 b 分支跳转目标在文件内 ---
        self._verify_arm64_entry(patched)

        # --- 5) 原子替换 ---
        self._run_cmd(f"mv -f {patched.name} {image.name}", check=True)
        self.manifest["kpm_patched"] = True
        logger.info("=== KPM 补丁已应用并通过全部校验 ===")

    @staticmethod
    def _verify_arm64_entry(path: Path):
        """解码 ARM64 Image 头 @0x04 的 b 指令，确认跳转目标落在文件内。

        这是 2026-11-10 那次 logo 无限重启的直接致命点：
        坏镜像把 b 目标改到 0x262e000，而文件只到 0x262d090（越界 3952 字节）。
        """
        import struct
        d = path.read_bytes()
        if len(d) < 64:
            raise RuntimeError(f"KPM 自检: {path.name} 太短，不是有效内核镜像")
        if d[56:60] != b"ARMd":
            raise RuntimeError(f"KPM 自检: {path.name} 缺少 ARM64 magic (0x38)")
        code1 = struct.unpack_from("<I", d, 4)[0]
        # ARM64 b <imm26>: opcode 0x14000000, target = PC(0x04) + sext(imm26)<<2
        if (code1 & 0xFC000000) != 0x14000000:
            # 不是 b 指令就不做跳转校验（有些镜像头布局不同），只记录
            logger.warning(f"KPM 自检: @0x04 不是 b 指令 (0x{code1:08x})，跳过跳转校验")
            return
        imm = code1 & 0x03FFFFFF
        if imm & 0x02000000:
            imm -= 0x04000000
        target = 0x04 + (imm << 2)
        in_file = target < len(d)
        nonzero = sum(1 for x in d[target:target + 4096] if x) if in_file else 0
        logger.info(f"KPM 自检: b->0x{target:x}, file=0x{len(d):x}, "
                    f"in_file={in_file}, code@target={nonzero} non-zero bytes")
        if not in_file:
            raise RuntimeError(
                f"KPM 自检失败: ARM64 入口 b 指令跳到 0x{target:x}，"
                f"但镜像只有 0x{len(d):x} 字节（越界 {target - len(d)} 字节）—— "
                "这种镜像刷入必然 logo 无限重启，构建中止")
        if nonzero < 64:
            raise RuntimeError(
                f"KPM 自检失败: b 目标 0x{target:x} 在文件内但几乎是空的"
                f"（前 4KB 仅 {nonzero} 字节非零），kpimg 可能未写入，构建中止")

    def prepare_boot_images(self) -> list:
        logger.info("=== 准备启动镜像 ===")
        self._chdir(self.work_dir)
        bootimgs_dir = self.work_dir / "bootimgs"
        bootimgs_dir.mkdir(exist_ok=True)
        artifacts = []

        image_source = self._kernel_dist_dir()

        for image_name in ["Image", "Image.lz4"]:
            src = image_source / image_name
            if src.exists():
                self._run_cmd(f"cp {src} {bootimgs_dir}/ && cp {src} {self.work_dir}/", check=False)

        # 2026-11-09：patch_kpm_image() 会把 dist/Image 换成打过 KPM 补丁的版本
        # （`mv oImage Image`）。上面的 cp 必须发生在它之后，boot 镜像里装的
        # 才是带 kpimg 的内核，否则 KPM 运行时永远返回 -EOPNOTSUPP。
        # 这里再显式同步一次，并记录实际大小便于事后核对。
        patched = image_source / "Image"
        if patched.exists():
            self._run_cmd(f"cp -f {patched} {bootimgs_dir}/Image && cp -f {patched} {self.work_dir}/Image", check=False)
            self.manifest["boot_image_kernel_size"] = patched.stat().st_size
            logger.info(f"boot 镜像内核大小 = {patched.stat().st_size} bytes")

        if (self.work_dir / "Image").exists():
            self._run_cmd("gzip -n -k -f -9 Image", check=False)

        # 2026-11-10 产物规则：5.10 / 5.15 产 boot*.img + AnyKernel3.zip；
        # 6.1 / 6.6 只产 AnyKernel3.zip（见 BuildConfig.produces_boot_img 注释）。
        if self.config.produces_boot_img():
            self._prepare_boot_images_with_template(bootimgs_dir, artifacts)
        else:
            logger.info(
                f"{self.config.android_version}-{self.config.kernel_version}: "
                "按规则只产 AnyKernel3.zip，跳过 boot*.img（通用 mkbootimg 打包器"
                "无 ramdisk / 硬编码 os_version 12 / 无 VBMETA，产出残缺镜像）")
            self.manifest["boot_img_policy"] = "anykernel3-zip-only"
        return artifacts

    def _prepare_boot_images_with_template(self, bootimgs_dir: Path, artifacts: list):
        """用 Google certified boot 镜像作模板 + magiskboot 打包 boot*.img。

        原实现把 URL 硬编码成 android12-5.10；现按
        `gki-certified-boot-<android>-<kernel>-<os_patch>_<revision>.zip` 拼，
        5.10 与 5.15 都实测 200。revision 缺省时回退 r1。
        """
        self._chdir(bootimgs_dir)
        av = self.config.android_version
        kv = self.config.kernel_version
        pat = self.config.os_patch_level
        rev = (self.config.revision or "").strip() or "r1"
        gki_url = (f"https://dl.google.com/android/gki/"
                   f"gki-certified-boot-{av}-{kv}-{pat}_{rev}.zip")
        fallback_url = (f"https://dl.google.com/android/gki/"
                        f"gki-certified-boot-{av}-{kv}-2023-01_r1.zip")
        result = subprocess.run(
            f"curl -sL -w '%{{http_code}}' {gki_url} -o /dev/null",
            shell=True, capture_output=True, text=True)
        url = gki_url if "200" in result.stdout else fallback_url
        if url != gki_url:
            logger.warning(f"certified boot 主 URL 不可用，回退到 {fallback_url}")
        # 2026-11-10：加超时与显式校验。旧实现 check=False 且无 --max-time，
        # 网络挂起时会静默卡住数分钟，且下载到 0 字节也被吞掉。
        self._run_cmd(
            f"curl -fL --retry 3 --connect-timeout 30 --max-time 300 -o gki-kernel.zip {url}",
            check=True)
        zip_path = bootimgs_dir / "gki-kernel.zip"
        if not zip_path.exists() or zip_path.stat().st_size < 1024 * 1024:
            raise RuntimeError(f"Google certified boot zip 下载失败或异常小: {zip_path}")
        self._run_cmd("unzip -o gki-kernel.zip && rm gki-kernel.zip", check=True)
        # 模板镜像按内核版本命名（boot-5.10.img / boot-5.15.img / boot-5.10-lts.img ...）。
        # 只用 `boot-<内核版本>*.img` 匹配，避免把上一次构建残留在 bootimgs_dir
        # 里的 `*-boot-gz.img` 之类产物误当成模板。
        pattern = f"boot-{kv}*.img"
        candidates = [p for p in bootimgs_dir.glob(pattern)
                      if "boot-gz" not in p.name and "boot-lz4" not in p.name]
        # 精确名（boot-5.10.img / boot-5.15.img）优先，避免同名 lts 变体
        # （boot-5.10-lts.img）在字典序里排到前面被误选。
        exact = bootimgs_dir / f"boot-{kv}.img"
        if exact in candidates and exact.stat().st_size > 1024 * 1024:
            boot_img_path = exact
        else:
            boot_img_path = next(
                (p for p in sorted(candidates) if p.stat().st_size > 1024 * 1024), None)
        if boot_img_path is None:
            # 兜底：有些包里就叫 boot.img（不带版本号）
            fallback = bootimgs_dir / "boot.img"
            if fallback.exists() and fallback.stat().st_size > 1024 * 1024:
                boot_img_path = fallback
        if boot_img_path is None:
            raise RuntimeError(
                f"未在 certified boot zip 里找到有效模板镜像"
                f"（匹配 {pattern}，候选: {[p.name for p in bootimgs_dir.glob('boot*.img')]}），"
                "中止以免产出无法启动的 boot 镜像")
        logger.info(f"certified boot 模板: {boot_img_path.name}")
        # 2026-11-09：改用 magiskboot 打包路径。
        # 原先用 mkbootimg --header_version 4 生成，它在没有 --gki_signing_key 时
        # 不写 boot signature 槽（boot_signature_size=0），magiskboot 解析时
        # 也看不到 VBMETA 块 -> 设备 bootloader 拒绝 -> 无限重启。
        # magiskboot 以这份 certified boot 镜像为模板 unpack/repack，
        # header / ramdisk / boot_signature / os_patch_level 全部沿用原厂。
        if self._build_boot_images_with_magiskboot(bootimgs_dir, artifacts, boot_img_path):
            return
        logger.warning("magiskboot 不可用，回退到 mkbootimg（可能无法启动）")
        self._run_cmd(f"$UNPACK_BOOTIMG --boot_img={boot_img_path}", check=False)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=True)

    def _setup_magiskboot(self) -> bool:
        """下载并解出 magiskboot。返回是否可用。

        2026-11-09 实测：boot image v4 的正确打包器。
        AOSP mkbootimg 在没有 --gki_signing_key 时不写 boot signature 槽，
        而设备 bootloader 需要它 -> 无限重启。magiskboot 保留该槽。
        """
        import platform
        arch = platform.machine()
        lib = MAGISKBOOT_LIB.get("x86_64" if arch in ("x86_64", "amd64") else "aarch64")
        if not lib:
            logger.warning(f"magiskboot: 不支持的架构 {arch}")
            return False
        tool = self.work_dir / "magiskboot"
        if tool.exists() and tool.stat().st_size > 1024:
            self.env["MAGISKBOOT"] = str(tool)
            return True
        apk = self.work_dir / "magisk.apk"
        self._run_cmd(f"curl -fL --retry 3 -Ss {MAGISKBOOT_URL} -o {apk}", check=False)
        if not apk.exists() or apk.stat().st_size < 1024 * 1024:
            logger.warning("magiskboot: Magisk APK 下载失败")
            return False
        if MAGISKBOOT_APK_SHA256:
            digest = _sha256_file(apk)
            if digest.lower() != MAGISKBOOT_APK_SHA256.lower():
                raise RuntimeError(
                    f"Magisk APK sha256 不匹配: 期望 {MAGISKBOOT_APK_SHA256} 实际 {digest}")
            logger.info(f"Magisk APK sha256 校验通过 ({digest[:16]}...)")
        self._run_cmd(f"unzip -o -j {apk} '{lib}' -d {self.work_dir}", check=False)
        extracted = self.work_dir / "libmagiskboot.so"
        # 注意：不同 Magisk 版本里 magiskboot 的大小差别很大
        # （实测 v30.7 的 x86_64 版只有 943,848 字节，v28 的 1.38 MB），
        # 用 1 MB 当阈值会误判「解出失败」。这里只要求它是个像样的二进制。
        if not extracted.exists() or extracted.stat().st_size < 100 * 1024:
            logger.warning(f"magiskboot: 无法从 APK 解出 {lib}")
            return False
        self._run_cmd(f"cp {extracted} {tool} && chmod 755 {tool}", check=False)
        # 冒烟测试：能打印用法说明才算可用
        r = self._run_cmd(f"{tool} 2>&1 | head -3", check=False, capture_output=True)
        probe = (r.stdout or "") + (r.stderr or "")
        if "MagiskBoot" not in probe:
            logger.warning(f"magiskboot: 冒烟测试失败（{probe.strip()[:120]}）")
            return False
        self.env["MAGISKBOOT"] = str(tool)
        logger.info(f"magiskboot 就绪: {tool}")
        return True

    def _build_boot_images_with_magiskboot(self, bootimgs_dir: Path, artifacts: list,
                                            stock_boot: Path):
        """用 magiskboot 以「Google 官方 certified boot 镜像」为模板打包。

        2026-11-09 实测根因与修法：
          原实现用 AOSP mkbootimg --header_version 4 生成 boot.img，
          它在没有 --gki_signing_key 时 **不写 boot signature 槽**
          （boot_signature_size = 0），magiskboot 解析时也看不到 VBMETA 块。
          设备 bootloader 校验失败 -> 「能开机但立刻重启」。
          实测 magiskboot 对 Google 官方 certified boot 镜像原样 repack 是
          byte-identical（md5 相同），说明它完整保留 header 语义；
          而 AnyKernel3 之所以能开机，正是因为它在设备上用 magiskboot
          重打包了设备自己的 boot.img。

          本实现：把 Google 的 boot-5.10.img 当模板，用 magiskboot
          unpack 出组件，再把 kernel 换成我们的（可选 gzip / raw），
          然后 repack。header / ramdisk / boot_signature / os_patch_level
          全部沿用原厂，等价于 AnyKernel3 的效果，但产物在 PC 上就能生成。
        """
        if not self._setup_magiskboot():
            return False
        work = self.work_dir / "mb_pack"
        if work.exists():
            self._run_cmd(f"rm -rf {work}", check=False)
        work.mkdir(parents=True, exist_ok=True)

        r = self._run_cmd(f"cd {work} && cp {stock_boot} stock.img && "
                          f"$MAGISKBOOT unpack -h stock.img", check=False, capture_output=True)
        mb_out = (r.stdout or "") + (r.stderr or "")
        if "VBMETA" not in mb_out:
            raise RuntimeError(
                "magiskboot unpack 模板后没有 VBMETA 块：模板不是有效的 boot image v4，"
                "中止以免产出同样会无限重启的镜像")
        if not (work / "kernel").exists():
            raise RuntimeError("magiskboot unpack 未产出 kernel 组件")

        src_image = self.work_dir / "Image"
        if not src_image.exists():
            raise RuntimeError(f"缺少 {src_image}，无法打包 boot 镜像")
        logger.info(f"使用内核 {src_image} ({src_image.stat().st_size} bytes)")

        variants = []
        gz = self.work_dir / "Image.gz"
        if gz.exists():
            variants.append(("boot-gz.img", gz))
        variants.append(("boot.img", src_image))

        for output_file, kernel_path in variants:
            self._run_cmd(f"cp {kernel_path} {work}/kernel", check=False)
            self._run_cmd(f"cd {work} && rm -f out.img && $MAGISKBOOT repack stock.img out.img",
                          check=True)
            produced = work / "out.img"
            if not produced.exists() or produced.stat().st_size < 1024 * 1024:
                raise RuntimeError(f"magiskboot repack 未产出有效镜像: {output_file}")
            dest = self.work_dir / (f"{self.config.android_version}-{self.config.kernel_version}."
                                    f"{self.config.sub_level}-{self.config.os_patch_level}-{output_file}")
            self._run_cmd(f"cp {produced} {dest}", check=True)
            artifacts.append(str(dest))
            logger.info(f"已生成 {dest.name}（magiskboot 打包，保留 boot signature / VBMETA）")

        for a in artifacts:
            if not a.endswith(".img"):
                continue
            r = self._run_cmd(f"$MAGISKBOOT unpack -h {a}", check=False, capture_output=True)
            mb_out = (r.stdout or "") + (r.stderr or "")
            if "VBMETA" not in mb_out:
                raise RuntimeError(
                    f"自检失败：{Path(a).name} 缺少 VBMETA/boot signature 块，"
                    "该镜像会被设备 bootloader 拒绝 -> 无限重启")
        self.manifest["boot_image_packer"] = "magiskboot"
        self.manifest["boot_image_vbmeta_verified"] = True
        logger.info("=== boot 镜像自检通过（VBMETA 块存在）===")
        return True

    def _create_boot_image_variants(self, bootimgs_dir: Path, artifacts: list, has_ramdisk: bool = False):
        self._chdir(bootimgs_dir)
        if (bootimgs_dir / "Image").exists():
            self._run_cmd("gzip -n -k -f -9 Image", check=False)

        for kernel_file, output_file in [("Image", "boot.img"), ("Image.gz", "boot-gz.img"), ("Image.lz4", "boot-lz4.img")]:
            kernel_path = bootimgs_dir / kernel_file
            if not kernel_path.exists():
                continue
            cmd = f"$MKBOOTIMG --header_version 4 --kernel {kernel_file} --output {output_file}"
            if has_ramdisk:
                cmd += f" --ramdisk out/ramdisk --os_version 12.0.0 --os_patch_level {self.config.os_patch_level}"
            self._run_cmd(cmd, check=False)
            # 只有提供了设备私钥才做 AVB 签名。
            # 原实现无条件调用，`$BOOT_SIGN_KEY_PATH` 为空时命令变成
            # "--key "（空参数），avbtool 报
            #   "add_hash_footer: error: argument --key: expected one argument"
            # 并在日志里刷三行 usage 噪音。check=False 掩盖了失败，镜像本身
            # 仍会产出（设备已禁用校验时本就靠 bootloader 放行），但这是
            # 真实的错误输出，必须在无密钥时干脆不调用。
            sign_key = os.environ.get("BOOT_SIGN_KEY_PATH", "").strip()
            if sign_key:
                self._run_cmd(
                    f"$AVBTOOL add_hash_footer --partition_name boot "
                    f"--partition_size $((64 * 1024 * 1024)) --image {output_file} "
                    f"--algorithm SHA256_RSA2048 --key {sign_key}", check=False)
            else:
                logger.info(f"未提供 BOOT_SIGN_KEY_PATH，跳过 {output_file} 的 AVB 哈希页签名")
                self.manifest.setdefault("avb_signed", []).append(f"{output_file}:skipped-no-key")
            dest = self.work_dir / f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-{output_file}"
            self._run_cmd(f"cp {output_file} {dest}", check=False)
            artifacts.append(str(dest))

    def create_anykernel_zips(self) -> list:
        logger.info("=== 创建 AnyKernel3 ZIP 文件 ===")
        self._chdir(self.work_dir)
        artifacts = []
        ak3_dir = self.anykernel_dir

        # 实际文件是 Image.lz4 / Image.gz（点号）；原连字符写法让 lz4/gz 包成死分支
        #
        # 2026-11-09：每个 zip 只放**一种**内核，不要同时放 Image 和 Image.gz。
        # AnyKernel3 的 flash_boot() 按固定顺序找内核：
        #     for i in zImage zImage-dtb Image Image-dtb Image.gz Image.gz-dtb ...
        # `Image` 排在 `Image.gz` 前面，所以一旦两个都在，AnyKernel3 永远选
        # 裸 Image —— 「-gz 包用 gzip 内核」这个预期不成立。
        # 分成三个 zip 时每个包只有一个内核文件，选择顺序不再有歧义。
        for suffix, file_suffix in [("", ""), ("-lz4", ".lz4"), ("-gz", ".gz")]:
            image_file = f"Image{file_suffix}"
            image_path = self.work_dir / image_file
            if not image_path.exists():
                continue
            zip_name = f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-AnyKernel3{suffix}.zip"
            self._run_cmd(f"cp {image_path} {ak3_dir}/", check=False)
            self._chdir(ak3_dir)
            # 2026-11-09 实测修正：原来用 `zip -r ../{zip_name} ./*`，
            # 但当前目录是 workspace 下的 AnyKernel3/，`..` 指向 **workspace 根**，
            # 而不是 work_dir。结果 zip 落在 workspace 根目录，而下面
            # artifacts.append() 记录的是 work_dir 下的路径 —— 两者对不上，
            # manifest 与 build_result.json 里的路径是错的，
            # 收产物阶段也会漏掉这些 zip。
            # 改为显式绝对路径，保证 zip 与 manifest 记录一致。
            zip_path = self.work_dir / zip_name
            self._run_cmd(f"zip -r {zip_path} ./*", check=False)
            self._run_cmd(f"rm {ak3_dir}/{image_file}", check=False)
            artifacts.append(str(zip_path))
            self._chdir(self.work_dir)
        return artifacts

    def build(self) -> BuildResult:
        import time
        start_time = time.time()
        logger.info("=" * 50)
        logger.info(f"开始 GKI Kernel 构建 - {self.config.config_name}")
        logger.info("=" * 50)

        try:
            self.clone_repositories()
            self.clone_toolchain()
            self.setup_repo_tool()
            self.init_and_sync_kernel()
            self.add_kernel_supatch()
            self.add_kernelsu()
            self.add_bbg()
            self.apply_susfs_patches()
            self.apply_sukisu_patches()
            self.apply_zram_patches()
            self.apply_task_mmu_fixes()
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()
            # 配置层追加对该 GKI fragment 拼装无效，直接在源码层补齐：
            #   assoc_array.h -> struct assoc_array 无条件完整（asm-offsets）
            #   vdso/Makefile -> 无条件 -include（vdso_prepare）
            self._fix_assoc_array_header()
            self._fix_vdso_gettimeofday_include()
            # builtin 分支无条件调用 susfs 的 priority_uid_filter 函数，
            # 但上游没给它 Kconfig 定义 -> 补上，否则编译器报
            # implicit declaration（详见 _ensure_susfs_priority_uid_filter_kconfig）。
            self._ensure_susfs_priority_uid_filter_kconfig()
            # KSU 侧的调用点也要用同一个 CONFIG 门控，否则 CONFIG=n 时会
            # 因为声明不可见而编译失败（见 _guard_ksu_priority_uid_filter_calls）。
            self._guard_ksu_priority_uid_filter_calls()
            # susfs.c 里两个 credential helper 的定义在 #ifdef 块外，
            # CONFIG 关闭时变成死代码 -> -Werror=unused-function 编译失败。
            self._guard_susfs_credential_helpers()

            if not self.build_kernel():
                return BuildResult(success=False, config=self.config, message="内核编译失败", build_time=time.time() - start_time)

            self.patch_kpm_image()
            artifacts = []
            artifacts.extend(self.prepare_boot_images())
            artifacts.extend(self.create_anykernel_zips())

            self._write_manifest(artifacts)
            self._verify_artifacts(artifacts)

            build_time = time.time() - start_time
            logger.info(f"构建成功! 耗时: {build_time:.2f} 秒, 生成 {len(artifacts)} 个产物")
            return BuildResult(success=True, config=self.config, message="构建成功", artifacts=artifacts, build_time=build_time)
        except Exception as e:
            logger.error(f"构建过程出错: {e}")
            return BuildResult(success=False, config=self.config, message=str(e), build_time=time.time() - start_time)


    def _write_manifest(self, artifacts: list):
        """BUILD_MANIFEST.txt：内核/manager/SUSFS/KPM 各依赖的真实 commit。"""
        if self.config.produces_boot_img():
            self.manifest["recommended_variant"] = RECOMMENDED_BOOT_VARIANT
            self.manifest["disabled_variants"] = list(DISABLED_BOOT_VARIANTS)
        else:
            self.manifest["recommended_variant"] = "AnyKernel3.zip"
            self.manifest["disabled_variants"] = ["boot*.img（本家族按规则不产出）"]
        self.manifest["artifacts"] = [Path(a).name for a in artifacts]
        out = self.work_dir / "BUILD_MANIFEST.txt"
        with open(out, "w", encoding="utf-8") as f:
            f.write("# GKI 构建依赖版本指纹\n")
            f.write("# 安装 manager 时请使用与本内核同源 (同 commit) 的官方 APK\n\n")
            f.write(json.dumps(self.manifest, indent=2, ensure_ascii=False, sort_keys=True))
            f.write("\n")
        logger.info(f"已写出 {out}")
        artifacts.append(str(out))

    def _verify_artifacts(self, artifacts: list):
        """宁可构建失败，也不产出「看起来成功」的残缺镜像。"""
        zips = [Path(a) for a in artifacts if a.endswith(".zip")]
        for z in zips:
            if not z.exists() or z.stat().st_size < 1024:
                raise RuntimeError(
                    f"自检失败：{z.name} 缺失或异常小 —— 生成 zip 的命令失败过"
                    "（检查环境是否有 zip 命令），却仍被写进产物清单")
        if not zips:
            raise RuntimeError("自检失败：没有产出任何 AnyKernel3 zip")

        # 2026-11-10：6.1 / 6.6 按规则只产 AnyKernel3.zip，不校验 boot*.img。
        boots = [Path(a) for a in artifacts if a.endswith(".img") and "boot" in Path(a).name]
        if self.config.produces_boot_img():
            if not boots:
                raise RuntimeError("自检失败：本家族应产 boot*.img，但一个都没有")
            for img in boots:
                if not img.exists() or img.stat().st_size < 1024 * 1024:
                    raise RuntimeError(f"自检失败：{img.name} 缺失或异常小")
            if not [i for i in boots if i.name.endswith("-boot-gz.img")]:
                raise RuntimeError("自检失败：缺少 boot-gz.img（本机唯一可启动压缩格式）")
            avbtool = self.env.get("AVBTOOL", "")
            if avbtool and Path(avbtool).exists() and self.env.get("BOOT_SIGN_KEY_PATH"):
                for img in boots:
                    if self._run_cmd(f"{avbtool} info_image --image {img}", check=False).returncode != 0:
                        logger.warning(f"AVB footer 校验未通过: {img.name}（设备已禁用校验时可忽略）")
        else:
            if boots:
                raise RuntimeError(
                    f"自检失败：{self.config.android_version}-{self.config.kernel_version} "
                    f"按规则只应产 AnyKernel3.zip，却出现了 boot*.img: "
                    f"{[b.name for b in boots]}")

        image_gz = self.work_dir / "Image.gz"
        if image_gz.exists() and self._run_cmd(f"gzip -t {image_gz}", check=False).returncode != 0:
            raise RuntimeError("自检失败：Image.gz 不是有效 gzip 文件")
        if self.config.use_kpm and self.config.kernel_version != "6.6" and not self.manifest.get("kpm_patched"):
            raise RuntimeError("自检失败：启用 KPM 但补丁未标记成功")
        logger.info(f"=== 产物自检通过：{len(boots)} 个 boot 镜像 / {len(zips)} 个 zip ===")

