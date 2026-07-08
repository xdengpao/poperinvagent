#!/usr/bin/env python3
"""业务时限口径 lint（任务 1.4，需求 R4.3）。

业务代码禁止裸用 ``timedelta(days=...)``/``timedelta(hours=...)`` 表达业务时限——
一律经 calendar.Duration（显式口径 td/h/d）。日历服务模块自身豁免。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERN = re.compile(r"timedelta\s*\(")
EXEMPT = ("src/invest_assistant/calendar/",)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    hits: list[str] = []
    for f in (root / "src").rglob("*.py"):
        rel = str(f.relative_to(root))
        if any(rel.startswith(e) for e in EXEMPT):
            continue
        text = f.read_text(encoding="utf-8")
        for m in PATTERN.finditer(text):
            line = text[: m.start()].count("\n") + 1
            hits.append(f"{rel}:{line}: 裸 timedelta —— 业务时限必须用 calendar.Duration（R4.3）")
    if hits:
        print("\n".join(hits))
        return 1
    print("业务时限口径检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
