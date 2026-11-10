#!/usr/bin/env python3
"""在开始构建前校验参数组合是否合法。

动机：Kernel Build 的弹窗里 Android 版本 / Kernel 版本 / Sub Level /
OS Patch Level 是四个独立输入，任意组合都允许填。但内核源码分支是
`common-<android>-<kernel>-<os_patch_level>`，**只有特定组合才存在**。
填错时 repo init 会报 `manifest project not found` 这类迷惑错误，
而且此时已经白下载了几十 GB 工具链。

所以这里对照 `.github/workflows/config/matrix.json`（与 Build Kernels
全矩阵用的是同一份数据源）做校验，非法组合直接给出可用值清单。

用法:
  validate_params.py --android android12 --kernel 5.10 \
      --sub-level 236 --os-patch 2025-05
"""
import argparse
import json
import sys
from pathlib import Path

MATRIX_PATH = Path(__file__).resolve().parent.parent / "config" / "matrix.json"


def load_matrix() -> dict:
    with open(MATRIX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser(description="校验 GKI 构建参数组合")
    ap.add_argument("--android", required=True)
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--sub-level", required=True)
    ap.add_argument("--os-patch", required=True)
    args = ap.parse_args()

    matrix = load_matrix()
    key = f"{args.android}-{args.kernel}"
    configs = matrix.get(key)

    print(f"=== 参数校验: {args.android} / {args.kernel} / "
          f"sub={args.sub_level} / patch={args.os_patch} ===")

    if not configs:
        print(f"\n[错误] 不支持的组合 {key}。", file=sys.stderr)
        print("可用的 Android/Kernel 组合：", file=sys.stderr)
        for k in sorted(matrix):
            print(f"  - {k}", file=sys.stderr)
        return 1

    # 列出该家族所有合法 (sub_level, os_patch_level) 组合
    valid = [(c["sub_level"], c["os_patch_level"]) for c in configs]
    print("该组合下合法的 (Sub Level, OS Patch Level) 取值：")
    for sub, pat in valid:
        mark = "  <-- 你填的" if (sub == args.sub_level and pat == args.os_patch) else ""
        print(f"  - sub_level={sub:<5} os_patch_level={pat}{mark}")

    # 精确匹配
    for c in configs:
        if (c["sub_level"] == args.sub_level
                and c["os_patch_level"] == args.os_patch):
            print(f"\n[OK] 匹配到 {key} sub={args.sub_level} patch={args.os_patch}")
            if c.get("revision"):
                print(f"      certified boot 模板 revision = {c['revision']}")
            else:
                print("      certified boot 模板 revision = r1（留空即可）")
            return 0

    # 提示最接近的组合
    print(f"\n[错误] {key} 下不存在 sub_level={args.sub_level} "
          f"os_patch_level={args.os_patch} 的组合。", file=sys.stderr)
    print("Sub Level 与 OS Patch Level 必须配套使用，不能随意组合。",
          file=sys.stderr)
    print("请从上面的清单里选一组。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
