"""适配器框架与启动前置自检（任务 4.1，需求 R1.1/R1.2）。"""

from invest_assistant.data.adapters import AdapterRegistry, BaseAdapter, ConnectivityMatrix, ProbeResult


class _FakeAdapter(BaseAdapter):
    def __init__(self, name, ok, required=True):
        self.name, self._ok, self.required_in_production = name, ok, required

    def probe(self) -> ProbeResult:
        return ProbeResult(adapter=self.name, ok=self._ok, error="" if self._ok else "连接超时")

    def fetch(self, request):
        return []


def test_startup_self_check_all_green():
    reg = AdapterRegistry()
    for n in ("ADP-EDGAR", "ADP-FRED", "ADP-TUSHARE"):
        reg.register(_FakeAdapter(n, ok=True))
    allow, results = reg.startup_self_check()
    assert allow and len(results) == 3


def test_required_source_failure_blocks_opinion_mode():
    """§2.3 清单内任一必需源失败 → 拒绝进入意见生成模式 + 降级声明（R1.2）。"""
    reg = AdapterRegistry()
    reg.register(_FakeAdapter("ADP-EDGAR", ok=True))
    reg.register(_FakeAdapter("ADP-CNINFO", ok=False))  # 巨潮探测失败
    allow, results = reg.startup_self_check()
    assert not allow
    matrix = ConnectivityMatrix(results=results)
    assert not matrix.opinion_mode_allowed
    assert any("ADP-CNINFO" in s for s in matrix.degraded_statement())


def test_probe_exception_lands_in_matrix_not_crash():
    class _Boom(BaseAdapter):
        name = "ADP-BOOM"

        def probe(self):
            raise RuntimeError("网关 500")

        def fetch(self, request):
            return []

    reg = AdapterRegistry()
    reg.register(_Boom())
    allow, results = reg.startup_self_check()
    assert not allow and results[0].error == "网关 500"
