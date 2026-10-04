"""改造投资组合治理服务编排层。

组合契约、分析引擎与治理生命周期，对接口（CLI/未来的 HTTP 适配层）
提供单一入口，并负责全部可解释性输出的序列化。
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date
from typing import Mapping, Sequence

from . import analysis
from .contracts import BudgetEnvelope, FundingPool, ProjectVersion
from .engine import PlanError, PlanResult, PlanWeights
from .governance import (
    ADJ_SUBMITTED,
    AdjustmentItem,
    AdjustmentOrder,
    Baseline,
    GovernanceError,
    Scenario,
    ScenarioAssumptions,
    VarianceAlert,
    apply_adjustment,
    backfill_actuals,
    compare_scenarios,
    publish_baseline,
    validate_adjustment,
)


class PortfolioService:
    def __init__(self) -> None:
        self.projects: list[ProjectVersion] = []
        self.budgets: dict[int, int] = {}
        self.pools: list[FundingPool] = []
        self.scenarios: dict[str, Scenario] = {}
        self.baselines: dict[str, Baseline] = {}
        self.alerts: list[VarianceAlert] = []

    # ---------- 目录登记 ----------
    def register_project(self, project: ProjectVersion) -> None:
        """登记项目版本；同一项目保留全部版本，规划取最新版。"""
        self.projects.append(project)

    def set_budget(self, envelope: BudgetEnvelope) -> None:
        self.budgets[envelope.year] = envelope.amount

    def add_funding_pool(self, pool: FundingPool) -> None:
        for existing in self.pools:
            if existing.source_id == pool.source_id and existing.year == pool.year:
                raise GovernanceError(
                    "资金池重复", [f"{pool.source_id} 年度 {pool.year}"])
        self.pools.append(pool)

    def latest_projects(self) -> dict[str, ProjectVersion]:
        latest: dict[str, ProjectVersion] = {}
        for pv in self.projects:
            old = latest.get(pv.project_id)
            if old is None or pv.version > old.version:
                latest[pv.project_id] = pv
        return latest

    # ---------- 静态诊断 ----------
    def overlap_report(self) -> list[dict]:
        return [
            {
                "asset_id": o.asset_id,
                "project_ids": list(o.project_ids),
                "claimed_by_project": o.claimed_by_project,
                "claimed_total": o.claimed_total,
                "duplicated_benefit": o.duplicated_benefit,
            }
            for o in analysis.find_scope_overlaps(self.latest_projects().values())
        ]

    def dependency_report(self) -> dict:
        latest = self.latest_projects()
        reverse = analysis.build_reverse_dependencies(latest.values())
        return {
            "cycles": analysis.find_dependency_cycles(latest.values()),
            "mutex_groups": analysis.mutex_pairs(latest.values()),
            "propagation_map": {
                pid: sorted(children)
                for pid, children in reverse.items()
                if children
            },
        }

    # ---------- 情景 ----------
    def new_scenario(
        self,
        scenario_id: str,
        name: str,
        *,
        expires_on: str = "",
        notes: str = "",
        weights: PlanWeights | None = None,
        clone_from: str | None = None,
    ) -> Scenario:
        if scenario_id in self.scenarios:
            raise GovernanceError(f"情景 {scenario_id} 已存在")
        if clone_from is not None:
            src = self._scenario(clone_from)
            w = src.assumptions.weights
            assumptions = ScenarioAssumptions(
                name=name,
                weights=PlanWeights(
                    risk_value_per_point=w.risk_value_per_point,
                    enablement_bonus_per_dependent=w.enablement_bonus_per_dependent,
                    manual_boost=dict(w.manual_boost),
                ),
                forced_in=dict(src.assumptions.forced_in),
                excluded=list(src.assumptions.excluded),
                notes=notes,
                created_on=date.today().isoformat(),
                expires_on=expires_on,
            )
            scenario = Scenario(
                scenario_id=scenario_id, assumptions=assumptions,
                parent_scenario_id=clone_from,
            )
        else:
            assumptions = ScenarioAssumptions(
                name=name, notes=notes, weights=weights or PlanWeights(),
                created_on=date.today().isoformat(), expires_on=expires_on,
            )
            scenario = Scenario(scenario_id=scenario_id, assumptions=assumptions)
        self.scenarios[scenario_id] = scenario
        return scenario

    def compute(self, scenario_id: str) -> PlanResult:
        scenario = self._scenario(scenario_id)
        return scenario.recompute(
            self.latest_projects().values(), self.budgets, self.pools)

    def submit_scenario(self, scenario_id: str) -> None:
        scenario = self._scenario(scenario_id)
        if scenario.assumptions.is_expired():
            scenario.status = "expired"
            raise GovernanceError("情景已超过有效期，不能提交；请复制为新情景")
        if scenario.plan is None:
            self.compute(scenario_id)
        scenario.submit()

    def publish(self, scenario_id: str, baseline_id: str,
                approved_on: str | None = None) -> Baseline:
        scenario = self._scenario(scenario_id)
        if scenario.assumptions.is_expired():
            scenario.status = "expired"
            raise GovernanceError("情景已超过有效期，不能发布为基线")
        if scenario.plan is None:
            self.compute(scenario_id)
        baseline = publish_baseline(
            scenario, self.latest_projects(), baseline_id,
            approved_on or date.today().isoformat(),
        )
        self.baselines[baseline_id] = baseline
        return baseline

    # ---------- 解释 ----------
    def explain_decision(self, scenario_id: str, project_id: str) -> str:
        return self._plan(scenario_id, auto_compute=True).explain(project_id)

    def explain_propagation(self, scenario_id: str) -> str:
        plan = self._plan(scenario_id, auto_compute=True)
        if not plan.propagation:
            return "本情景中没有落选项目连带下游配套失效。"
        lines = ["依赖传播（项目延后/落选 → 随之失效的配套工程）："]
        for pid, info in sorted(plan.propagation.items()):
            lines.append(f"- {pid} 落选 → {info['downstream']}")
            for child, chain in info["chains"].items():
                lines.append(f"    {child} 传播链：{' → '.join(chain)}")
        return "\n".join(lines)

    def explain_headroom(self, scenario_id: str) -> str:
        plan = self._plan(scenario_id, auto_compute=True)
        lines = ["年度预算余量："]
        for year, usage in sorted(plan.budget_usage.items()):
            lines.append(
                f"  {year}: 额度 {usage.limit} / 已占用 {int(usage.committed)} / "
                f"余量 {int(usage.remaining)}")
        lines.append("资金来源余量：")
        for key, usage in sorted(plan.funding_usage.items()):
            lines.append(
                f"  {key}: 额度 {int(usage.limit)} / 已占用 {int(usage.committed)} / "
                f"余量 {int(usage.remaining)}")
        return "\n".join(lines)

    def overview(self, scenario_id: str) -> dict:
        plan = self._plan(scenario_id, auto_compute=True)
        return {
            "scenario_id": scenario_id,
            "status": self._scenario(scenario_id).status,
            "horizon": list(plan.horizon),
            "selected": [
                {
                    "project_id": d.project_id,
                    "start_year": d.start_year,
                    "end_year": d.end_year,
                    "score": d.score,
                    "recognized_benefit": d.recognized_benefit,
                    "cost_by_year": d.cost_by_year,
                }
                for d in plan.selected
            ],
            "deferred": [
                {
                    "project_id": d.project_id,
                    "status": d.status,
                    "reason_codes": d.reason_codes,
                    "blocker_chain": d.blocker_chain,
                }
                for d in plan.deferred
            ],
            "budget_headroom": {
                str(y): int(u.remaining) for y, u in plan.budget_usage.items()
            },
            "funding_headroom": {
                k: int(u.remaining) for k, u in plan.funding_usage.items()
            },
            "duplicated_benefit_eliminated": plan.duplicated_benefit_eliminated,
            "overlaps": self.overlap_report(),
        }

    def compare(self, left_id: str, right_id: str) -> dict:
        diff = compare_scenarios(self._scenario(left_id), self._scenario(right_id))
        return to_jsonable(diff)

    # ---------- 基线与调整单 ----------
    def baseline_view(self, baseline_id: str) -> dict:
        baseline = self._baseline(baseline_id)
        return {
            "baseline_id": baseline.baseline_id,
            "revision": baseline.revision,
            "approved_on": baseline.approved_on,
            "scenario_id": baseline.scenario_id,
            "schedule": baseline.schedule,
            "project_versions": baseline.project_versions,
            "budget_remaining": baseline.budget_remaining,
            "funding_remaining": baseline.funding_remaining,
            "adjustments": baseline.adjustments,
            "actual_spend": baseline.actual_spend,
            "actual_mileage": baseline.actual_mileage,
        }

    def create_adjustment(
        self,
        baseline_id: str,
        order_id: str,
        items: Sequence[AdjustmentItem | Mapping[str, object]],
        rationale: str = "",
        *,
        submit: bool = False,
        apply: bool = False,
        applied_on: str | None = None,
    ) -> dict:
        baseline = self._baseline(baseline_id)
        normalized = [
            i if isinstance(i, AdjustmentItem)
            else AdjustmentItem(
                action=str(i["action"]),
                project_id=str(i["project_id"]),
                start_year=i.get("start_year"),
                reason=str(i.get("reason", "")),
            )
            for i in items
        ]
        for i in normalized:
            if i.action not in ("add", "remove", "retime"):
                raise GovernanceError(
                    f"调整动作 {i.action} 无效，仅支持 add/remove/retime")
        order = AdjustmentOrder(
            order_id=order_id,
            baseline_revision=baseline.revision,
            items=normalized,
            rationale=rationale,
        )
        preview = validate_adjustment(
            order, baseline, self.latest_projects(), self.pools)
        result = {
            "order_id": order_id,
            "baseline_revision": baseline.revision,
            "preview": {
                "schedule": preview["schedule"],
                "committed": {str(y): v for y, v in preview["committed"].items()},
                "funded": preview["funded"],
                "cascade_removed": preview["cascade_removed"],
            },
        }
        if submit or apply:
            order.submit()
        if apply:
            new_baseline = apply_adjustment(
                order, baseline, self.latest_projects(), self.pools,
                applied_on or date.today().isoformat())
            self.baselines[baseline_id] = new_baseline
            result["applied"] = True
            result["new_revision"] = new_baseline.revision
        return result

    def backfill(
        self, project_id: str, year: int, spend: int, mileage: float,
        baseline_id: str | None = None,
    ) -> list[dict]:
        baseline = self._baseline(baseline_id or self._only_baseline_id())
        alerts = backfill_actuals(
            baseline, self.latest_projects(), project_id, year, spend, mileage)
        self.alerts.extend(alerts)
        return [to_jsonable(a) for a in alerts]

    # ---------- 内部 ----------
    def _scenario(self, scenario_id: str) -> Scenario:
        try:
            return self.scenarios[scenario_id]
        except KeyError:
            raise GovernanceError(f"情景 {scenario_id} 不存在")

    def _plan(self, scenario_id: str, auto_compute: bool = False) -> PlanResult:
        scenario = self._scenario(scenario_id)
        if scenario.plan is None:
            if auto_compute:
                return self.compute(scenario_id)
            raise GovernanceError(f"情景 {scenario_id} 尚未计算")
        return scenario.plan

    def _baseline(self, baseline_id: str) -> Baseline:
        try:
            return self.baselines[baseline_id]
        except KeyError:
            raise GovernanceError(f"基线 {baseline_id} 不存在")

    def _only_baseline_id(self) -> str:
        if len(self.baselines) != 1:
            raise GovernanceError("存在多个基线，请显式指定 baseline_id")
        return next(iter(self.baselines))


def to_jsonable(obj):
    """把 dataclass/映射等递归转换为 JSON 可序列化结构。"""
    if is_dataclass(obj):
        return to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 2)
    return obj
