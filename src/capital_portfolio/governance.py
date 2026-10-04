"""情景治理：锁定假设、提交审批、发布基线、正式调整单与滚动实际值。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from .engine import PlanResult, PlanWeights, build_plan, PlanError
from .contracts import FundingPool, ProjectVersion

STATUS_DRAFT = "draft"
STATUS_SUBMITTED = "submitted"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

ADJ_DRAFT = "draft"
ADJ_SUBMITTED = "submitted"
ADJ_APPROVED = "approved"
ADJ_REJECTED = "rejected"


class GovernanceError(ValueError):
    """情景/基线/调整单生命周期或一致性规则被违反。"""

    def __init__(self, message: str, violations: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.violations = list(violations)


@dataclass
class ScenarioAssumptions:
    """评审人可锁定的情景假设。"""

    name: str
    weights: PlanWeights = field(default_factory=PlanWeights)
    forced_in: dict[str, int | None] = field(default_factory=dict)
    excluded: list[str] = field(default_factory=list)
    notes: str = ""
    created_on: str = ""
    expires_on: str = ""

    def is_expired(self, as_of: date | None = None) -> bool:
        if not self.expires_on:
            return False
        as_of = as_of or date.today()
        return date.fromisoformat(self.expires_on) < as_of


@dataclass
class Scenario:
    scenario_id: str
    assumptions: ScenarioAssumptions
    status: str = STATUS_DRAFT
    plan: PlanResult | None = None
    baseline_id: str | None = None
    parent_scenario_id: str | None = None
    revision_of_baseline: int | None = None

    def _require_editable(self) -> None:
        if self.status != STATUS_DRAFT:
            raise GovernanceError(f"情景 {self.scenario_id} 状态为 {self.status}，不可修改")
        if self.assumptions.is_expired():
            self.status = STATUS_EXPIRED
            raise GovernanceError(f"情景 {self.scenario_id} 已过期，请复制为新情景后再调整")

    def set_priority_boost(self, project_id: str, boost: float) -> None:
        self._require_editable()
        weights = self.assumptions.weights
        manual = dict(weights.manual_boost)
        if boost == 0:
            manual.pop(project_id, None)
        else:
            manual[project_id] = float(boost)
        self.assumptions.weights = PlanWeights(
            risk_value_per_point=weights.risk_value_per_point,
            enablement_bonus_per_dependent=weights.enablement_bonus_per_dependent,
            manual_boost=manual,
        )

    def set_weight(self, field_name: str, value: float) -> None:
        self._require_editable()
        if field_name not in ("risk_value_per_point", "enablement_bonus_per_dependent"):
            raise GovernanceError(f"未知权重 {field_name}")
        if value < 0:
            raise GovernanceError("权重不能为负")
        w = self.assumptions.weights
        self.assumptions.weights = PlanWeights(
            risk_value_per_point=int(value) if field_name == "risk_value_per_point"
            else w.risk_value_per_point,
            enablement_bonus_per_dependent=int(value)
            if field_name == "enablement_bonus_per_dependent"
            else w.enablement_bonus_per_dependent,
            manual_boost=dict(w.manual_boost),
        )

    def lock_in(self, project_id: str, start_year: int | None = None) -> None:
        """锁定入选假设；start_year=None 只锁入选不锁年度。"""
        self._require_editable()
        if project_id in self.assumptions.excluded:
            self.assumptions.excluded.remove(project_id)
        self.assumptions.forced_in[project_id] = start_year

    def lock_exclusion(self, project_id: str) -> None:
        self._require_editable()
        self.assumptions.forced_in.pop(project_id, None)
        if project_id not in self.assumptions.excluded:
            self.assumptions.excluded.append(project_id)

    def unlock(self, project_id: str) -> None:
        self._require_editable()
        self.assumptions.forced_in.pop(project_id, None)
        if project_id in self.assumptions.excluded:
            self.assumptions.excluded.remove(project_id)

    def recompute(
        self,
        projects: Sequence[ProjectVersion],
        budgets: Mapping[int, int],
        funding_pools: Sequence[FundingPool],
    ) -> PlanResult:
        if self.status == STATUS_EXPIRED or self.assumptions.is_expired():
            self.status = STATUS_EXPIRED
            raise GovernanceError(f"情景 {self.scenario_id} 已过期，不能重新计算")
        try:
            self.plan = build_plan(
                projects, budgets, funding_pools,
                weights=self.assumptions.weights,
                forced_in=self.assumptions.forced_in,
                excluded=self.assumptions.excluded,
            )
        except PlanError as exc:
            raise GovernanceError(str(exc), getattr(exc, "violations", [])) from exc
        return self.plan

    def submit(self, as_of: date | None = None) -> None:
        if self.status != STATUS_DRAFT:
            raise GovernanceError("只有草稿情景可以提交审批")
        if self.assumptions.is_expired(as_of):
            self.status = STATUS_EXPIRED
            raise GovernanceError("情景已超过有效期，不能提交；请复制为新情景")
        if self.plan is None:
            raise GovernanceError("情景尚未计算候选方案，不能提交")
        self.status = STATUS_SUBMITTED

    def reject(self, note: str = "") -> None:
        if self.status != STATUS_SUBMITTED:
            raise GovernanceError("只有已提交情景可以驳回")
        self.status = STATUS_REJECTED

    def return_to_draft(self) -> None:
        if self.status not in (STATUS_REJECTED, STATUS_SUBMITTED):
            raise GovernanceError("只有已提交/被驳回情景可以退回草稿")
        self.status = STATUS_DRAFT


@dataclass
class Baseline:
    """审批发布的基线；只能通过正式调整单变更。"""

    baseline_id: str
    revision: int
    approved_on: str
    scenario_id: str
    schedule: dict[str, int] = field(default_factory=dict)
    project_versions: dict[str, int] = field(default_factory=dict)
    budgets: dict[int, int] = field(default_factory=dict)
    budget_committed: dict[int, float] = field(default_factory=dict)
    funding_limits: dict[str, float] = field(default_factory=dict)
    funding_committed: dict[str, float] = field(default_factory=dict)
    recognized_benefit: dict[str, int] = field(default_factory=dict)
    adjustments: list[dict] = field(default_factory=list)
    actual_spend: dict[str, int] = field(default_factory=dict)       # "PID@YEAR"
    actual_mileage: dict[str, float] = field(default_factory=dict)   # "PID@YEAR"

    def key(self, project_id: str, year: int) -> str:
        return f"{project_id}@{year}"

    @property
    def budget_remaining(self) -> dict[int, int]:
        return {
            y: int(self.budgets[y] - self.budget_committed.get(y, 0.0))
            for y in sorted(self.budgets)
        }

    @property
    def funding_remaining(self) -> dict[str, int]:
        return {
            k: int(limit - self.funding_committed.get(k, 0.0))
            for k, limit in self.funding_limits.items()
        }


def publish_baseline(
    scenario: Scenario,
    projects_by_id: Mapping[str, ProjectVersion],
    baseline_id: str,
    approved_on: str,
) -> Baseline:
    """把已提交情景发布为基线。过期情景一律拒绝。"""
    if scenario.assumptions.is_expired(date.fromisoformat(approved_on)):
        scenario.status = STATUS_EXPIRED
        raise GovernanceError("情景已超过有效期，不能发布为基线")
    if scenario.status != STATUS_SUBMITTED:
        raise GovernanceError("只有已提交、在有效期内的情景可以发布")
    plan = scenario.plan
    assert plan is not None

    baseline = Baseline(
        baseline_id=baseline_id,
        revision=1,
        approved_on=approved_on,
        scenario_id=scenario.scenario_id,
        schedule={d.project_id: d.start_year for d in plan.selected},
        project_versions={
            d.project_id: projects_by_id[d.project_id].version for d in plan.selected
        },
        budgets={y: u.limit for y, u in plan.budget_usage.items()},
        budget_committed={y: u.committed for y, u in plan.budget_usage.items()},
        funding_limits={k: u.limit for k, u in plan.funding_usage.items()},
        funding_committed={k: u.committed for k, u in plan.funding_usage.items()},
        recognized_benefit=dict(plan.recognized_by_project),
    )
    scenario.status = STATUS_APPROVED
    scenario.baseline_id = baseline_id
    scenario.revision_of_baseline = 1
    return baseline


@dataclass
class AdjustmentItem:
    action: str  # add | remove | retime
    project_id: str
    start_year: int | None = None
    reason: str = ""


@dataclass
class AdjustmentOrder:
    """正式调整单：审批通过后产生基线新版本。"""

    order_id: str
    baseline_revision: int
    items: list[AdjustmentItem]
    rationale: str = ""
    status: str = ADJ_DRAFT
    cascade_removed: list[str] = field(default_factory=list)
    applied_on: str = ""

    def submit(self) -> None:
        if self.status != ADJ_DRAFT:
            raise GovernanceError("只有草稿调整单可以提交")
        if not self.items:
            raise GovernanceError("调整单没有调整明细")
        self.status = ADJ_SUBMITTED


def _project_curve(
    projects_by_id: Mapping[str, ProjectVersion], baseline: Baseline, pid: str
) -> dict[int, int]:
    pv = projects_by_id[pid]
    return pv.scheduled_curve(baseline.schedule[pid])


def _funding_keys(
    pv: ProjectVersion, curve: Mapping[int, int], pools: Sequence[FundingPool]
) -> dict[str, float]:
    pool_index = {(p.source_id, p.year) for p in pools}
    need: dict[str, float] = {}

    def resolve(source: str, year: int) -> str:
        if (source, year) in pool_index:
            return f"{source}@{year}"
        if (source, None) in pool_index:
            return source
        raise GovernanceError(f"资金来源 {source} 无对应资金池")

    for year, cost in curve.items():
        for fc in pv.funding_sources:
            key = resolve(fc.source_id, year)
            need[key] = need.get(key, 0.0) + cost * fc.share
    return need


def validate_adjustment(
    order: AdjustmentOrder,
    baseline: Baseline,
    projects_by_id: Mapping[str, ProjectVersion],
    pools: Sequence[FundingPool],
) -> dict:
    """在基线当前版本上预演调整单，返回占用预演或抛出 GovernanceError。"""
    if baseline.revision != order.baseline_revision:
        raise GovernanceError(
            f"调整单基于第 {order.baseline_revision} 版，基线已到第 {baseline.revision} 版"
        )
    schedule = dict(baseline.schedule)
    committed = {y: v for y, v in baseline.budget_committed.items()}
    funded = dict(baseline.funding_committed)

    removes = {i.project_id for i in order.items if i.action == "remove"}
    adds = {i.project_id for i in order.items if i.action == "add"}
    retimes = {i.project_id for i in order.items if i.action == "retime"}
    if removes & adds:
        raise GovernanceError(f"同一项目不能同时新增与移除：{sorted(removes & adds)}")

    # 级联移除：被移除项目的在册下游配套全部失效
    cascade: set[str] = set()
    direct: dict[str, list[str]] = {}
    for pid, pv in projects_by_id.items():
        for dep in pv.depends_on:
            direct.setdefault(dep.project_id, []).append(pid)
    stack = list(removes)
    seen = set()
    while stack:
        cur = stack.pop()
        for child in direct.get(cur, ()):
            if child in schedule and child not in removes and child not in seen:
                seen.add(child)
                cascade.add(child)
                stack.append(child)
    all_remove = removes | cascade

    def release(pid: str) -> None:
        curve = _project_curve(projects_by_id, baseline, pid)
        pv = projects_by_id[pid]
        for year, cost in curve.items():
            committed[year] = committed.get(year, 0.0) - cost
            for key, amount in _funding_keys(pv, curve, pools).items():
                funded[key] -= amount

    def occupancy_conflict(pv: ProjectVersion, curve: Mapping[int, int], self_id: str):
        years = set(curve)
        for other_id, other_start in new_schedule.items():
            if other_id == self_id or other_id in all_remove:
                continue
            other = projects_by_id[other_id]
            shared = set(pv.asset_ids) & set(other.asset_ids)
            other_years = set(other.scheduled_curve(other_start))
            if shared and years & other_years:
                return (other_id, sorted(shared), sorted(years & other_years)[0])
            if set(pv.mutex_groups) & set(other.mutex_groups):
                return (other_id, None, None)
        return None

    def occupy(pid: str, start: int) -> dict[int, int]:
        pv = projects_by_id.get(pid)
        if pv is None:
            raise GovernanceError(f"项目 {pid} 不在申报目录")
        if start < pv.base_year:
            raise GovernanceError(
                f"{pid} 开工年 {start} 早于其最早可行年度 {pv.base_year}")
        curve = pv.scheduled_curve(start)
        new_end = max(curve)
        for dep in pv.depends_on:
            if dep.project_id not in schedule or dep.project_id in all_remove:
                raise GovernanceError(
                    f"新增/调整 {pid} 的前置项目 {dep.project_id} 不在调整后基线中"
                )
            dep_start = new_schedule.get(dep.project_id, schedule[dep.project_id])
            dep_end = max(projects_by_id[dep.project_id]
                          .scheduled_curve(dep_start))
            if start < dep_end + 1 + dep.lag_years:
                raise GovernanceError(
                    f"{pid} 开工年 {start} 早于前置 {dep.project_id} 完工约束 "
                    f"{dep_end + 1 + dep.lag_years}"
                )
        # 反向约束：重排/新增后，在册下游项目仍须满足其完工滞后要求
        for other_id, other_pv in projects_by_id.items():
            if other_id == pid or other_id not in new_schedule or other_id in all_remove:
                continue
            for dep in other_pv.depends_on:
                if dep.project_id != pid:
                    continue
                if new_schedule[other_id] < new_end + 1 + dep.lag_years:
                    raise GovernanceError(
                        f"{pid} 重排至 {start} 年后，在册下游项目 {other_id} "
                        f"开工年 {new_schedule[other_id]} 早于完工约束 "
                        f"{new_end + 1 + dep.lag_years}，需同步调整或随调整单移除"
                    )
        for year, cost in curve.items():
            if year not in baseline.budgets:
                raise GovernanceError(f"{pid} 排到预算年度 {year}，该年度无预算 envelope")
            if cost > baseline.budgets[year] - committed.get(year, 0.0) + 1e-6:
                raise GovernanceError(
                    f"{pid} 在 {year} 年需 {cost}，预算余量 "
                    f"{int(baseline.budgets[year] - committed.get(year, 0.0))}"
                )
        for key, amount in _funding_keys(pv, curve, pools).items():
            limit = baseline.funding_limits.get(key)
            if limit is None:
                raise GovernanceError(f"资金来源 {key} 不在基线资金目录中")
            if amount > limit - funded.get(key, 0.0) + 1e-6:
                raise GovernanceError(
                    f"{pid} 占用 {key} 需 {int(round(amount))}，余量 "
                    f"{int(limit - funded.get(key, 0.0))}"
                )
        conflict = occupancy_conflict(pv, curve, pid)
        if conflict:
            other, shared, yr = conflict
            if shared is None:
                raise GovernanceError(f"{pid} 与在册项目 {other} 属同一互斥施工组")
            raise GovernanceError(
                f"{pid} 与 {other} 在 {yr} 年同时占用共同管段 {shared}"
            )
        return curve

    # 先释放
    for pid in all_remove:
        if pid not in schedule:
            raise GovernanceError(f"项目 {pid} 不在当前基线中，无法移除")
        release(pid)

    # 再新增 / 重排（重排先视作按新年份落位）
    new_schedule = dict(schedule)
    for pid in all_remove:
        new_schedule.pop(pid, None)

    def stage(pid: str, start: int) -> None:
        curve = occupy(pid, start)
        pv = projects_by_id[pid]
        for year, cost in curve.items():
            committed[year] = committed.get(year, 0.0) + cost
        for key, amount in _funding_keys(pv, curve, pools).items():
            funded[key] = funded.get(key, 0.0) + amount
        new_schedule[pid] = start

    for item in order.items:
        if item.action == "retime":
            if item.project_id not in schedule or item.start_year is None:
                raise GovernanceError(f"重排项目 {item.project_id} 不在基线或未给新年份")
            old_curve = _project_curve(projects_by_id, baseline, item.project_id)
            pv = projects_by_id[item.project_id]
            for year, cost in old_curve.items():
                committed[year] -= cost
                for key, amount in _funding_keys(pv, old_curve, pools).items():
                    funded[key] -= amount
            new_schedule.pop(item.project_id, None)
            stage(item.project_id, item.start_year)
    for item in order.items:
        if item.action == "add":
            if item.start_year is None:
                raise GovernanceError(f"新增项目 {item.project_id} 必须指定开工年")
            if item.project_id in baseline.schedule:
                raise GovernanceError(f"项目 {item.project_id} 已在基线中，应用 retime")
            stage(item.project_id, item.start_year)

    return {
        "schedule": new_schedule,
        "committed": committed,
        "funded": funded,
        "cascade_removed": sorted(cascade),
    }


def apply_adjustment(
    order: AdjustmentOrder,
    baseline: Baseline,
    projects_by_id: Mapping[str, ProjectVersion],
    pools: Sequence[FundingPool],
    applied_on: str,
) -> Baseline:
    """审批通过后应用调整单，返回基线新版本（原基线不变）。"""
    if order.status != ADJ_SUBMITTED:
        raise GovernanceError("只有已提交的调整单可以审批应用")
    preview = validate_adjustment(order, baseline, projects_by_id, pools)
    new_baseline = Baseline(
        baseline_id=baseline.baseline_id,
        revision=baseline.revision + 1,
        approved_on=applied_on,
        scenario_id=baseline.scenario_id,
        schedule=preview["schedule"],
        project_versions={
            pid: projects_by_id[pid].version for pid in preview["schedule"]
        },
        budgets=dict(baseline.budgets),
        budget_committed=preview["committed"],
        funding_limits=dict(baseline.funding_limits),
        funding_committed=preview["funded"],
        recognized_benefit={
            pid: v for pid, v in baseline.recognized_benefit.items()
            if pid in preview["schedule"]
        },
        adjustments=baseline.adjustments + [{
            "order_id": order.order_id,
            "from_revision": baseline.revision,
            "to_revision": baseline.revision + 1,
            "applied_on": applied_on,
            "items": [vars(i) for i in order.items],
            "cascade_removed": preview["cascade_removed"],
            "rationale": order.rationale,
        }],
        actual_spend={
            k: v for k, v in baseline.actual_spend.items()
            if k.split("@", 1)[0] in preview["schedule"]
        },
        actual_mileage={
            k: v for k, v in baseline.actual_mileage.items()
            if k.split("@", 1)[0] in preview["schedule"]
        },
    )
    order.status = ADJ_APPROVED
    order.cascade_removed = preview["cascade_removed"]
    order.applied_on = applied_on
    return new_baseline


@dataclass
class VarianceAlert:
    project_id: str
    year: int
    kind: str          # cost_overrun | cost_underspend | mileage_lag
    level: str         # warn | major
    message: str
    actual: float
    planned: float


def backfill_actuals(
    baseline: Baseline,
    projects_by_id: Mapping[str, ProjectVersion],
    project_id: str,
    year: int,
    spend: int,
    mileage: float,
) -> list[VarianceAlert]:
    """滚动回填某项目某年度实际支出与里程完成量，并触发偏差预警。"""
    if project_id not in baseline.schedule:
        raise GovernanceError(f"项目 {project_id} 不在当前基线中")
    if spend < 0 or mileage < 0:
        raise GovernanceError("实际支出与里程不能为负")
    pv = projects_by_id[project_id]
    curve = _project_curve(projects_by_id, baseline, project_id)
    if year not in curve:
        raise GovernanceError(
            f"{project_id} 计划施工年为 {sorted(curve)}，{year} 年无计划，不能回填"
        )

    baseline.actual_spend[baseline.key(project_id, year)] = spend
    baseline.actual_mileage[baseline.key(project_id, year)] = mileage

    alerts: list[VarianceAlert] = []
    reported_years = sorted(
        int(k.split("@", 1)[1])
        for k in baseline.actual_spend
        if k.split("@", 1)[0] == project_id
    )
    cum_actual = sum(baseline.actual_spend[baseline.key(project_id, y)]
                     for y in reported_years)
    cum_planned = sum(curve[y] for y in reported_years)
    rate = (cum_actual - cum_planned) / cum_planned if cum_planned else 0.0
    if rate >= 0.20:
        alerts.append(VarianceAlert(
            project_id, year, "cost_overrun", "major",
            f"截至 {year} 年累计支出超概算 {rate:.1%}（实付 {cum_actual} / 计划 {cum_planned}）",
            cum_actual, cum_planned))
    elif rate >= 0.10:
        alerts.append(VarianceAlert(
            project_id, year, "cost_overrun", "warn",
            f"截至 {year} 年累计支出超概算 {rate:.1%}（实付 {cum_actual} / 计划 {cum_planned}）",
            cum_actual, cum_planned))
    elif rate <= -0.10:
        alerts.append(VarianceAlert(
            project_id, year, "cost_underspend", "warn",
            f"截至 {year} 年累计支出低于计划 {abs(rate):.1%}（实付 {cum_actual} / 计划 {cum_planned}）",
            cum_actual, cum_planned))

    total_mileage = pv.mileage
    if total_mileage > 0:
        cum_mileage = sum(baseline.actual_mileage[baseline.key(project_id, y)]
                          for y in reported_years)
        planned_share = cum_planned / pv.estimated_cost
        actual_share = cum_mileage / total_mileage
        gap = actual_share - planned_share
        if gap <= -0.15:
            alerts.append(VarianceAlert(
                project_id, year, "mileage_lag", "warn",
                f"里程完成 {actual_share:.1%} 落后按成本应有的进度 {planned_share:.1%}"
                f"（累计 {cum_mileage:.0f}/{total_mileage:.0f} 米）",
                cum_mileage, total_mileage * planned_share))
    return alerts


@dataclass
class ScenarioDiff:
    """两个情景之间可复核的差异。"""

    left_id: str
    right_id: str
    assumption_changes: list[str]
    only_left: list[str]
    only_right: list[str]
    rescheduled: dict[str, tuple[int, int]]
    status_changes: dict[str, tuple[str, str]]
    cost_by_year_left: dict[int, float]
    cost_by_year_right: dict[int, float]
    budget_headroom_left: dict[int, float]
    budget_headroom_right: dict[int, float]
    recognized_benefit_left: int
    recognized_benefit_right: int
    duplicated_left: int
    duplicated_right: int

    def as_text(self) -> str:
        changes = [f"  - {c}" for c in self.assumption_changes] or ["  （无）"]
        rescheduled = [
            f"  {pid}: {a} → {b}"
            for pid, (a, b) in sorted(self.rescheduled.items())
        ] or ["  （无）"]
        statuses = [
            f"  {pid}: {a} → {b}"
            for pid, (a, b) in sorted(self.status_changes.items())
        ] or ["  （无）"]
        lines = [
            f"情景对比：{self.left_id} → {self.right_id}",
            "假设变化：",
            *changes,
            f"仅 {self.left_id} 入选：{self.only_left or '无'}",
            f"仅 {self.right_id} 入选：{self.only_right or '无'}",
            "开工年调整：",
            *rescheduled,
            "状态变化：",
            *statuses,
        ]
        years = sorted(set(self.cost_by_year_left) | set(self.cost_by_year_right))
        lines.append("年度占用 / 预算余量（左 → 右）：")
        for y in years:
            lines.append(
                f"  {y}: {int(self.cost_by_year_left.get(y, 0))} → "
                f"{int(self.cost_by_year_right.get(y, 0))}；"
                f"余量 {int(self.budget_headroom_left.get(y, 0))} → "
                f"{int(self.budget_headroom_right.get(y, 0))}"
            )
        lines.append(
            f"去重后认可收益：{self.recognized_benefit_left} → {self.recognized_benefit_right}"
        )
        lines.append(
            f"消除的重复申报收益：{self.duplicated_left} → {self.duplicated_right}"
        )
        return "\n".join(lines)


def compare_scenarios(left: Scenario, right: Scenario) -> ScenarioDiff:
    if left.plan is None or right.plan is None:
        raise GovernanceError("两个情景都必须已完成计算才能比较")
    la, ra = left.assumptions, right.assumptions
    changes: list[str] = []
    if la.forced_in != ra.forced_in:
        changes.append(f"锁定入选：{la.forced_in} → {ra.forced_in}")
    if sorted(la.excluded) != sorted(ra.excluded):
        changes.append(f"锁定排除：{sorted(la.excluded)} → {sorted(ra.excluded)}")
    lw, rw = la.weights, ra.weights
    if lw.risk_value_per_point != rw.risk_value_per_point:
        changes.append(
            f"风险权重：{lw.risk_value_per_point} → {rw.risk_value_per_point}")
    if lw.enablement_bonus_per_dependent != rw.enablement_bonus_per_dependent:
        changes.append(
            f"配套带动权重：{lw.enablement_bonus_per_dependent} → "
            f"{rw.enablement_bonus_per_dependent}")
    if dict(lw.manual_boost) != dict(rw.manual_boost):
        changes.append(
            f"人工优先级调整：{dict(lw.manual_boost)} → {dict(rw.manual_boost)}")

    lp, rp = left.plan, right.plan
    l_sel = {d.project_id: d.start_year for d in lp.selected}
    r_sel = {d.project_id: d.start_year for d in rp.selected}
    rescheduled = {
        pid: (l_sel[pid], r_sel[pid])
        for pid in sorted(set(l_sel) & set(r_sel))
        if l_sel[pid] != r_sel[pid]
    }
    l_status = {d.project_id: d.status for d in lp.decisions}
    r_status = {d.project_id: d.status for d in rp.decisions}
    status_changes = {
        pid: (l_status[pid], r_status[pid])
        for pid in sorted(set(l_status) & set(r_status))
        if l_status[pid] != r_status[pid]
    }

    def cost_by_year(plan: PlanResult) -> dict[int, float]:
        out: dict[int, float] = {}
        for d in plan.selected:
            for y, c in d.cost_by_year.items():
                out[y] = out.get(y, 0.0) + c
        return out

    cy_l, cy_r = cost_by_year(lp), cost_by_year(rp)
    return ScenarioDiff(
        left_id=left.scenario_id,
        right_id=right.scenario_id,
        assumption_changes=changes,
        only_left=sorted(set(l_sel) - set(r_sel)),
        only_right=sorted(set(r_sel) - set(l_sel)),
        rescheduled=rescheduled,
        status_changes=status_changes,
        cost_by_year_left=cy_l,
        cost_by_year_right=cy_r,
        budget_headroom_left={y: u.remaining for y, u in lp.budget_usage.items()},
        budget_headroom_right={y: u.remaining for y, u in rp.budget_usage.items()},
        recognized_benefit_left=sum(lp.recognized_by_project.values()),
        recognized_benefit_right=sum(rp.recognized_by_project.values()),
        duplicated_left=lp.duplicated_benefit_eliminated,
        duplicated_right=rp.duplicated_benefit_eliminated,
    )
