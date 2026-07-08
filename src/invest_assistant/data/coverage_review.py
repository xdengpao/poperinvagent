"""数据源覆盖率季度复议（P3 任务 24，需求 R1.3；O-01 决策 1/2）。

汇集 Tushare 逐接口权限覆盖率（含 D3 卖方盈利预测 report_rc）与一致预期
覆盖率登记，产出决策 1 季度复议报告：是否值得订阅付费源、冷启动库进度。
只汇集不判定；订阅决策由用户按报告作出（A1）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoverageReview:
    period: str
    tushare_coverage: dict[str, bool]     # 接口 → 权限是否覆盖（来自 probe，R1.3）
    consensus_covered_count: int          # 一致预期已覆盖标的数
    consensus_total_count: int            # 应覆盖标的数
    degraded_downgrade_count: int         # 因预期数据缺失被降档的标的数（决策 1 依据）
    guidance_cold_start_quarters: int     # 指引兑现率库已积累季数（决策 2，满 4 季切换）

    @property
    def report_rc_available(self) -> bool:
        """D3 卖方盈利预测接口权限（决定免费降级覆盖率能否提升）。"""
        return self.tushare_coverage.get("report_rc", False)

    @property
    def consensus_coverage_rate(self) -> float | None:
        if self.consensus_total_count <= 0:
            return None
        return self.consensus_covered_count / self.consensus_total_count

    @property
    def subscription_recommended(self) -> bool:
        """决策 1 建议：免费源覆盖不足且降档标的多 → 建议评估订阅。

        判据（保守）：report_rc 无权限 且 一致预期覆盖率 <50% 且 降档标的 ≥3。
        仅为建议，用户决定（不自动订阅）。
        """
        rate = self.consensus_coverage_rate
        return (not self.report_rc_available and rate is not None and rate < 0.5
                and self.degraded_downgrade_count >= 3)

    @property
    def cold_start_switchable(self) -> bool:
        """决策 2：指引兑现率库满 4 季 → 可从中档下限切换为实际兑现率。"""
        return self.guidance_cold_start_quarters >= 4

    def to_markdown(self) -> str:
        rate = self.consensus_coverage_rate
        rate_s = "—" if rate is None else f"{rate:.1%}"
        cov = "\n".join(f"  - {k}：{'✅' if v else '❌'}" for k, v in sorted(self.tushare_coverage.items()))
        return f"""# 数据源覆盖率复议 · {self.period}（O-01 决策 1/2）

## Tushare 接口权限覆盖（R1.3）
{cov}
- D3 卖方盈利预测（report_rc）：{"✅ 有权限" if self.report_rc_available else "❌ 无权限（免费降级覆盖受限）"}

## 一致预期覆盖（决策 1）
- 覆盖 {self.consensus_covered_count}/{self.consensus_total_count}（{rate_s}）
- 本季因预期缺失被降档标的：{self.degraded_downgrade_count} 只
- 订阅评估建议：{"建议评估付费源订阅" if self.subscription_recommended else "暂不需要（免费源足够）"}

## 冷启动进度（决策 2）
- 指引兑现率库已积累：{self.guidance_cold_start_quarters} 季
- 冷启动切换：{"✅ 满 4 季，可切换为实际兑现率" if self.cold_start_switchable else "未满 4 季，维持中档下限"}
"""


def build_coverage_review(
    period: str,
    *,
    tushare_probe_coverage: dict[str, bool],
    consensus_covered: int,
    consensus_total: int,
    degraded_downgrades: int,
    guidance_quarters: int,
) -> CoverageReview:
    return CoverageReview(
        period=period,
        tushare_coverage=dict(tushare_probe_coverage),
        consensus_covered_count=consensus_covered,
        consensus_total_count=consensus_total,
        degraded_downgrade_count=degraded_downgrades,
        guidance_cold_start_quarters=guidance_quarters,
    )
