#!/usr/bin/env python3
"""在打 SUSFS 补丁前恢复被 GKI 源码删掉的 `#include <trace/hooks/*>` 上下文行。

Google 会在较新的 GKI sublevel 里移除 `<trace/hooks/blk.h>`、`<trace/hooks/fs.h>`、
`<trace/hooks/mm.h>` 等 include，但 susfs4ksu 的
`50_add_susfs_in_gki-*.patch` 仍把这些行当作首个 hunk 的上下文，导致
`patch` 在该文件 Hunk #1 直接 FAILED（实测 android13-5.15-189 的 fs/namespace.c）。

这些 include 只是上下文：SUSFS 不使用任何 trace hook 符号。把缺失的行按补丁期望的
顺序插回去、头文件不存在时建一个空 stub header，即可让补丁完整应用，且不影响内核。

来源：WildKernels/GKI_KernelSU_SUSFS 的
`.github/scripts/susfs_restore_context.py`（通用、幂等、无版本门槛）。
该步骤在其 `.github/actions/susfs-patches/action.yml` 里对所有内核家族无条件执行。

用法: susfs_restore_context.py <patch> <tree>
"""
import pathlib
import re
import sys

INCLUDE_RE = re.compile(r"^#include\s+(?P<inc><trace/hooks/[^>]+>)")
ANY_INCLUDE = re.compile(r'^#include\s+(?P<inc>[<"][^>"]+[>"])')


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
            if incs:
                files[cur] = incs
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
    """为只作上下文用的 trace hook 头文件建空占位（若树里已不存在）。"""
    rel = inc.strip("<>")
    header = tree / "include" / rel
    if header.exists():
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
    for rel, includes in plan.items():
        # 只处理 <trace/hooks/...> 这一类上下文。
        if not any(INCLUDE_RE.match("#include " + i) for i in includes):
            continue
        target = tree / rel
        if not target.exists():
            continue
        lines = target.read_text(errors="replace").splitlines(keepends=True)
        present = [l.strip() for l in lines]

        # 按 hunk 的 include 顺序维护一个游标：缺哪一行就插在它的
        # 前驱之后（而不是插到文件最后一个 include 之后），以免破坏上下文。
        changed = False
        cursor = 0
        for inc in includes:
            want = f"#include {inc}"
            try:
                idx = present.index(want, cursor)
            except ValueError:
                idx = None
            if idx is not None:
                cursor = idx + 1
                continue
            if not INCLUDE_RE.match("#include " + inc):
                continue
            lines.insert(cursor, want + "\n")
            present.insert(cursor, want)
            added.append(f"{rel}: {want}")
            stub = ensure_stub(tree, inc)
            if stub:
                stubbed.append(str(stub.relative_to(tree)))
            cursor += 1
            changed = True
        if changed:
            target.write_text("".join(lines))

    for a in added:
        print(f"context restored  {a}")
    for s in stubbed:
        print(f"stub header made  {s}")
    if not added and not stubbed:
        print("context already complete - nothing to restore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
