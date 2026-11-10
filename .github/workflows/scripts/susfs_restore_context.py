#!/usr/bin/env python3
"""在打 SUSFS 补丁前恢复被 GKI 源码删掉/改动的 `#include` 上下文行。

susfs4ksu 的 `50_add_susfs_in_gki-*.patch` 仍把若干 include 当作 hunk 的上下文，
而不同 GKI sublevel 的源码里这些 include 可能已被 Google 移除或改名，导致
`patch` 在该文件 Hunk #1 直接 FAILED。实测到两种：

  - fs/namespace.c  缺 `#include <trace/hooks/blk.h>`（android13-5.15-189）
  - fs/proc/base.c   缺 `#include <linux/dma-buf.h>`（android14-6.1 sublevel<=141，
                     见 WildKernels 的 "Android 14 6.1 Fake Patches"）

做法：解析补丁每个文件的**首个 hunk** 的 include 序列，按顺序在源码里找，
缺失的按补丁期望位置插回去；头文件本身不存在时建一个空 stub header
（SUSFS 不使用这些符号，它们只是上下文）。通用、幂等、无版本门槛。

来源：WildKernels/GKI_KernelSU_SUSFS 的
`.github/scripts/susfs_restore_context.py`（通用、幂等、无版本门槛）。
该步骤在其 `.github/actions/susfs-patches/action.yml` 里对所有内核家族无条件执行。

用法: susfs_restore_context.py <patch> <tree>
"""
import pathlib
import re
import sys
from pathlib import Path

# 补丁 hunk 的上下文可能引用任何 include，不只是 <trace/hooks/...>：
#   - fs/proc/base.c 的 hunk #1 期望 #include <linux/dma-buf.h>
#     （android14-6.1 sublevel<=141 的源码没有这行，见 WildKernels 的
#      "Android 14 6.1 Fake Patches"）
#   - fs/namespace.c 的 hunk #1 期望 #include <trace/hooks/blk.h>
# 所以这里对所有 <...> 形式的 include 都允许补齐。
INCLUDE_RE = re.compile(r"^#include\s+(?P<inc><[^>]+>)")
ANY_INCLUDE = re.compile(r'^#include\s+(?P<inc>[<"][^>"]+[>"])')

# 只有 <trace/hooks/*> 允许建空 stub 头：这些头在 GKI 里纯粹是给
# tracepoint 用的，SUSFS 不引用其中任何符号，stub 不会引入
# undefined symbol（WildKernels 的 susfs_restore_context.py 同此做法）。
# 其它 include（如 <linux/dma-buf.h>）是内核真实使用的头，若树里没有
# 就不能 stub —— 那会让用到它的代码变成 undefined symbol。这种情况
# 宁可不插入，让 patch 报 hunk 失败并把 .rej 打出来。
STUB_ONLY_RE = re.compile(r"^#include\s+<trace/hooks/[^>]+>$")


def first_hunk_context_includes(hunk_lines):
    """首个 hunk 期望在源码里存在的有序 #include 行列表。"""
    out = []
    for line in hunk_lines:
        if line[:1] != " ":
            continue
        m = ANY_INCLUDE.match(line[1:].strip())
        if m:
            out.append(m.group("inc"))
    return out


COND_RE = re.compile(r"^\s*#\s*(?:ifndef|ifdef|if|if\s+!defined)\b")
ENDIF_RE = re.compile(r"^\s*#\s*endif\b")


def first_hunk_context_conds(hunk_lines):
    """首个 hunk 上下文里的条件编译指令（有序、去重）。

    不要求成对：hunk 边界常常在 #if 块中间截断（例如 fs/exec.c 的
    `#ifndef __GENKSYMS__` 后面没有 #endif），此时源码里对应的 #endif
    本来就存在，只要补开头即可。只补开头而漏补 #endif 才是危险的，
    所以这里只返回「开指令」，由调用方按需插入。
    """
    out = []
    for line in hunk_lines:
        if line[:1] != " ":
            continue
        text = line[1:].strip()
        if COND_RE.match(text) and text not in out:
            out.append(text)
    return out

def first_hunk_context_blanks(hunk_lines):
    """首个 hunk 里 include 序列之间的空行上下文位置。

    返回 {include: 前置空行数}：该 include 在补丁里出现前有几个空行。
    android14-6.1-78 的 fs/exec.c 就是这里出的问题 ——
    补丁期望 `coredump.h` 与 `#ifndef __GENKSYMS__` 之间有一个空行，
    而 6.1-78 的源码里没有，hunk 就匹配不上。
    """
    blanks = {}
    pending = 0
    for line in hunk_lines:
        if line[:1] != " ":
            continue
        text = line[1:].strip()
        if not text:
            pending += 1
            continue
        m = ANY_INCLUDE.match(text)
        if m:
            blanks.setdefault(m.group("inc"), pending)
            pending = 0
    return blanks


def parse_patch(text):
    """{路径: [include, ...]} —— 每个被 patch 文件的首 hunk。"""
    files = {}
    cur = None
    hunk = []
    in_first = False

    def flush():
        nonlocal hunk, in_first
        if cur is not None and in_first:
            incs = first_hunk_context_includes(hunk)
            conds = first_hunk_context_conds(hunk)
            blanks = first_hunk_context_blanks(hunk)
            if incs:
                files[cur] = (incs, conds, blanks)
        hunk = []
        in_first = False

    for line in text.splitlines():
        if line.startswith("diff --git "):
            flush()
            m = re.match(r"diff --git a/(\S+) b/", line)
            cur = m.group(1) if m else None
            continue
        if line.startswith("@@"):
            if in_first:
                flush()
                cur = None
                continue
            in_first = True
            continue
        if in_first:
            hunk.append(line)
    flush()
    return files


