"""跨年度候选方案规划引擎。

按优先级顺序为每个项目版本尝试最早可行开工年，逐笔占用年度预算与
资金来源；任一一致性约束不满足即落选并给出可复核原因。项目落选后，
传递依赖它的配套工程按依赖链级联失效。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from . import analysis
from .contracts import FundingPool, ProjectVersion

RISK_VALUE_PER_POINT = 2_000_000
ENABLEMENT_BONUS_PER_DEPENDENT = 500_000


class PlanError(ValueError):
    """规划输入或锁定假设无法满足（含具体冲突明细）。"""

    def __init__(self, message: str, violations: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.violations = list(violations)


@dataclass(frozen=True)
class PlanWeights:
    """优先级评分权重（评审人可在情景中调整）。"""

    risk_value_per_point: int = RISK_VALUE_PER_POINT
    enablement_bonus_per_dependent: int = ENABLEMENT_BONUS_PER_DEPENDENT
    manual_boost: Mapping[str, float] = field(default_factory=dict)


@dataclass
class ScoreBreakdown:
    project_id: str
    claimed_benefit: int
    risk_points: int
    dependent_count: int
    manual_boost: float
    risk_weight: int
    enablement_weight: int
    total: float

    def components(self) -> dict[str, float]:
        return {
            "申报收益": float(self.claimed_benefit),
            f"风险严重度×{self.risk_weight}": float(
                self.risk_points * self.risk_weight
            ),
            f"配套带动×{self.enablement_weight}": float(
                self.dependent_count * self.enablement_weight
            ),
            "人工优先级调整": float(self.manual_boost),
        }


@dataclass
class Decision:
    """单个项目入选/落选决定及其完整解释。"""

    project_id: str
    status: str  # selected | deferred | infeasible
    score: float
    score_components: Mapping[str, float]
    start_year: int | None = None
    end_year: int | None = None
    cost_by_year: dict[int, int] = field(default_factory=dict)
    recognized_benefit: int = 0
    reason_codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    blocker_chain: list[str] = field(default_factory=list)

    @property
    def selected(self) -> bool:
        return self.status == "selected"


@dataclass
class Usage:
    limit: int
    committed: float = 0.0

    @property
    def remaining(self) -> float:
        return self.limit - self.committed


@dataclass
class PlanResult:
    horizon: tuple[int, int]
    decisions: list[Decision]
    budget_usage: dict[int, Usage]
    funding_usage: dict[str, Usage]
    recognized_by_project: dict[str, int]
    asset_winner: dict[str, str]
    duplicated_benefit_eliminated: int
    overlaps: list[analysis.AssetOverlap]
    propagation: dict[str, dict]
    score_order: list[str]
    weights: PlanWeights

    def decision(self, project_id: str) -> Decision:
        for d in self.decisions:
            if d.project_id == project_id:
                return d
        raise KeyError(project_id)

    @property
    def selected(self) -> list[Decision]:
        return [d for d in self.decisions if d.selected]

    @property
    def deferred(self) -> list[Decision]:
        return [d for d in self.decisions if not d.selected]

    def explain(self, project_id: str) -> str:
        d = self.decision(project_id)
        lines = [f"项目 {project_id}：{self._status_text(d.status)}（评分 {d.score:.0f}）"]
        lines.append("评分构成：" + "，".join(f"{k}={v:.0f}" for k, v in d.score_components.items()))
        if d.selected:
            lines.append(f"排程：{d.start_year}–{d.end_year}，年度占用 {d.cost_by_year}")
            lines.append(f"去重后认可收益：{d.recognized_benefit}")
        lines.extend(f"- {r}" for r in d.reasons)
        if d.blocker_chain:
            lines.append("依赖传播链：" + " → ".join(d.blocker_chain))
        return "\n".join(lines)

    @staticmethod
    def _status_text(status: str) -> str:
        return {"selected": "入选", "deferred": "延后", "infeasible": "不可行"}[status]


def _direct_dependents(projects: Mapping[str, ProjectVersion]) -> dict[str, list[str]]:
    direct: dict[str, list[str]] = {pid: [] for pid in projects}
    for pid, pv in projects.items():
        for dep in pv.depends_on:
            direct.setdefault(dep.project_id, []).append(pid)
    return direct


def _shortest_path(
    source: str, target: str, direct: Mapping[str, list[str]]
) -> list[str]:
    """source 落选导致 target 失效的最短依赖链（source→…→target）。"""
    queue = deque([(source, [source])])
    seen = {source}
    while queue:
        node, path = queue.popleft()
        for child in direct.get(node, ()):
            if child == target:
                return path + [child]
            if child not in seen:
                seen.add(child)
                queue.append((child, path + [child]))
    return [source, target]


def build_plan(
    projects: Sequence[ProjectVersion],
    budgets: Mapping[int, int],
    funding_pools: Sequence[FundingPool] = (),
    weights: PlanWeights | None = None,
    forced_in: Mapping[str, int | None] | None = None,
    excluded: Sequence[str] = (),
) -> PlanResult:
    """生成跨年度候选方案。

    forced_in: 锁定入选项目 -> 锁定开工年（None 表示只锁定入选不锁年）。
    excluded:  锁定排除项目。
    锁定假设与约束冲突时抛出 :class:`PlanError`，附全部冲突明细。
    """
    weights = weights or PlanWeights()
    forced_in = dict(forced_in or {})
    excluded = set(excluded)

    latest: dict[str, ProjectVersion] = {}
    for pv in projects:
        old = latest.get(pv.project_id)
        if old is None or pv.version > old.version:
            latest[pv.project_id] = pv

    violations = [f"未知项目：{pid}" for pid in set(excluded) | set(forced_in)
                  if pid not in latest]
    budgets = {int(y): int(a) for y, a in budgets.items()}
    for year, amount in budgets.items():
        if amount < 0:
            violations.append(f"{year} 年度预算为负")
    if not budgets:
        violations.append("未提供任何年度预算")
    if violations:
        raise PlanError("规划输入无效", violations)

    horizon_start, horizon_end = min(budgets), max(budgets)
    for pid in forced_in:
        pv = latest[pid]
        if pv.base_year > horizon_end:
            violations.append(f"锁定项目 {pid} 最早可行年 {pv.base_year} 超出 horizon")
    if violations:
        raise PlanError("锁定假设不可行", violations)

    cycles = analysis.find_dependency_cycles(latest.values())
    cyclic = {pid for cyc in cycles for pid in cyc}
    missing_deps = {
        pid: [d.project_id for d in pv.depends_on if d.project_id not in latest]
        for pid, pv in latest.items()
    }

    direct = _direct_dependents(latest)
    reverse = analysis.build_reverse_dependencies(latest.values())

    overlaps = analysis.find_scope_overlaps(latest.values())

    # ---- 评分与排序 ----
    scores: dict[str, ScoreBreakdown] = {}
    for pid, pv in latest.items():
        boost = float(dict(weights.manual_boost).get(pid, 0.0))
        total = float(pv.claimed_benefit)
        total += pv.risk_points * float(weights.risk_value_per_point)
        total += len(reverse.get(pid, ())) * float(weights.enablement_bonus_per_dependent)
        total += boost
        scores[pid] = ScoreBreakdown(
            project_id=pid,
            claimed_benefit=pv.claimed_benefit,
            risk_points=pv.risk_points,
            dependent_count=len(reverse.get(pid, ())),
            manual_boost=boost,
            risk_weight=weights.risk_value_per_point,
            enablement_weight=weights.enablement_bonus_per_dependent,
            total=total,
        )

    def rank_key(pid: str):
        locked = pid in forced_in
        return (
            0 if locked else 1,           # 锁定入选优先处理
            -scores[pid].total,
            -latest[pid].estimated_cost,  # 同分时大项目在前，保证确定性
            pid,
        )

    score_order = sorted(latest, key=rank_key)

    # ---- 占用账本 ----
    budget_usage = {y: Usage(limit=budgets[y]) for y in sorted(budgets)}
    pool_index: dict[tuple[str, int | None], FundingPool] = {}
    for pool in funding_pools:
        key = (pool.source_id, pool.year)
        if key in pool_index:
            raise PlanError("资金池重复定义", [f"来源 {pool.source_id} 年度 {pool.year}"])
        pool_index[key] = pool
    funding_usage = {
        f"{pid}{('@' + str(y)) if y is not None else ''}": Usage(limit=p.amount)
        for (pid, y), p in pool_index.items()
    }

    def pool_key(source: str, year: int) -> str:
        if (source, year) in pool_index:
            return f"{source}@{year}"
        if (source, None) in pool_index:
            return source
        raise PlanError("资金来源未定义", [f"项目占用来源 {source}，但无对应资金池"])

    decisions: dict[str, Decision] = {}
    selected: dict[str, int] = {}   # project_id -> start_year
    selected_asset_years: dict[str, set[int]] = {}

    def asset_conflict(pv: ProjectVersion, years: Sequence[int]) -> tuple[str, int] | None:
        year_set = set(years)
        for other_id, other_start in selected.items():
            other = latest[other_id]
            shared = set(pv.asset_ids) & set(other.asset_ids)
            if shared and year_set & selected_asset_years[other_id]:
                bad = sorted(year_set & selected_asset_years[other_id])[0]
                return other_id, bad
        return None

    def mutex_conflict(pv: ProjectVersion) -> str | None:
        for other_id in selected:
            if set(pv.mutex_groups) & set(latest[other_id].mutex_groups):
                return other_id
        return None

    def funding_need(pv: ProjectVersion, cost_by_year: Mapping[int, int]) -> dict[str, float]:
        need: dict[str, float] = {}
        for year, cost in cost_by_year.items():
            for fc in pv.funding_sources:
                key = pool_key(fc.source_id, year)
                need[key] = need.get(key, 0.0) + cost * fc.share
        return need

    def try_schedule(pv: ProjectVersion, lower: int):
        """返回最早可行 (start_year, curve, needs)，否则 (None, 尝试明细)。"""
        attempts: list[str] = []
        upper = horizon_end - pv.duration_years + 1
        locked_year = forced_in.get(pv.project_id)
        candidates = [locked_year] if locked_year is not None else range(lower, upper + 1)
        for start in candidates:
            if start < lower:
                attempts.append(
                    f"{start}年：早于依赖约束的最早开工年 {lower}"
                )
                continue
            curve = pv.scheduled_curve(start)
            blocked: list[str] = []
            for year, cost in curve.items():
                usage = budget_usage.get(year)
                if usage is None:
                    blocked.append(f"{year}年超出预算 horizon（截止 {horizon_end}）")
                elif cost > usage.remaining + 1e-6:
                    blocked.append(
                        f"{year}年预算不足：需 {cost}，余量 {int(usage.remaining)}"
                    )
            needs = funding_need(pv, curve) if not any("超出预算" in b for b in blocked) else {}
            for key, amount in needs.items():
                usage = funding_usage[key]
                if amount > usage.remaining + 1e-6:
                    blocked.append(
                        f"资金来源 {key} 不足：需 {int(round(amount))}，"
                        f"余量 {int(usage.remaining)}"
                    )
            other = mutex_conflict(pv)
            if other is not None:
                blocked.append(f"互斥施工冲突：{other} 已入选（同互斥组）")
            hit = asset_conflict(pv, curve)
            if hit is not None:
                blocked.append(
                    f"共同管段 {sorted(set(pv.asset_ids) & set(latest[hit[0]].asset_ids))} "
                    f"在 {hit[1]} 年与已入选项目 {hit[0]} 同时施工"
                )
            if not blocked:
                return start, curve, needs
            if locked_year is not None:
                attempts.append(f"{start}年（锁定年）：" + "；".join(blocked))
            else:
                attempts.append(f"{start}年：" + "；".join(blocked))
        return None, attempts, {}

    def commit(pv: ProjectVersion, start: int, curve: dict[int, int], needs: dict[str, float]):
        selected[pv.project_id] = start
        selected_asset_years[pv.project_id] = set(curve)
        for year, cost in curve.items():
            budget_usage[year].committed += cost
        for key, amount in needs.items():
            funding_usage[key].committed += amount

    def make_decision(pid: str, status: str, **kw) -> Decision:
        sb = scores[pid]
        d = Decision(
            project_id=pid,
            status=status,
            score=sb.total,
            score_components=sb.components(),
            **kw,
        )
        decisions[pid] = d
        return d

    # 显式排除
    for pid in excluded:
        make_decision(
            pid,
            "deferred",
            reason_codes=["reviewer_excluded"],
            reasons=["评审人在情景假设中锁定排除该项目"],
        )

    # 环 / 缺失依赖：不可行
    for pid in score_order:
        if pid in decisions:
            continue
        if pid in cyclic:
            make_decision(
                pid, "infeasible",
                reason_codes=["dependency_cycle"],
                reasons=[f"前置依赖存在环：{next(c for c in cycles if pid in c)}"],
            )
        elif missing_deps[pid]:
            make_decision(
                pid, "infeasible",
                reason_codes=["missing_dependency"],
                reasons=[f"前置项目不在申报范围：{missing_deps[pid]}"],
            )

    # 主循环：允许因依赖尚未处理而等待重试
    pending = [pid for pid in score_order if pid not in decisions]
    forced_violations: list[str] = []
    while pending:
        progressed = False
        next_pending: list[str] = []
        for pid in pending:
            pv = latest[pid]
            dep_status = [(d, decisions.get(d.project_id)) for d in pv.depends_on]
            waiting = [d.project_id for d, dec in dep_status if dec is None]
            if waiting:
                next_pending.append(pid)  # 依赖尚未决策，下轮再试
                continue
            progressed = True
            blockers = [
                (d, dec) for d, dec in dep_status
                if dec is not None and not dec.selected
            ]
            if blockers:
                dep, dec = blockers[0]
                chain = _shortest_path(dep.project_id, pid, direct)
                make_decision(
                    pid, "deferred",
                    reason_codes=["dependency_blocked"],
                    blocker_chain=chain,
                    reasons=[
                        f"前置项目 {dep.project_id} 状态为"
                        f"{PlanResult._status_text(dec.status)}，配套工程随之失效"
                        + (f"（滞后 {dep.lag_years} 年）" if dep.lag_years else "")
                    ],
                )
                if pid in forced_in:
                    forced_violations.append(
                        f"锁定入选项目 {pid} 的前置 {dep.project_id} "
                        f"状态为 {dec.status}，锁定假设无法满足"
                    )
                continue
            lower = max(pv.base_year, horizon_start)
            for dep in pv.depends_on:
                dep_dec = decisions[dep.project_id]
                lower = max(lower, (dep_dec.end_year or horizon_end) + 1 + dep.lag_years)
            start, curve_or_attempts, needs = (None, None, None)
            if lower <= horizon_end - pv.duration_years + 1:
                start, curve_or_attempts, needs = try_schedule(pv, lower)
            else:
                curve_or_attempts = [
                    f"依赖最早完工约束使开工年不早于 {lower}，已超出 horizon {horizon_end}"
                ]
            if start is not None:
                commit(pv, start, curve_or_attempts, needs)
                make_decision(
                    pid, "selected",
                    start_year=start,
                    end_year=max(curve_or_attempts),
                    cost_by_year=dict(curve_or_attempts),
                    reasons=[_selection_reason(pv, start, lower)],
                )
            else:
                locked_note = "（锁定入选假设无法满足）" if pid in forced_in else ""
                make_decision(
                    pid, "deferred",
                    reason_codes=["no_feasible_window"],
                    reasons=[f"horizon 内无可行排程窗口{locked_note}"]
                    + curve_or_attempts,
                )
                if pid in forced_in:
                    forced_violations.append(
                        f"锁定入选项目 {pid} 无法排程：\n      "
                        + "\n      ".join(curve_or_attempts)
                    )
        pending = next_pending
        if not progressed and pending:
            for pid in pending:
                make_decision(
                    pid, "infeasible",
                    reason_codes=["dependency_cycle"],
                    reasons=["依赖链无法解析（可能成环或与被排除项目互锁）"],
                )
            break

    if forced_violations:
        raise PlanError("锁定假设与一致性约束冲突", forced_violations)

    # ---- 收益去重归属（按评分顺序，入选项目之间） ----
    ordered = [latest[pid] for pid in score_order if decisions[pid].selected]
    recognized, winner, duplicated = analysis.attribute_benefits(ordered)
    for d in decisions.values():
        d.recognized_benefit = recognized.get(d.project_id, 0)

    # ---- 依赖传播汇总 ----
    propagation: dict[str, dict] = {}
    for pid, d in decisions.items():
        if d.selected:
            continue
        downstream = sorted(
            p for p in reverse.get(pid, ())
            if p in decisions and not decisions[p].selected
        )
        if downstream:
            chains = {p: _shortest_path(pid, p, direct) for p in downstream}
            propagation[pid] = {"downstream": downstream, "chains": chains}

    ordered_decisions = [decisions[pid] for pid in score_order]
    return PlanResult(
        horizon=(horizon_start, horizon_end),
        decisions=ordered_decisions,
        budget_usage=budget_usage,
        funding_usage=funding_usage,
        recognized_by_project=recognized,
        asset_winner=winner,
        duplicated_benefit_eliminated=duplicated,
        overlaps=overlaps,
        propagation=propagation,
        score_order=score_order,
        weights=weights,
    )


def _selection_reason(pv: ProjectVersion, start: int, lower: int) -> str:
    bits = [f"最早可行开工年 {start}"]
    if pv.depends_on:
        bits.append(f"前置依赖约束下界 {lower}")
    if pv.funding_sources:
        bits.append("资金来源占用已通过校验")
    bits.append("年度预算、共同管段与互斥施工约束均满足")
    return "；".join(bits)
