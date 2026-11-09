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
                   KPM_PATCH_UPSTREAM, KSU_MIN_UAPI_VERSION, RECOMMENDED_BOOT_VARIANT,
                   DISABLED_BOOT_VARIANTS, SUSFS_VERSION_LABEL, KSU_PIN,
                   SUSFS_PIN, SUKISU_PATCH_PIN)

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
        if not repo_path.exists():
            self._run_cmd(f"curl https://storage.googleapis.com/git-repo-downloads/repo > {repo_path}", check=False)
            self._run_cmd(f"chmod a+rx {repo_path}", check=False)
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
            with open(manifest_path, "r") as f:
                content = f.read()
            content = content.replace(f'"{formatted_branch}"', f'"deprecated/{formatted_branch}"')
            with open(manifest_path, "w") as f:
                f.write(content)

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
        三者同源，官方值当前是 41151 (main 3966 commits)。

        因此**绝不能**用 builtin 自身或 merge-base(builtin, main) 的提交数
        去 override：那会算出 41865 / 41010，反而把正确的 41151 改坏，
        正是 manager「显示未安装」的成因之一。

        唯一要防的是：detached + shallow clone 下本地 `main` ref 不存在，
        且 GITHUB_COMMITS (GitHub API 限流) 为空时，Makefile 会掉到兜底
        值 13000。做法是 unshallow 并显式 fetch main，让 `rev-list --count
        main` 可用；公式本身保持内核原样。
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
                self._run_cmd(f"patch -p1 --fuzz=3 < {patch_file}", check=True)  # SUSFS 补丁冲突必须显性失败
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
        # susfs v2.3.0 的 task_mmu.c hunk 用了 `end = VMA_PAD_START(vma);`，
        # 但 5.10 内核源码里没有这个宏（它是较新内核才有的），编译会报
        # 'VMA_PAD_START' undeclared。
        #
        # 原实现依赖 `#include <linux/pkeys.h>` 作为插入锚点：如果这个
        # include 不存在（或 susfs 补丁正好改动了那段），inserted 会一直是
        # False，宏定义从未被写入 -> 静默编译失败。这里改成不依赖任何锚点：
        # 无条件把 #ifndef 保护的定义插到第一个 #include 之前，幂等且必定生效。
        if "VMA_PAD_START" in content and "#define VMA_PAD_START" not in content:
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
                "CONFIG_KSU_SUSFS", "CONFIG_HEADERS_INSTALL")
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
                cmd = "tools/bazel build --disk_cache=/home/runner/.cache/bazel --config=fast --lto=thin //common:kernel_aarch64_dist"

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

    def patch_kpm_image(self):
        """给 kernel Image 注入 KernelPatch (kpimg)。CONFIG_KPM=y 只是桥接层，
        运行时还需要 Image 被打过补丁，否则 kpm version 返回 -EOPNOTSUPP。"""
        if not self.config.use_kpm:
            self.manifest.update(kpm_patched=False, kpm_skip_reason="use_kpm=false")
            return
        if self.config.kernel_version == "6.6":
            self.manifest.update(kpm_patched=False,
                                 kpm_skip_reason="6.6 KPM patch skipped (known stability issues)")
            logger.warning("=== 跳过 KPM 补丁 (6.6 已知稳定性问题) ===")
            return

        logger.info("=== 修补 Image 文件 (KPM) ===")
        self._chdir(self.work_dir)
        if self.config.android_version in ["android12", "android13"]:
            image_dir = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_dir = self.work_dir / "bazel-bin/common/kernel_aarch64"
        if not image_dir.exists():
            raise RuntimeError(f"KPM: 找不到内核产物目录 {image_dir}")

        self._chdir(image_dir)
        patch_path = image_dir / "patch"
        self._run_cmd(f"curl -fL --retry 3 -Ss {KPM_PATCH_URL} -o patch", check=True)
        if not patch_path.exists() or patch_path.stat().st_size < 1024:
            raise RuntimeError("KPM: 补丁下载失败或文件异常小")
        digest = _sha256_file(patch_path)
        logger.info(f"KPM patch sha256: {digest}")
        self.manifest.update(kpm_patch_url=KPM_PATCH_URL, kpm_patch_sha256=digest,
                             kpm_patch_upstream=KPM_PATCH_UPSTREAM)
        if KPM_PATCH_SHA256:
            if digest.lower() != KPM_PATCH_SHA256.lower():
                raise RuntimeError(f"KPM 补丁 sha256 不匹配: 期望 {KPM_PATCH_SHA256} 实际 {digest}")
            self.manifest["kpm_patch_sha256_verified"] = True
        else:
            self.manifest["kpm_patch_sha256_verified"] = False
        self._run_cmd("chmod 755 patch && ./patch", check=True)
        oimage = image_dir / "oImage"
        if not oimage.exists() or oimage.stat().st_size < 1024 * 1024:
            raise RuntimeError("KPM: patch 未生成有效 oImage，中止（避免产出未打补丁的内核）")
        self._run_cmd("rm -f patch && mv oImage Image", check=True)
        self.manifest["kpm_patched"] = True
        logger.info("=== KPM 补丁已应用 ===")

    def prepare_boot_images(self) -> list:
        logger.info("=== 准备启动镜像 ===")
        self._chdir(self.work_dir)
        bootimgs_dir = self.work_dir / "bootimgs"
        bootimgs_dir.mkdir(exist_ok=True)
        artifacts = []

        if self.config.android_version in ["android12", "android13"]:
            image_source = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_source = self.work_dir / "bazel-bin/common/kernel_aarch64"

        for image_name in ["Image", "Image.lz4"]:
            src = image_source / image_name
            if src.exists():
                self._run_cmd(f"cp {src} {bootimgs_dir}/ && cp {src} {self.work_dir}/", check=False)

        if (self.work_dir / "Image").exists():
            self._run_cmd("gzip -n -k -f -9 Image", check=False)

        if self.config.android_version == "android12":
            self._prepare_android12_boot_images(bootimgs_dir, artifacts)
        else:
            self._prepare_boot_images_generic(bootimgs_dir, artifacts)
        return artifacts

    def _prepare_android12_boot_images(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        gki_url = f"https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-{self.config.os_patch_level}_{self.config.revision}.zip"
        fallback_url = "https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-2023-01_r1.zip"
        result = subprocess.run(f"curl -sL -w '%{{http_code}}' {gki_url} -o /dev/null", shell=True, capture_output=True, text=True)
        url = gki_url if "200" in result.stdout else fallback_url
        self._run_cmd(f"curl -Lo gki-kernel.zip {url} && unzip -o gki-kernel.zip && rm gki-kernel.zip", check=False)
        boot_img_path = bootimgs_dir / "boot-5.10.img"
        if boot_img_path.exists():
            self._run_cmd(f"$UNPACK_BOOTIMG --boot_img={boot_img_path}", check=False)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=True)

    def _prepare_boot_images_generic(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=False)

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
            self._run_cmd(f"$AVBTOOL add_hash_footer --partition_name boot --partition_size $((64 * 1024 * 1024)) --image {output_file} --algorithm SHA256_RSA2048 --key $BOOT_SIGN_KEY_PATH", check=False)
            dest = self.work_dir / f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-{output_file}"
            self._run_cmd(f"cp {output_file} {dest}", check=False)
            artifacts.append(str(dest))

    def create_anykernel_zips(self) -> list:
        logger.info("=== 创建 AnyKernel3 ZIP 文件 ===")
        self._chdir(self.work_dir)
        artifacts = []
        ak3_dir = self.anykernel_dir

        # 实际文件是 Image.lz4 / Image.gz（点号）；原连字符写法让 lz4/gz 包成死分支
        for suffix, file_suffix in [("", ""), ("-lz4", ".lz4"), ("-gz", ".gz")]:
            image_file = f"Image{file_suffix}"
            image_path = self.work_dir / image_file
            if not image_path.exists():
                continue
            zip_name = f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-AnyKernel3{suffix}.zip"
            self._run_cmd(f"cp {image_path} {ak3_dir}/", check=False)
            self._chdir(ak3_dir)
            self._run_cmd(f"zip -r ../{zip_name} ./*", check=False)
            self._run_cmd(f"rm {ak3_dir}/{image_file}", check=False)
            artifacts.append(str(self.work_dir / zip_name))
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
        self.manifest["recommended_variant"] = RECOMMENDED_BOOT_VARIANT
        self.manifest["disabled_variants"] = list(DISABLED_BOOT_VARIANTS)
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
        boots = [Path(a) for a in artifacts if a.endswith(".img") and "boot" in Path(a).name]
        if not boots:
            raise RuntimeError("自检失败：没有产出任何 boot*.img")
        for img in boots:
            if not img.exists() or img.stat().st_size < 1024 * 1024:
                raise RuntimeError(f"自检失败：{img.name} 缺失或异常小")
        if not [i for i in boots if i.name.endswith("-boot-gz.img")]:
            raise RuntimeError("自检失败：缺少 boot-gz.img（本机唯一可启动压缩格式）")
        image_gz = self.work_dir / "Image.gz"
        if image_gz.exists() and self._run_cmd(f"gzip -t {image_gz}", check=False).returncode != 0:
            raise RuntimeError("自检失败：Image.gz 不是有效 gzip 文件")
        avbtool = self.env.get("AVBTOOL", "")
        if avbtool and Path(avbtool).exists() and self.env.get("BOOT_SIGN_KEY_PATH"):
            for img in boots:
                if self._run_cmd(f"{avbtool} info_image --image {img}", check=False).returncode != 0:
                    logger.warning(f"AVB footer 校验未通过: {img.name}（设备已禁用校验时可忽略）")
        if self.config.use_kpm and self.config.kernel_version != "6.6" and not self.manifest.get("kpm_patched"):
            raise RuntimeError("自检失败：启用 KPM 但补丁未标记成功")
        logger.info(f"=== 产物自检通过：{len(boots)} 个 boot 镜像 ===")

