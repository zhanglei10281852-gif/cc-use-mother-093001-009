"""管网改造投资组合治理的领域模型。

所有金额单位为元(整数)，日期为 ISO 字符串，便于 JSON 持久化与审计复核。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ScenarioStatus(str, Enum):
    DRAFT = "DRAFT"            # 草拟，可调整假设并重新求解
    SUBMITTED = "SUBMITTED"    # 已提交审批
    APPROVED = "APPROVED"      # 审批通过，可发布为基线
    PUBLISHED = "PUBLISHED"    # 已发布为基线
    REJECTED = "REJECTED"      # 审批退回


class ChangeOrderStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    APPLIED = "APPLIED"        # 已应用到基线，产生新基线修订
    REJECTED = "REJECTED"


class ChangeOrderType(str, Enum):
    RESCHEDULE = "RESCHEDULE"            # 调整开工年
    REPLACE_VERSION = "REPLACE_VERSION"  # 替换项目版本
    ADD_ITEM = "ADD_ITEM"                # 新增入选项目
    REMOVE_ITEM = "REMOVE_ITEM"          # 移除入选项目
    REALLOCATE = "REALLOCATE"            # 调整年度资金来源


class Decision(str, Enum):
    INCLUDED = "INCLUDED"
    EXCLUDED = "EXCLUDED"


class ReasonCode(str, Enum):
    LOCKED_INCLUDE = "LOCKED_INCLUDE"          # 评审人锁定入选
    LOCKED_EXCLUDE = "LOCKED_EXCLUDE"          # 评审人锁定落选
    SELECTED = "SELECTED"                      # 按优先级得分入选
    BUDGET_INSUFFICIENT = "BUDGET_INSUFFICIENT"  # 任一年度资金不足
    DEPENDENCY_UNMET = "DEPENDENCY_UNMET"      # 前置项目未入选或来不及完成
    EXCLUSION_CONFLICT = "EXCLUSION_CONFLICT"  # 互斥施工冲突
    OVERLAP_CONFLICT = "OVERLAP_CONFLICT"      # 管段范围重叠(禁止策略下)
    WINDOW_INFEASIBLE = "WINDOW_INFEASIBLE"    # 规划窗口内无法安排
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"      # 依赖成环


# 互斥范围
EXCLUSION_SAME_YEAR = "same_year"    # 施工窗口不得重叠
EXCLUSION_PORTFOLIO = "portfolio"    # 同一组合中不得同时入选

# 重叠处理策略
OVERLAP_DEDUPE = "dedupe"  # 允许入选，收益去重
OVERLAP_FORBID = "forbid"  # 禁止重叠项目同时入选


# ---------------------------------------------------------------------------
# 项目版本
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskEvidence:
    """风险证据：某类风险的严重度×可能性及佐证材料。"""
    risk_type: str        # 如 泄漏/腐蚀/爆管/塌陷
    severity: int         # 1-5
    likelihood: int       # 1-5
    evidence_ref: str     # 检测报告/事故记录编号
    assessed_at: str      # 评估日期 ISO

    @property
    def score(self) -> int:
        return self.severity * self.likelihood

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "RiskEvidence":
        return RiskEvidence(**d)


@dataclass(frozen=True)
class BenefitClaim:
    """收益申报：某管段上某指标的预计收益量。"""
    segment_id: str
    metric: str           # 如 leak_reduction_m3 / risk_events_avoided / people_served
    value: float

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "BenefitClaim":
        return BenefitClaim(**d)


@dataclass(frozen=True)
class ProjectVersionRecord:
    """改造项目的一个申报版本。"""
    project_id: str
    version: int
    name: str
    segments: dict[str, float]              # 资产范围: 管段 -> 改造长度(米)
    cost_curve: tuple[int, ...]             # 成本曲线: 自开工年起各年度成本(元)
    earliest_start: int                     # 最早开工年
    risk_evidence: tuple[RiskEvidence, ...]
    prerequisites: tuple[str, ...]          # 前置依赖项目
    funding_sources: tuple[str, ...]        # 可用资金来源(按优先顺序)
    benefits: tuple[BenefitClaim, ...]
    exclusive_with: tuple[tuple[str, str], ...]  # (项目, 互斥范围)
    submitted_at: str
    submitted_by: str = ""

    @property
    def total_cost(self) -> int:
        return sum(self.cost_curve)

    @property
    def duration(self) -> int:
        return len(self.cost_curve)

    @property
    def scope_length_m(self) -> float:
        return sum(self.segments.values())

    @property
    def risk_score(self) -> int:
        return sum(e.score for e in self.risk_evidence)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cost_curve"] = list(self.cost_curve)
        d["prerequisites"] = list(self.prerequisites)
        d["funding_sources"] = list(self.funding_sources)
        d["exclusive_with"] = [list(x) for x in self.exclusive_with]
        return d

    @staticmethod
    def from_dict(d: dict) -> "ProjectVersionRecord":
        return ProjectVersionRecord(
            project_id=d["project_id"], version=int(d["version"]), name=d["name"],
            segments={k: float(v) for k, v in d["segments"].items()},
            cost_curve=tuple(int(x) for x in d["cost_curve"]),
            earliest_start=int(d["earliest_start"]),
            risk_evidence=tuple(RiskEvidence.from_dict(x) for x in d.get("risk_evidence", [])),
            prerequisites=tuple(d.get("prerequisites", [])),
            funding_sources=tuple(d.get("funding_sources", [])),
            benefits=tuple(BenefitClaim.from_dict(x) for x in d.get("benefits", [])),
            exclusive_with=tuple((x[0], x[1]) for x in d.get("exclusive_with", [])),
            submitted_at=d["submitted_at"], submitted_by=d.get("submitted_by", ""),
        )


@dataclass(frozen=True)
class FundingSource:
    """资金来源及其年度额度。"""
    source_id: str
    name: str
    annual_caps: dict[int, int]   # 年度 -> 额度(元)
    updated_at: str

    def to_dict(self) -> dict:
        return {"source_id": self.source_id, "name": self.name,
                "annual_caps": {str(k): v for k, v in self.annual_caps.items()},
                "updated_at": self.updated_at}

    @staticmethod
    def from_dict(d: dict) -> "FundingSource":
        return FundingSource(source_id=d["source_id"], name=d["name"],
                             annual_caps={int(k): int(v) for k, v in d["annual_caps"].items()},
                             updated_at=d["updated_at"])


# ---------------------------------------------------------------------------
# 情景(候选方案)
# ---------------------------------------------------------------------------

# 假设类型
ASSUME_INCLUDE = "INCLUDE"              # 锁定入选
ASSUME_EXCLUDE = "EXCLUDE"              # 锁定落选
ASSUME_PIN_START = "PIN_START"          # 锁定开工年
ASSUME_CAP_OVERRIDE = "CAP_OVERRIDE"    # 资金额度假设
ASSUME_PRIORITY = "PRIORITY"            # 优先级倍率调整


@dataclass(frozen=True)
class Assumption:
    """评审人锁定的假设。"""
    assumption_id: str
    kind: str
    project_id: str | None
    value: dict            # PIN_START: {"start_year":..}; PRIORITY: {"multiplier":..};
                           # CAP_OVERRIDE: {"source":..,"year":..,"cap":..}
    note: str
    locked_by: str
    locked_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Assumption":
        return Assumption(**d)


@dataclass(frozen=True)
class ScenarioItem:
    """入选项目及其排程与资金安排。"""
    project_id: str
    version: int
    start_year: int
    funding_plan: dict[int, dict[str, int]]  # 年度 -> 资金来源 -> 占用金额
    score: float
    locked: bool = False

    def to_dict(self) -> dict:
        return {"project_id": self.project_id, "version": self.version,
                "start_year": self.start_year,
                "funding_plan": {str(y): dict(s) for y, s in self.funding_plan.items()},
                "score": self.score, "locked": self.locked}

    @staticmethod
    def from_dict(d: dict) -> "ScenarioItem":
        return ScenarioItem(
            project_id=d["project_id"], version=int(d["version"]),
            start_year=int(d["start_year"]),
            funding_plan={int(y): {s: int(a) for s, a in srcs.items()}
                          for y, srcs in d["funding_plan"].items()},
            score=float(d["score"]), locked=bool(d.get("locked", False)))


@dataclass(frozen=True)
class DecisionRecord:
    """每个项目的入选/落选决定及可解释原因。"""
    project_id: str
    decision: str
    reason_code: str
    reason: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "DecisionRecord":
        return DecisionRecord(**d)


@dataclass(frozen=True)
class ScenarioRecord:
    """跨年度候选方案(情景)的一个修订。"""
    scenario_id: str
    revision: int
    name: str
    horizon: tuple[int, int]                 # 规划窗口(起止年, 含)
    status: str
    created_by: str
    created_at: str
    valid_until: str                         # 有效期, 过期不能直接发布
    assumptions: tuple[Assumption, ...]
    items: tuple[ScenarioItem, ...]
    decisions: tuple[DecisionRecord, ...]
    overlaps: tuple[dict, ...]               # 范围重叠与收益冲突清单
    benefit_total: float
    benefit_by_metric: dict[str, float]
    config: dict                             # 求解参数快照(可复核)
    notes: str = ""

    def item_map(self) -> dict[str, ScenarioItem]:
        return {i.project_id: i for i in self.items}

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id, "revision": self.revision, "name": self.name,
            "horizon": list(self.horizon), "status": self.status,
            "created_by": self.created_by, "created_at": self.created_at,
            "valid_until": self.valid_until,
            "assumptions": [a.to_dict() for a in self.assumptions],
            "items": [i.to_dict() for i in self.items],
            "decisions": [d.to_dict() for d in self.decisions],
            "overlaps": list(self.overlaps),
            "benefit_total": self.benefit_total,
            "benefit_by_metric": dict(self.benefit_by_metric),
            "config": dict(self.config), "notes": self.notes,
        }

    @staticmethod
    def from_dict(d: dict) -> "ScenarioRecord":
        return ScenarioRecord(
            scenario_id=d["scenario_id"], revision=int(d["revision"]), name=d["name"],
            horizon=(int(d["horizon"][0]), int(d["horizon"][1])), status=d["status"],
            created_by=d["created_by"], created_at=d["created_at"],
            valid_until=d["valid_until"],
            assumptions=tuple(Assumption.from_dict(x) for x in d.get("assumptions", [])),
            items=tuple(ScenarioItem.from_dict(x) for x in d.get("items", [])),
            decisions=tuple(DecisionRecord.from_dict(x) for x in d.get("decisions", [])),
            overlaps=tuple(d.get("overlaps", [])),
            benefit_total=float(d.get("benefit_total", 0.0)),
            benefit_by_metric={k: float(v) for k, v in d.get("benefit_by_metric", {}).items()},
            config=dict(d.get("config", {})), notes=d.get("notes", ""))


# ---------------------------------------------------------------------------
# 基线 / 调整单 / 实际回填 / 预警
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaselineRecord:
    """审批发布的投资基线，仅能通过正式调整单变更。"""
    baseline_id: str
    revision: int
    scenario_id: str
    scenario_revision: int
    approved_by: str
    published_at: str
    items: tuple[ScenarioItem, ...]
    funding_snapshot: dict                   # source -> {year: cap} 发布时快照
    benefit_total: float
    change_orders_applied: tuple[str, ...] = ()

    def item_map(self) -> dict[str, ScenarioItem]:
        return {i.project_id: i for i in self.items}

    def to_dict(self) -> dict:
        return {"baseline_id": self.baseline_id, "revision": self.revision,
                "scenario_id": self.scenario_id, "scenario_revision": self.scenario_revision,
                "approved_by": self.approved_by, "published_at": self.published_at,
                "items": [i.to_dict() for i in self.items],
                "funding_snapshot": self.funding_snapshot,
                "benefit_total": self.benefit_total,
                "change_orders_applied": list(self.change_orders_applied)}

    @staticmethod
    def from_dict(d: dict) -> "BaselineRecord":
        return BaselineRecord(
            baseline_id=d["baseline_id"], revision=int(d["revision"]),
            scenario_id=d["scenario_id"], scenario_revision=int(d["scenario_revision"]),
            approved_by=d["approved_by"], published_at=d["published_at"],
            items=tuple(ScenarioItem.from_dict(x) for x in d.get("items", [])),
            funding_snapshot={s: {int(y): int(a) for y, a in years.items()}
                              for s, years in d.get("funding_snapshot", {}).items()},
            benefit_total=float(d.get("benefit_total", 0.0)),
            change_orders_applied=tuple(d.get("change_orders_applied", [])))


@dataclass(frozen=True)
class ChangeOrderRecord:
    """正式调整单：基线变更的唯一途径。"""
    change_order_id: str
    baseline_id: str
    type: str
    payload: dict
    reason: str
    status: str
    created_by: str
    created_at: str
    decided_by: str = ""
    decided_at: str = ""
    decision_note: str = ""
    validation: tuple[str, ...] = ()         # 应用前一致性校验结果

    def to_dict(self) -> dict:
        d = asdict(self)
        d["validation"] = list(self.validation)
        return d

    @staticmethod
    def from_dict(d: dict) -> "ChangeOrderRecord":
        return ChangeOrderRecord(
            change_order_id=d["change_order_id"], baseline_id=d["baseline_id"],
            type=d["type"], payload=dict(d["payload"]), reason=d["reason"],
            status=d["status"], created_by=d["created_by"], created_at=d["created_at"],
            decided_by=d.get("decided_by", ""), decided_at=d.get("decided_at", ""),
            decision_note=d.get("decision_note", ""),
            validation=tuple(d.get("validation", [])))


@dataclass(frozen=True)
class ActualEntry:
    """实际支出与里程完成量的滚动回填记录。"""
    entry_id: str
    baseline_id: str
    project_id: str
    period: str                 # "2027" 或 "2027-Q1"
    actual_spend: int
    mileage_completed_m: float
    source: str                 # 数据来源(计量系统/月报...)
    note: str
    recorded_at: str
    superseded: bool = False    # 滚动回填时被更新记录取代

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ActualEntry":
        return ActualEntry(**d)


@dataclass(frozen=True)
class AlertRecord:
    """偏差预警。"""
    alert_id: str
    baseline_id: str
    project_id: str
    period: str
    alert_type: str             # COST_DEVIATION / MILEAGE_LAG
    severity: str               # WARNING / CRITICAL
    planned: float
    actual: float
    deviation_pct: float
    message: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "AlertRecord":
        return AlertRecord(**d)


# ---------------------------------------------------------------------------
# 领域异常
# ---------------------------------------------------------------------------

class DomainError(Exception):
    """业务规则冲突(对应 HTTP 422)。"""


class NotFoundError(DomainError):
    """对象不存在(对应 HTTP 404)。"""


class ConflictError(DomainError):
    """状态冲突, 如过期情景发布(对应 HTTP 409)。"""


class InfeasibleError(DomainError):
    """锁定假设相互矛盾导致求解不可行。"""
