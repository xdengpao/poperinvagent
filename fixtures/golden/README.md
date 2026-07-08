# 金标准回归 fixture（`fixtures/golden/`）

来源：S1/S2/S3 第 1 期人工代跑报告（2026-07-07，`docs/S1-*.md`、`docs/S2-*.md`、`docs/S3-*.md`）。
本目录是 P0 第一工作项（任务 2）的产物：把报告及其输入证据回填为机器可读 fixture，
供回归测试断言（需求 R20.1/R20.2/R20.3）。FactSet 型一次性来源以证据对象固化，不依赖适配器复取。

**纪律**：fixture 只登记报告中实际出现的数据与结论，逐条附 `provenance`（报告文件+可定位的表/节）；
报告未给出的数字一律不得编造——以 `"gap": true` 显式登记缺失。数值单位在字段名或 `unit` 中显式。

## 目录与 schema

### `s1/evidence.json` — 行业六因子证据
```json
{
  "period": "2026-07",
  "cutoff_date": "2026-07-07",
  "industries": [
    {
      "industry": "<报告中的行业名>",
      "factors": [
        {
          "factor": "<报告中的因子编号 F1-F6>",
          "name": "<因子名>",
          "grade": 0-5,
          "approx": false,        // 报告标 ≈（近似口径）时为 true
          "gap": false,           // 证据缺失计 0 时为 true（grade 必须为 0）
          "basis": "<报告给出的评分依据原文摘要>",
          "provenance": "docs/S1-行业闸门扫描-第1期-2026-07-07.md#<定位>"
        }
      ]
    }
  ]
}
```

### `s1/rulings.json` — 裁决集
```json
[
  {
    "id": "裁决A",
    "date": "2026-07-07",
    "scope": ["<受影响行业>"],
    "factor": "<因子编号>",
    "from": 4, "to": 5,
    "rationale": "<报告裁决记录原文摘要>",
    "review_condition": "<条件化复核，如财报季复核>",
    "provenance": "..."
  }
]
```

### `s1/expected.json` — 两态期望（裁决 A 前/后）
```json
{
  "pre_ruling":  { "scores": {"<行业>": <总分>}, "qualified": ["..."], "not_qualified": ["..."] },
  "post_ruling": { "scores": {"<行业>": <总分>}, "qualified": ["..."], "not_qualified": ["..."] },
  "gate_line": 21,
  "top_n": 5,
  "provenance": "..."
}
```

### `s2/inputs.json` — 候选域输入
```json
{
  "period": "2026-07",
  "qualified_industries": ["..."],
  "candidates": [
    {
      "symbol": "NVDA",
      "name": "英伟达",
      "market": "US",                  // US | KR | JP | NL | CH | TW | IL/US ...
      "chain": "<环节>",
      "mainline": "<主线归属：算力芯片|半导体设备与材料|云计算与数据中心|电力|...>",
      "sources": ["seed", "graph#1"],  // 三来源并集证据；user 自选为 "user"
      "adv_usd_90d": 30650000000,      // 90 日日均美元成交额（报告口径，代理口径）
      "unit": "USD",
      "data_scope": "US-full" | "non-US" | "A-share",
      "disruption_hit": false,
      "provenance": "docs/S2-候选池-第1期-2026-07-07.md#<定位>"
    }
  ],
  "liquidity_threshold_usd": 10000000,
  "liquidity_proxy": "90d",            // 框架规定 20d，本期 90d 代理，G 级标注
  "graph_coverage_note": "AI Chips 主题 75 家，本期前 50；51-75 下期补扫"
}
```
排除标的（GOOGL/AMZN/VRT/ANET/AMBA/MBLY/SMCI 等）也放入 candidates，
由 `mainline` 不在 `qualified_industries` 内表达排除动因；报告的排除理由放 `exclusion_reason`。

### `s2/expected.json`
```json
{
  "group_A": ["<24 只 symbol>"],
  "group_B": ["<16 只>"],
  "group_C": ["<7 只代码>"],
  "excluded": [ {"symbol": "GOOGL", "reason_class": "mainline-mismatch", "provenance": "..."} ],
  "disruption_new": [],
  "provenance": "..."
}
```

### `s3/cases.json` — 一票否决（P0 仅回填数据；引擎断言在 P1 任务 11.3）
```json
{
  "period": "2026-07",
  "cases": [
    {
      "symbol": "MPWR",
      "verdict": "veto" | "pass" | "edge" | "stranded",
      "items": {
        "item1": {
          "conditions": {
            "regulatory_fraud_case": true|false|null,   // null=报告未涉及
            "audit_opinion_adverse": true|false|null,   // 含 ICFR
            "auditor_changes_5y_ge2": true|false|null,
            "restatement_8k402": true|false|null        // 加严项→裁决队列
          },
          "hit": true|false|null,
          "evidence": "<报告证据摘要>",
          "provenance": "..."
        },
        "item2..item5": { "...同构，conditions 按各项子条件命名..." }
      },
      "monitoring": ["<监控项>"],
      "ruling_request": "D-1" | null,   // 边缘态关联的裁决请求编号
      "stranded_reason": "<滞留原因>" | null,
      "provenance": "..."
    }
  ],
  "expected_tally": { "pass": 10, "veto": 3, "edge": 3, "stranded": 8 }
}
```

### `normalization/cases.json` — 归一化六实跑案例
```json
{
  "cases": [
    {
      "case": "KLA-split",              // KLA 拆股 | AVGO-53wk | ARM-semiannual-cf |
                                         // AMD-discontinued | NVDA-oneoff | MRVL-negative-ni
      "transform": "split_adjustment",
      "inputs": { "...报告/披露中实际出现的原始数字，缺失用 gap 登记..." },
      "expected": { "...归一化后的期望值或期望判定..." },
      "notes": "<口径说明，含数据粒度（年度/季度/半年）>",
      "provenance": "..."
    }
  ]
}
```
六案例中报告只给结论性数字的（如 ARM FY2025 转化率 0.50、FY2026 1.69），
inputs 记录报告给出的层级（可以是比率本身），expected 记录规则判定
（如负净利期 OCF>0→通过）；不得虚构未披露的明细。

## 版本

fixture 一经回填冻结；勘误以新文件版本追加（`*.v2.json`）并在本 README 登记差异。
