"""S3 一票否决审查器（任务 11，需求 R9）。

五项 × 四态：判定输入 = 条件布尔向量（数值子项来自派生指标，取证子项来自
通过防幻觉链的证据）+ 定性警报 + 取证完成度。输出 veto_verdict（命中路径
逐条件向量，支撑 R20.2 逐条件断言；A4 三元组引用）。

条件语义：True=证据确认成立；False=证据确认不成立；None=无证据（A2：
不得推断，计入未决——若该项无命中/裁决信号且案件取证未完成 → 滞留）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..rules.engine import RuleRepository

ITEM_RULES = {
    "item1": "S3.ITEM1.ENUM",
    "item2": "S3.ITEM2.CASHFLOW",
    "item3": "S3.ITEM3.HOLDER",
    "item4": "S3.ITEM4.MA",
    "item5": "S3.ITEM5.DILUTION",
}


@dataclass(frozen=True)
class ItemInput:
    conditions: dict[str, bool | None]
    qualitative_alert: bool = False  # 取证层登记的待定性信号（非财务类处罚/法律信号叠加等）
    concluded_not_hit: bool = False  # 结论级证据：取证已确认本项未命中（细分条件可为 None）
    # 结论级证据对应报告"全部未命中"总述的证据对象；机械条件重算优先于结论级
    # （若二者矛盾按保守方向取命中并留痕）。


@dataclass(frozen=True)
class CaseInput:
    symbol: str
    market: str  # US | CN | ...
    items: dict[str, ItemInput]
    evidence_complete: bool = True  # 取证任务全部闭合（False=滞留候选，FR-O-06）


@dataclass
class ItemResolution:
    item: str
    state: str  # hit | ruling_pending | clear | unresolved
    conditions: dict[str, bool | None]
    note: str = ""


@dataclass
class VetoVerdict:
    symbol: str
    verdict: str  # veto | edge | stranded | pass
    items: list[ItemResolution]
    ruling_needed: bool = False
    rule_ids: list[str] = field(default_factory=list)
    snapshot_id: str = ""
    evidence_ids: list[str] = field(default_factory=list)


def _any_true(conds: dict[str, bool | None], names: list[str]) -> bool:
    return any(conds.get(n) is True for n in names)


def _all_true(conds: dict[str, bool | None], names: list[str]) -> bool:
    return all(conds.get(n) is True for n in names)


class S3Engine:
    def __init__(self, rules: RuleRepository):
        self._rules = rules

    def _resolve_item(self, item: str, inp: ItemInput, market: str) -> ItemResolution:
        rule = self._rules.get(ITEM_RULES[item])
        p = rule.params
        conds = inp.conditions

        if item == "item3":
            if market == "CN":
                if _all_true(conds, p["cn_hit_all"]):
                    return ItemResolution(item, "hit", conds)
            elif p.get("us_mode") == "transfer_to_s4":
                # 美股口径（R9.4）：Form 4 减持单独判定、10b5-1/RSU 豁免，记录转 S4——
                # 本项不产生否决命中；证据缺失也不计未决（口径差异标注）
                note = "美股口径：减持记录转 S4 管理层维（O-03）"
                if inp.qualitative_alert:
                    return ItemResolution(item, "ruling_pending", conds, note)
                return ItemResolution(item, "clear", conds, note)

        if "hit_any" in p and _any_true(conds, p["hit_any"]):
            return ItemResolution(item, "hit", conds)
        if "hit_all" in p and p["hit_all"] and _all_true(conds, p["hit_all"]):
            return ItemResolution(item, "hit", conds)
        if inp.qualitative_alert or ("ruling_any" in p and _any_true(conds, p["ruling_any"])):
            return ItemResolution(item, "ruling_pending", conds, "字面/定性信号 → 裁决队列（FR-R-06）")
        if inp.concluded_not_hit:
            return ItemResolution(item, "clear", conds, "结论级证据：确认未命中")

        checked = [n for names in (p.get("hit_any", []), p.get("hit_all", []),
                                   p.get("cn_hit_all", []) if market == "CN" else [])
                   for n in names]
        if checked and all(conds.get(n) is False for n in checked):
            return ItemResolution(item, "clear", conds)
        if any(conds.get(n) is None for n in checked):
            return ItemResolution(item, "unresolved", conds, "关键条件无证据（A2）")
        return ItemResolution(item, "clear", conds)

    def judge(self, case: CaseInput, snapshot_id: str = "") -> VetoVerdict:
        resolutions = [self._resolve_item(item, inp, case.market) for item, inp in sorted(case.items.items())]
        rule_ids = [ITEM_RULES[r.item] for r in resolutions] + ["S3.VERDICT.FOURSTATE"]

        # 四态优先级（S3.VERDICT.FOURSTATE，保守方向：命中 > 边缘 > 滞留 > 通过）
        if any(r.state == "hit" for r in resolutions):
            verdict = "veto"
        elif any(r.state == "ruling_pending" for r in resolutions):
            verdict = "edge"
        elif not case.evidence_complete or any(r.state == "unresolved" for r in resolutions):
            verdict = "stranded"
        else:
            verdict = "pass"
        return VetoVerdict(
            symbol=case.symbol, verdict=verdict, items=resolutions,
            ruling_needed=(verdict == "edge"), rule_ids=rule_ids, snapshot_id=snapshot_id,
        )
