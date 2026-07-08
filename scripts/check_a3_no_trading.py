#!/usr/bin/env python3
"""A3 铁律 CI 检查（任务 1.4）：代码库禁止出现下单类调用与交易执行 SDK。

- 禁止 import 的包：ib_insync 下单部分无法按符号区分，故整包禁止；ccxt、easytrader 等；
- 禁止的调用样式：place_order / submit_order / create_order / cancel_order 等。
命中即退出码 1（CI 合并阻断）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(import|from)\s+(ib_insync|ccxt|easytrader|alpaca|futu|vnpy)\b", re.M
)
FORBIDDEN_CALLS = re.compile(
    r"\b(place_?order|submit_?order|create_?order|cancel_?order|order_?submit|"
    r"placeOrder|submitOrder|create_order_instruction)\s*\(",
)

SCAN_DIRS = ("src",)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    hits: list[str] = []
    for d in SCAN_DIRS:
        for f in (root / d).rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            for pat, kind in ((FORBIDDEN_IMPORTS, "交易 SDK import"), (FORBIDDEN_CALLS, "下单调用")):
                for m in pat.finditer(text):
                    line = text[: m.start()].count("\n") + 1
                    hits.append(f"{f.relative_to(root)}:{line}: {kind}: {m.group(0).strip()}")
    if hits:
        print("A3 铁律违规（系统无交易执行权）：")
        print("\n".join(hits))
        return 1
    print("A3 检查通过：无交易执行代码。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
