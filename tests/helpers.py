"""测试共享辅助：固定时钟的服务实例与项目构造器。"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_portfolio.domain import ProjectVersionRecord, RiskEvidence  # noqa: E402
from capital_portfolio.seed import seed  # noqa: E402
from capital_portfolio.service import PortfolioService  # noqa: E402
from capital_portfolio.store import Store  # noqa: E402


class Clock:
    def __init__(self, day: date):
        self.day = day

    def __call__(self) -> date:
        return self.day


def make_service(today: date = date(2027, 1, 15), seeded: bool = True):
    clock = Clock(today)
    svc = PortfolioService(Store(":memory:"), clock=clock)
    if seeded:
        seed(svc)
    return svc, clock


def make_project(pid: str, cost: list[int], earliest_start: int = 2027,
                 prereqs: tuple = (), sources: tuple = ("F1",),
                 segments: dict | None = None, benefits: tuple = (),
                 exclusive: tuple = (), risk_score: int = 10,
                 version: int = 1) -> ProjectVersionRecord:
    """构造单元测试用项目版本。"""
    return ProjectVersionRecord(
        project_id=pid, version=version, name=f"项目{pid}",
        segments=segments or {f"SEG-{pid}": 1000.0},
        cost_curve=tuple(cost), earliest_start=earliest_start,
        risk_evidence=(RiskEvidence("腐蚀", risk_score, 1, "T-1", "2026-01-01"),),
        prerequisites=prereqs, funding_sources=sources, benefits=benefits,
        exclusive_with=exclusive, submitted_at="2027-01-01")
