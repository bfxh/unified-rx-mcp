"""conftest.py —— pytest 全局配置：tmp 基目录在 %TEMP%\\unified-rx-pytest，
并把该前缀加入沙盒放行（保持 fail-closed 语义：仅此显式白名单 + 项目根）。

不再把 _pytest_tmp 放仓库根（UPGRADE-A2）：夹具残留会污染 bug_scan 自扫与 git。
"""
import os
import tempfile

# pytest 默认跑在 fail-closed 沙盒内：未设置时 = 项目根 + 专用 tmp 前缀
# （runner 上真实工作区不存在，用 checkout 根替代——等价且可移植）
_TMP_BASE = os.path.join(tempfile.gettempdir(), "unified-rx-pytest")
os.makedirs(_TMP_BASE, exist_ok=True)
_WORKSPACE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("UNIFIED_RX_SANDBOX", os.pathsep.join([
    _WORKSPACE,           # 真实工作区（本机=D:\开发，CI=checkout 根）
    _TMP_BASE,            # pytest 夹具专用前缀（进程内显式授权）
]))
os.environ["UNIFIED_RX_SANDBOX"] = os.environ["UNIFIED_RX_SANDBOX"].replace(os.pathsep, ";")

# S122：套件里有大量"故意重复同一工具+参数"的测试（缓存命中/压力/稳定性），
# 熔断器默认旁路；熔断自身行为由 tests/test_s122_breaker.py 显式开启验证。
os.environ["UNIFIED_RX_BREAKER"] = "off"

# S140：全局护栏在套件里默认高阈值/关闭（与 UNIFIED_RX_BREAKER=off 同理），
# 全局 QPM/日告警行为由 tests/test_s122_breaker.py 显式开启验证。
os.environ.setdefault("UNIFIED_RX_GLOBAL_QPM", "1000000")
os.environ.setdefault("UNIFIED_RX_DAILY_ALERT", "0")
# S141：会话烧量哨兵默认关（专项测试显式开——否则跑套件期间会读真实 rollout 目录）
os.environ.setdefault("UNIFIED_RX_BURN_MB", "0")

tempfile.tempdir = _TMP_BASE


def pytest_configure(config):
    config.option.basetemp = _TMP_BASE


import pytest

# S140：测试打点落点——放 basetemp 下的独立目录，而不是 tmp_path：
# 一批测试直接把 tmp_path 当语料根做全量对账，塞进 stats.jsonl 会多出 1 个条目。
_STATS_TMP = os.path.join(_TMP_BASE, "_stats")
os.makedirs(_STATS_TMP, exist_ok=True)

# S151：常驻服务在套件里默认**关闭**——测试走 spawn 路径（与历史行为一致），
# 服务本身由 tests/test_s151_svc.py 显式开启并逐字节对账。
os.environ.setdefault("UNIFIED_RX_SVC", "off")

# S146：握手留痕同样隔离——tests/test_v2 直接 _handle(initialize) 会把空 params
# 裸写进真实 ~/.unified-rx/clients.jsonl（首轮留痕 4 条 null 全来自测试，实锤
# 归因困难）。审计账本只许装真实宿主握手，测试一律写 tmp。
_CLIENTS_TMP = os.path.join(_STATS_TMP, "clients.jsonl")
os.environ["UNIFIED_RX_CLIENTS_LOG"] = _CLIENTS_TMP


@pytest.fixture(autouse=True)
def _isolate_stats(monkeypatch):
    """S140：测试打点一律落 tmp 隔离区——套件/bench 的 registry.call 不再写进
    真实 ~/.unified-rx/stats.jsonl（9/8 统计污染的教训之一）。
    S141：日计数落点同样隔离（熔断开启的测试不碰真实 daily_state.jsonl）。"""
    import registry
    target = os.path.join(_STATS_TMP, "stats.jsonl")
    monkeypatch.setattr(registry, "_stats_path", lambda: target)
    try:
        from tools import breaker as _breaker
        monkeypatch.setattr(_breaker, "_daily_path",
                            lambda: os.path.join(_STATS_TMP, "daily_state.jsonl"))
    except Exception:                                              # noqa: BLE001
        pass
