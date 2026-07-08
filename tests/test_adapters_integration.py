"""适配器真联测（RUN_INTEGRATION=1 且网络策略放行时启用；沙箱网关 403 自动跳过）。

生产验收口径（任务 4 联测）：
  RUN_INTEGRATION=1 EDGAR_USER_AGENT="name email" FRED_API_KEY=... TUSHARE_TOKEN=... pytest -m ""
"""

import os

import pytest

RUN = os.environ.get("RUN_INTEGRATION", "") not in ("", "0", "false")
pytestmark = pytest.mark.skipif(not RUN, reason="真联测需生产网络（RUN_INTEGRATION=1 启用）")


def _skip_if_gateway_denied(result):
    if not result.ok and ("CONNECT" in result.error or "403" in result.error or "URLError" in result.error):
        pytest.skip(f"网络策略拒绝：{result.error}")


def test_edgar_live_companyfacts():
    from invest_assistant.data.adapters.edgar import EdgarAdapter

    a = EdgarAdapter()
    r = a.probe()
    _skip_if_gateway_denied(r)
    assert r.ok, r.error
    rows = a.fetch({"kind": "companyfacts", "cik": "1045810"})  # NVDA
    assert any(row["field"].endswith("Revenues") or "Revenue" in row["field"] for row in rows)


def test_fred_live_dfii10():
    from invest_assistant.data.adapters.fred import FredAdapter

    a = FredAdapter()
    r = a.probe()
    _skip_if_gateway_denied(r)
    if not r.ok and "FRED_API_KEY" in r.error:
        pytest.skip("FRED_API_KEY 未配置")
    rows = a.fetch({"series_id": "DFII10", "start": "2026-06-01"})
    assert rows and all(isinstance(x["value"], float) for x in rows)


def test_tushare_live_coverage():
    from invest_assistant.data.adapters.tushare import TushareAdapter

    a = TushareAdapter()
    r = a.probe()
    if not r.ok and "TUSHARE_TOKEN" in r.error:
        pytest.skip("TUSHARE_TOKEN 未配置")
    _skip_if_gateway_denied(r)
    assert "report_rc" in r.coverage  # 覆盖率必须登记（R1.3，决策 1 复议数据）


def test_registry_startup_matrix_live():
    from invest_assistant.data.adapters.misc import build_default_registry

    allow, results = build_default_registry().startup_self_check()
    assert len(results) == 9  # §2.3 全清单
    # 不断言 allow——生产环境应 True；此断言留给部署验收清单
