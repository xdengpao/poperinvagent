"""交易日历服务（任务 3，需求 R4）。

- 交易日算术按标的所在市场日历（XSHG=沪、XNYS=美）；
- 账务日拼合：以美东收盘日为账务日，A 股取同账务日最近收盘（R4.2）;
- 时间量词三口径解析：N 交易日（市场日历）/ 墙钟小时 / 自然日（R4.3）。

全系统业务时限禁止裸用 timedelta —— 一律经 :class:`Duration`
（scripts/lint_business_duration.py 强制，任务 1.4）。
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

import exchange_calendars as xcals

_MARKET_CAL = {"CN": "XSHG", "US": "XNYS"}


class DurationKind(StrEnum):
    TRADING_DAYS = "td"  # N 交易日：标的所在市场日历
    WALL_CLOCK_HOURS = "h"  # 冷静期 24h/48h：墙钟
    NATURAL_DAYS = "d"  # 意见有效期 90 天 / 观察期一个季度：自然日


@dataclass(frozen=True)
class Duration:
    """业务时限。``Duration.parse("30td")`` / ``("48h")`` / ``("90d")``。"""

    amount: int
    kind: DurationKind

    _PATTERN = re.compile(r"^(\d+)(td|h|d)$")

    @classmethod
    def parse(cls, text: str) -> Duration:
        m = cls._PATTERN.match(text)
        if not m:
            raise ValueError(f"非法时限表达：{text!r}（口径必须显式：td/h/d，R4.3）")
        return cls(int(m.group(1)), DurationKind(m.group(2)))


class CalendarService:
    """沪深 + 美股双市场日历（exchange_calendars，含假日与半日市）。"""

    @staticmethod
    @lru_cache(maxsize=4)
    def _cal(market: str) -> xcals.ExchangeCalendar:
        code = _MARKET_CAL.get(market)
        if code is None:
            raise ValueError(f"未接入市场日历：{market}（当前支持 {sorted(_MARKET_CAL)}，扩展见 O-03）")
        return xcals.get_calendar(code)

    def is_trading_day(self, market: str, day: _dt.date) -> bool:
        return bool(self._cal(market).is_session(day))

    def add(self, market: str, start: _dt.date, duration: Duration) -> _dt.date | _dt.datetime:
        """自 start 起加一个业务时限，返回到期日（含义：第 N 个交易日/自然日到期）。"""
        if duration.kind is DurationKind.TRADING_DAYS:
            cal = self._cal(market)
            session = start if cal.is_session(start) else cal.date_to_session(start, direction="next")
            return cal.session_offset(session, duration.amount).date()
        if duration.kind is DurationKind.NATURAL_DAYS:
            return start + _dt.timedelta(days=duration.amount)
        # 墙钟小时：调用方须给出带时刻的起点
        raise ValueError("墙钟时限请用 add_wall_clock(start_dt, duration)")

    def add_wall_clock(self, start: _dt.datetime, duration: Duration) -> _dt.datetime:
        if duration.kind is not DurationKind.WALL_CLOCK_HOURS:
            raise ValueError("仅接受墙钟口径")
        return start + _dt.timedelta(hours=duration.amount)

    def accounting_day(self, us_close_date: _dt.date) -> dict[str, _dt.date]:
        """账务日拼合（R4.2）：账务日 = 美东收盘日；A 股取同账务日最近（≤ 账务日）收盘。"""
        cn = self._cal("CN")
        cn_session = cn.date_to_session(us_close_date, direction="previous")
        return {"accounting": us_close_date, "US": us_close_date, "CN": cn_session.date()}

    def sessions_between(self, market: str, start: _dt.date, end: _dt.date) -> int:
        cal = self._cal(market)
        return len(cal.sessions_in_range(start, end))
