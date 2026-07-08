#!/usr/bin/env python3
"""启动前置自检演示（R1.1/R1.2）：全清单探测 → 连通性矩阵 → 意见模式判定。"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from invest_assistant.data.adapters.misc import build_default_registry  # noqa: E402


def main() -> int:
    allow, results = build_default_registry().startup_self_check()
    print(f"{'适配器':<16} {'状态':<4} 说明")
    print("-" * 72)
    for r in results:
        mark = "✅" if r.ok else "❌"
        print(f"{r.adapter:<16} {mark:<4} {r.error or 'OK'}"
              + (f"  覆盖率:{r.coverage}" if r.coverage else ""))
    print("-" * 72)
    print("意见生成模式：" + ("允许" if allow else "拒绝（degraded——生产必需源探测失败，R1.2）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
