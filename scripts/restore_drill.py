#!/usr/bin/env python3
"""恢复演练脚本（R18.2/R19.3，规格书 FR-L-05）：
① audit_chain 完整性校验（篡改检测）；② 一次历史判定重放（金标准 S1 两态 diff）。
季度执行并纳入验收；退出码 0=演练通过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "src")

from invest_assistant.analysis.s1 import FactorEvidence, Ruling, S1Engine  # noqa: E402
from invest_assistant.audit.chain import AuditChain  # noqa: E402
from invest_assistant.rules import load_rules  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def drill_audit_chain() -> bool:
    chain = AuditChain()
    day_objects = {
        "2026-07-07": [{"id": "seed", "kind": "evidence"}],
        "2026-07-08": [{"id": "opinion-1", "kind": "opinion"}],
    }
    for day, objs in day_objects.items():
        chain.append_day(day, objs)
    ok, _ = chain.verify(day_objects)
    day_objects["2026-07-07"][0]["kind"] = "tampered"
    detected = not chain.verify(day_objects)[0]
    print(f"① 审计链校验：完整性 {'✅' if ok else '❌'}；篡改检测 {'✅' if detected else '❌'}")
    return ok and detected


def drill_replay() -> bool:
    golden = ROOT / "fixtures" / "golden"
    ev_doc = json.loads((golden / "s1" / "evidence.json").read_text())
    expected = json.loads((golden / "s1" / "expected.json").read_text())
    rulings = [
        Ruling(r["id"], tuple(r["scope"]), r["factor"], r["from"], r["to"])
        for r in json.loads((golden / "s1" / "rulings.json").read_text())
    ]
    evidence = [
        FactorEvidence(ind["industry"], f["factor"], f["grade"], f["approx"], f["gap"])
        for ind in ev_doc["industries"] for f in ind["factors"]
    ]
    result = S1Engine(load_rules()).run(evidence, rulings, snapshot_id="DRILL")
    got = {s.industry: s.total for s in result.scores}
    match = got == expected["post_ruling"]["scores"]
    print(f"② 历史判定重放（S1 裁决后态）：逐字段一致 {'✅' if match else '❌ ' + str(got)}")
    return match


def main() -> int:
    ok = drill_audit_chain() & drill_replay()
    print("恢复演练：" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
