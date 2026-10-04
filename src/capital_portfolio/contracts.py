"""投资项目、预算、资金来源与风险证据的基础契约。

契约保持向后兼容：``BudgetEnvelope(year, amount)`` 与
``ProjectVersion(project_id, version, asset_ids, estimated_cost)``
的既有位置参数用法不变，新增能力全部通过带默认值的关键字参数提供。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

MIN_YEAR = 2026


@dataclass(frozen=True)
class BudgetEnvelope:
    """年度预算 envelope（某一年度的总盘，币种最小单位整数记账）。"""

    year: int
    amount: int

    def __post_init__(self) -> None:
        if not isinstance(self.year, int) or self.year < MIN_YEAR:
            raise ValueError("预算年度或金额无效")
        if not isinstance(self.amount, int) or self.amount < 0:
            raise ValueError("预算年度或金额无效")


@dataclass(frozen=True)
class RiskEvidence:
    """风险证据：类别、1-5 级严重度、来源与观测日期（ISO 日期字符串）。"""

    evidence_id: str
    category: str
    severity: int
    source: str
    observed_on: str

    def __post_init__(self) -> None:
        if not self.evidence_id or not self.category:
            raise ValueError("风险证据缺少标识或类别")
        if not isinstance(self.severity, int) or not 1 <= self.severity <= 5:
            raise ValueError("风险严重度必须在 1..5 之间")
        if not self.source or not self.observed_on:
            raise ValueError("风险证据缺少来源或观测日期")


@dataclass(frozen=True)
class Dependency:
    """前置依赖：``project_id`` 必须先完工，可带 ``lag_years`` 年滞后。"""

    project_id: str
    lag_years: int = 0

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("依赖项目标识为空")
        if self.lag_years < 0:
            raise ValueError("依赖滞后期不能为负")


@dataclass(frozen=True)
class FundingCommitment:
    """项目对某资金来源的占用比例（share∈(0,1]，各来源合计不超过 1）。"""

    source_id: str
    share: float

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("资金来源标识为空")
        if not 0 < self.share <= 1 + 1e-9:
            raise ValueError("资金来源占用比例必须在 (0,1] 之间")


@dataclass(frozen=True)
class FundingPool:
    """资金来源总盘。year=None 表示跨多年度总池，否则为年度专池。"""

    source_id: str
    amount: int
    year: int | None = None

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("资金来源标识为空")
        if not isinstance(self.amount, int) or self.amount < 0:
            raise ValueError("资金池金额无效")
        if self.year is not None and self.year < MIN_YEAR:
            raise ValueError("资金池年度无效")


@dataclass(frozen=True)
class ProjectVersion:
    """改造项目版本。

    - asset_ids：资产范围（共同管段标识）
    - cost_curve：相对开工年的成本曲线 {偏移年: 金额}，缺省为单年完工
    - risk_evidence：风险证据
    - depends_on：前置依赖（字符串标识或 Dependency）
    - funding_sources：资金来源及占用比例
    - mutex_groups：互斥备选组（同组项目在同一方案中不可共存）
    - benefit_by_asset：按管段申报的收益（货币化），键必须在资产范围内
    - mileage：里程（米），用于实际完成量回填
    """

    project_id: str
    version: int
    asset_ids: tuple[str, ...]
    estimated_cost: int
    name: str = ""
    risk_evidence: tuple[RiskEvidence, ...] = ()
    depends_on: tuple[Dependency, ...] = ()
    funding_sources: tuple[FundingCommitment, ...] = ()
    mutex_groups: tuple[str, ...] = ()
    benefit_by_asset: Mapping[str, int] = field(default_factory=dict)
    cost_curve: Mapping[int, int] = field(default_factory=dict)
    mileage: float = 0.0
    base_year: int = 2027

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("项目版本信息不完整")
        if not isinstance(self.version, int) or self.version < 1:
            raise ValueError("项目版本信息不完整")
        if not self.asset_ids:
            raise ValueError("项目版本信息不完整")
        if not isinstance(self.estimated_cost, int) or self.estimated_cost <= 0:
            raise ValueError("项目版本信息不完整")

        assets: list[str] = []
        for asset in self.asset_ids:
            if not asset:
                raise ValueError("资产范围包含空标识")
            if asset not in assets:
                assets.append(asset)
        object.__setattr__(self, "asset_ids", tuple(assets))

        risks = tuple(self.risk_evidence)
        object.__setattr__(self, "risk_evidence", risks)

        deps: list[Dependency] = []
        for dep in self.depends_on:
            if isinstance(dep, str):
                dep = Dependency(dep)
            elif not isinstance(dep, Dependency):
                raise ValueError("前置依赖必须是项目标识或 Dependency")
            if dep.project_id == self.project_id:
                raise ValueError("项目不能依赖自身")
            deps.append(dep)
        object.__setattr__(self, "depends_on", tuple(deps))

        funds = tuple(self.funding_sources)
        if sum(f.share for f in funds) > 1 + 1e-9:
            raise ValueError("资金来源占用比例合计超过 1")
        object.__setattr__(self, "funding_sources", funds)

        groups = tuple(dict.fromkeys(self.mutex_groups))
        object.__setattr__(self, "mutex_groups", groups)

        benefit = {str(k): int(v) for k, v in dict(self.benefit_by_asset).items()}
        if any(v < 0 for v in benefit.values()):
            raise ValueError("申报收益不能为负")
        if any(k not in assets for k in benefit):
            raise ValueError("收益申报管段超出项目资产范围")
        object.__setattr__(self, "benefit_by_asset", benefit)

        curve = {int(k): int(v) for k, v in dict(self.cost_curve).items()}
        if not curve:
            curve = {0: self.estimated_cost}
        if any(k < 0 or v <= 0 for k, v in curve.items()):
            raise ValueError("成本曲线偏移年与金额无效")
        if sum(curve.values()) != self.estimated_cost:
            raise ValueError("成本曲线金额合计必须等于估算总成本")
        object.__setattr__(self, "cost_curve", dict(sorted(curve.items())))

        if self.mileage < 0:
            raise ValueError("里程不能为负")
        if self.base_year < MIN_YEAR:
            raise ValueError("最早开工年度无效")

    # ---- 排程派生 ----
    @property
    def duration_years(self) -> int:
        return max(self.cost_curve) + 1

    @property
    def risk_points(self) -> int:
        """风险证据严重度合计，用于优先级排序。"""
        return sum(e.severity for e in self.risk_evidence)

    @property
    def claimed_benefit(self) -> int:
        """按管段申报的收益合计（多项目共段时可能被重复计算）。"""
        return sum(self.benefit_by_asset.values())

    def scheduled_curve(self, start_year: int) -> dict[int, int]:
        """把相对成本曲线平移到绝对年度。"""
        if start_year < self.base_year:
            raise ValueError("开工年度早于项目最早可行年度")
        return {start_year + offset: amount for offset, amount in self.cost_curve.items()}

    def construction_years(self, start_year: int) -> tuple[int, ...]:
        return tuple(sorted(self.scheduled_curve(start_year)))

    def end_year(self, start_year: int) -> int:
        return max(self.scheduled_curve(start_year))