def ensure_stub(tree, inc):
    """为只作上下文用的 trace hook 头文件建空占位（若树里已不存在）。

    只允许 <trace/hooks/*>：这类头只服务 tracepoint，SUSFS 不引用其符号。
    其它 include 不建 stub（见 STUB_ONLY_RE 注释）。
    """
    rel = inc.strip("<>")
    header = tree / "include" / rel
    if header.exists():
        return None
    if not STUB_ONLY_RE.match(f"#include {inc}"):
        return None
    header.parent.mkdir(parents=True, exist_ok=True)
    guard = "_SUSFS_CONTEXT_STUB_" + re.sub(r"\W", "_", rel).upper()
    header.write_text(
        "/* Stub created by GKI_KernelSU_SUSFS.\n"
        f" * The SUSFS patch lists #include <{rel}> as hunk context only; this\n"
        " * GKI sublevel no longer ships the header. No symbols are used. */\n"
        f"#ifndef {guard}\n#define {guard}\n#endif\n"
    )
    return header


def restore_file(tree: Path, rel: str, includes, conds, blanks) -> tuple:
    """把 rel 里缺失的上下文行按补丁期望的位置插回去。

    做法：以补丁 hunk 给出的 include 序列为模板，在源码里按顺序查找，
    找到的记录其下标；缺失的记录「应插在哪个已找到项之后」。
    这样插入位置与补丁期望一致 —— 单纯「插在游标处」会因为源码里
    多出/少掉空行而错位一整行，导致 hunk 仍然匹配不上。

    返回 (added, stubbed)。
    """
    added, stubbed = [], []
    target = tree / rel
    if not target.exists():
        return added, stubbed

    lines = target.read_text(errors="replace").splitlines(keepends=True)
    present = [l.strip() for l in lines]
    orig_len = len(lines)

    # 1) 找出每个期望 include 在源码里的位置（贪心顺序匹配）
    positions = {}      # include -> 下标
    order = []          # 按补丁顺序记录 include
    search_from = 0
    for inc in includes:
        want = f"#include {inc}"
        # 源码里可能带行尾注释（补丁上下文写
        # `#include "proc/internal.h" /* only for ... */`，源码是裸的），
        # 比较时把注释去掉。
        def _key(text):
            return text.split("/*")[0].strip()

        idx = None
        for j in range(search_from, len(present)):
            if _key(present[j]) == _key(want):
                idx = j
                break
        if idx is not None:
            positions[inc] = idx
            order.append((inc, idx))
            search_from = idx + 1

    missing = [inc for inc in includes if inc not in positions]
    if not missing:
        return added, stubbed

    # 2) 决定每个缺失 include 的插入点：它前面那个「已找到的」include 之后
    insert_map = []   # (在 present 中的下标, 要插入的行列表)
    for inc in missing:
        # 找它在补丁序列里的前驱
        seq = includes.index(inc)
        anchor = None
        for j in range(seq - 1, -1, -1):
            if includes[j] in positions:
                anchor = positions[includes[j]]
                break
        if anchor is None:
            anchor = -1   # 插到文件开头
        block = []
        # 空行：补丁期望该 include 前有空行而源码没有时补上
        # （android14-6.1-78 的 fs/exec.c 就是缺这个空行）
        want_blanks = blanks.get(inc, 0)
        have_blanks = 0
        for j in range(anchor, -1, -1):
            if present[j] == "":
                have_blanks += 1
            else:
                break
        for _ in range(max(0, want_blanks - have_blanks)):
            block.append("")
            added.append(f"{rel}: <blank>")
        # 条件编译指令先于 include（只补开头；对应的 #endif 源码里本来就有，
        # 例如 fs/exec.c 的 `#ifndef __GENKSYMS__`）
        for opener in conds:
            if opener not in present:
                block.append(opener)
                added.append(f"{rel}: {opener}")
        block.append(f"#include {inc}")
        added.append(f"{rel}: #include {inc}")
        insert_map.append((anchor, block))
        if not (tree / "include" / inc.strip("<>")).exists():
            if STUB_ONLY_RE.match(f"#include {inc}"):
                stub = ensure_stub(tree, inc)
                if stub:
                    stubbed.append(str(stub.relative_to(tree)))

    # 3) 从后往前插，避免下标错乱
    for anchor, block in sorted(insert_map, key=lambda x: -x[0]):
        at = anchor + 1
        for line in reversed(block):
            lines.insert(at, line + "\n")
        # 更新后续插入点的基准（前面的插入会让下标后移）
        for k in range(len(insert_map)):
            a2, b2 = insert_map[k]
            if a2 > anchor:
                insert_map[k] = (a2 + len(block), b2)

    if len(lines) != orig_len:
        target.write_text("".join(lines))
    return added, stubbed


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: susfs_restore_context.py <patch> <tree>")
    patch_path = pathlib.Path(sys.argv[1])
    tree = pathlib.Path(sys.argv[2])
    if not patch_path.exists():
        raise SystemExit(f"patch not found: {patch_path}")
    if not tree.is_dir():
        raise SystemExit(f"tree not found: {tree}")

    plan = parse_patch(patch_path.read_text(errors="replace"))
    added, stubbed = [], []
    for rel, (includes, conds, blanks) in plan.items():
        if not any(INCLUDE_RE.match("#include " + i) for i in includes):
            continue
        a, st = restore_file(tree, rel, includes, conds, blanks)
        added.extend(a)
        stubbed.extend(st)

    for a in added:
        print(f"context restored  {a}")
    for s in stubbed:
        print(f"stub header made  {s}")
    if not added and not stubbed:
        print("context already complete - nothing to restore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
