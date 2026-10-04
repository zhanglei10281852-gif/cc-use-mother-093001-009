"""跨年度投资组合求解器。

确定性贪心算法：按优先级得分排序，逐轮安排满足前置依赖的项目，
在每个候选开工年校验预算占用、互斥施工与范围重叠策略，
并为每个项目记录可解释的入选/落选决定。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .analysis import (
    dedupe_benefits, dependency_graph, detect_overlaps, find_cycles,
    transitive_prerequisites,
)
from .domain import (
    ASSUME_CAP_OVERRIDE, ASSUME_EXCLUDE, ASSUME_INCLUDE, ASSUME_PIN_START,
    ASSUME_PRIORITY, EXCLUSION_PORTFOLIO, EXCLUSION_SAME_YEAR,
    OVERLAP_FORBID, Assumption, Decision, DecisionRecord, InfeasibleError,
    ProjectVersionRecord, ReasonCode, ScenarioItem,
)

_SCORE_SCALE = 1_000_000  # 收益成本比放大系数，使风险分与效益分同量级


@dataclass(frozen=True)
class SolveConfig:
    horizon: tuple[int, int]                     # 规划窗口(起止年, 含)
    caps: dict[str, dict[int, int]]              # 资金来源 -> 年度 -> 额度
    metric_values: dict[str, float] = field(default_factory=dict)  # 收益指标单价(元)
    risk_weight: float = 1.0
    benefit_weight: float = 1.0
    overlap_policy: str = "dedupe"               # dedupe | forbid
    priority_multipliers: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "horizon": list(self.horizon),
            "caps": {s: {str(y): c for y, c in years.items()} for s, years in self.caps.items()},
            "metric_values": dict(self.metric_values),
            "risk_weight": self.risk_weight, "benefit_weight": self.benefit_weight,
            "overlap_policy": self.overlap_policy,
            "priority_multipliers": dict(self.priority_multipliers),
        }


@dataclass
class SolveResult:
    items: list[ScenarioItem]
    decisions: list[DecisionRecord]
    overlaps: list[dict]
    benefit_total: float
    benefit_by_metric: dict[str, float]
    benefit_attribution: list[dict]


def benefit_value(p: ProjectVersionRecord, metric_values: dict[str, float]) -> float:
    return sum(b.value * metric_values.get(b.metric, 0.0) for b in p.benefits)


def priority_score(p: ProjectVersionRecord, config: SolveConfig) -> float:
    mult = config.priority_multipliers.get(p.project_id, 1.0)
    per_yuan = benefit_value(p, config.metric_values) / max(p.total_cost, 1)
    return mult * (config.risk_weight * p.risk_score
                   + config.benefit_weight * per_yuan * _SCORE_SCALE)


def _exclusion_map(projects: dict[str, ProjectVersionRecord]
                   ) -> dict[str, dict[str, str]]:
    """对称化的互斥关系: 项目 -> {对方项目: 范围}。冲突时取更强约束(portfolio)。"""
    rel: dict[str, dict[str, str]] = {pid: {} for pid in projects}
    for pid, p in projects.items():
        for other, scope in p.exclusive_with:
            if other not in projects:
                continue
            for a, b in ((pid, other), (other, pid)):
                prev = rel[a].get(b)
                if prev != EXCLUSION_PORTFOLIO:
                    rel[a][b] = scope if prev is None else max(prev, scope)
    return rel


def _windows_overlap(a_start: int, a_dur: int, b_start: int, b_dur: int) -> bool:
    return a_start <= b_start + b_dur - 1 and b_start <= a_start + a_dur - 1


def solve(projects: dict[str, ProjectVersionRecord], config: SolveConfig,
          assumptions: list[Assumption] | None = None) -> SolveResult:
    """求解跨年度候选方案。所有决定均记录原因，保证可复核。"""
    assumptions = assumptions or []
    graph = dependency_graph(projects)
    cycles = find_cycles(graph)
    if cycles:
        raise InfeasibleError(
            "项目依赖存在环: " + "; ".join(" -> ".join(c) for c in cycles))

    locked_in = {a.project_id for a in assumptions if a.kind == ASSUME_INCLUDE}
    locked_out = {a.project_id for a in assumptions if a.kind == ASSUME_EXCLUDE}
    pins = {a.project_id: int(a.value["start_year"])
            for a in assumptions if a.kind == ASSUME_PIN_START}
    multipliers = dict(config.priority_multipliers)
    for a in assumptions:
        if a.kind == ASSUME_PRIORITY:
            multipliers[a.project_id] = float(a.value["multiplier"])
    config = SolveConfig(horizon=config.horizon, caps=config.caps,
                         metric_values=config.metric_values,
                         risk_weight=config.risk_weight,
                         benefit_weight=config.benefit_weight,
                         overlap_policy=config.overlap_policy,
                         priority_multipliers=multipliers)

    # 锁定假设一致性校验
    for pid in sorted(locked_in & locked_out):
        raise InfeasibleError(f"项目 {pid} 同时被锁定入选与落选，假设矛盾")
    for pid in sorted(locked_in):
        if pid not in projects:
            raise InfeasibleError(f"锁定入选的项目 {pid} 不存在")
        bad = [q for q in transitive_prerequisites(graph, pid) if q in locked_out]
        if bad:
            raise InfeasibleError(
                f"项目 {pid} 被锁定入选，但其前置项目 {', '.join(bad)} 被锁定落选，假设矛盾")

    # 资金额度假设覆盖
    caps = {s: dict(years) for s, years in config.caps.items()}
    for a in assumptions:
        if a.kind == ASSUME_CAP_OVERRIDE:
            caps.setdefault(a.value["source"], {})[int(a.value["year"])] = int(a.value["cap"])

    horizon_start, horizon_end = config.horizon
    exclusions = _exclusion_map(projects)
    remaining = {s: dict(years) for s, years in caps.items()}
    scores = {pid: priority_score(p, config) for pid, p in projects.items()}
    order = sorted(projects, key=lambda pid: (pid not in locked_in, -scores[pid], pid))
    rank = {pid: i + 1 for i, pid in enumerate(order)}

    decisions: list[DecisionRecord] = []
    items: list[ScenarioItem] = []
    selected: dict[str, ScenarioItem] = {}
    pending: dict[str, dict] = {}   # 暂存的落选原因(可能因后续选择改变)

    # 锁定落选最先记录
    for pid in order:
        if pid in locked_out:
            decisions.append(DecisionRecord(
                project_id=pid, decision=Decision.EXCLUDED.value,
                reason_code=ReasonCode.LOCKED_EXCLUDE.value,
                reason="评审人锁定落选",
                details={"score": round(scores[pid], 2), "rank": rank[pid]}))

    def try_schedule(pid: str) -> tuple[int | None, dict | None, dict]:
        """返回 (开工年, 资金计划, 失败信息)。"""
        p = projects[pid]
        failure: dict = {"budget_shortfalls": [], "exclusion_conflicts": [],
                         "overlap_conflicts": []}
        prereq_ends = [selected[q].start_year + projects[q].duration
                       for q in graph[pid] if q in selected]
        earliest = max([p.earliest_start, horizon_start, *prereq_ends])
        candidates = [pins[pid]] if pid in pins else range(
            earliest, horizon_end - p.duration + 2)
        for start in candidates:
            if start < earliest or start + p.duration - 1 > horizon_end:
                continue
            # 互斥施工: 窗口重叠检查
            conflict = None
            for other, scope in exclusions.get(pid, {}).items():
                if other not in selected:
                    continue
                o = selected[other]
                if scope == EXCLUSION_SAME_YEAR and _windows_overlap(
                        start, p.duration, o.start_year, projects[other].duration):
                    conflict = other
                    break
            if conflict:
                failure["exclusion_conflicts"].append({"year": start, "with": conflict})
                continue
            # 范围重叠禁止策略
            if config.overlap_policy == OVERLAP_FORBID:
                shared = [o for o in selected
                          if set(projects[o].segments) & set(p.segments)]
                if shared:
                    failure["overlap_conflicts"].append({"year": start, "with": sorted(shared)})
                    continue
            # 预算占用: 逐年在可用资金来源间分配
            plan: dict[int, dict[str, int]] = {}
            shortfall = None
            for i, cost in enumerate(p.cost_curve):
                year = start + i
                need = cost
                alloc: dict[str, int] = {}
                for src in p.funding_sources:
                    avail = remaining.get(src, {}).get(year, 0)
                    take = min(avail, need)
                    if take > 0:
                        alloc[src] = take
                        need -= take
                    if need == 0:
                        break
                if need > 0:
                    shortfall = {"year": year, "short": need,
                                 "available": cost - need, "required": cost}
                    break
                plan[year] = alloc
            if shortfall:
                failure["budget_shortfalls"].append(shortfall)
                continue
            return start, plan, failure
        return None, None, failure

    def commit(pid: str, start: int, plan: dict, locked: bool) -> None:
        item = ScenarioItem(project_id=pid, version=projects[pid].version,
                            start_year=start, funding_plan=plan,
                            score=round(scores[pid], 2), locked=locked)
        for year, allocs in plan.items():
            for src, amount in allocs.items():
                remaining[src][year] -= amount
        selected[pid] = item
        items.append(item)
        decisions.append(DecisionRecord(
            project_id=pid, decision=Decision.INCLUDED.value,
            reason_code=(ReasonCode.LOCKED_INCLUDE if locked else ReasonCode.SELECTED).value,
            reason=("评审人锁定入选" if locked else
                    f"按优先级得分入选(第 {rank[pid]} 位, 得分 {scores[pid]:.2f})"),
            details={"score": round(scores[pid], 2), "rank": rank[pid],
                     "start_year": start, "funding_plan": {str(y): dict(a) for y, a in plan.items()}}))

    def fail_reason(pid: str, failure: dict) -> DecisionRecord:
        p = projects[pid]
        if failure["exclusion_conflicts"] and not failure["budget_shortfalls"]:
            others = sorted({c["with"] for c in failure["exclusion_conflicts"]})
            code, reason = ReasonCode.EXCLUSION_CONFLICT, f"与入选项目 {', '.join(others)} 互斥施工"
        elif failure["overlap_conflicts"] and not failure["budget_shortfalls"]:
            others = sorted({x for c in failure["overlap_conflicts"] for x in c["with"]})
            code, reason = ReasonCode.OVERLAP_CONFLICT, f"与入选项目 {', '.join(others)} 管段范围重叠"
        elif failure["budget_shortfalls"]:
            s = failure["budget_shortfalls"][0]
            code = ReasonCode.BUDGET_INSUFFICIENT
            reason = (f"{s['year']} 年资金不足: 需 {s['required']:,} 元, "
                      f"可用 {s['available']:,} 元, 缺口 {s['short']:,} 元")
        else:
            code = ReasonCode.WINDOW_INFEASIBLE
            reason = (f"规划窗口 {horizon_start}-{horizon_end} 内无法安排 "
                      f"{p.duration} 年工期(最早开工 {p.earliest_start})")
        return DecisionRecord(project_id=pid, decision=Decision.EXCLUDED.value,
                              reason_code=code.value, reason=reason,
                              details={"score": round(scores[pid], 2), "rank": rank[pid],
                                       "failures": failure})

    # 组合级互斥(不同组合不得同时入选)在就绪检查中处理
    def portfolio_blocked(pid: str) -> str | None:
        for other, scope in exclusions.get(pid, {}).items():
            if scope == EXCLUSION_PORTFOLIO and other in selected:
                return other
        return None

    # 多轮扫描: 前置就绪的项目按优先级尝试安排
    candidates = [pid for pid in order if pid not in locked_out]
    while True:
        progressed = False
        for pid in list(candidates):
            if pid in selected:
                candidates.remove(pid)
                continue
            unready = [q for q in graph[pid] if q not in selected]
            if unready:
                continue  # 等待前置项目先入选
            blocker = portfolio_blocked(pid)
            if blocker:
                decisions.append(DecisionRecord(
                    project_id=pid, decision=Decision.EXCLUDED.value,
                    reason_code=ReasonCode.EXCLUSION_CONFLICT.value,
                    reason=f"与入选项目 {blocker} 互斥(同一组合不得同时实施)",
                    details={"score": round(scores[pid], 2), "rank": rank[pid],
                             "failures": {"exclusion_conflicts": [{"with": blocker}]}}))
                candidates.remove(pid)
                progressed = True
                continue
            start, plan, failure = try_schedule(pid)
            if start is None:
                if pid in locked_in:
                    rec = fail_reason(pid, failure)
                    raise InfeasibleError(
                        f"锁定入选的项目 {pid} 无法安排: {rec.reason}")
                pending[pid] = failure
                continue
            commit(pid, start, plan, locked=pid in locked_in)
            candidates.remove(pid)
            progressed = True
        if not progressed:
            break

    # 锁定入选的项目若最终未能安排, 假设与约束矛盾
    for pid in sorted(locked_in - set(selected)):
        failure = pending.get(pid, {"budget_shortfalls": [], "exclusion_conflicts": [],
                                    "overlap_conflicts": []})
        unready = [q for q in graph[pid] if q not in selected]
        if unready:
            raise InfeasibleError(
                f"锁定入选的项目 {pid} 的前置项目未能入选: {', '.join(sorted(unready))}")
        raise InfeasibleError(
            f"锁定入选的项目 {pid} 无法安排: {fail_reason(pid, failure).reason}")

    # 剩余未入选项目记录落选原因
    for pid in candidates:
        unready = [q for q in graph[pid] if q not in selected]
        if unready:
            decisions.append(DecisionRecord(
                project_id=pid, decision=Decision.EXCLUDED.value,
                reason_code=ReasonCode.DEPENDENCY_UNMET.value,
                reason=f"前置项目未入选: {', '.join(sorted(unready))}",
                details={"score": round(scores[pid], 2), "rank": rank[pid],
                         "missing_prerequisites": sorted(unready)}))
        else:
            decisions.append(fail_reason(pid, pending.get(pid, {
                "budget_shortfalls": [], "exclusion_conflicts": [], "overlap_conflicts": []})))

    selected_ids = set(selected)
    overlaps = detect_overlaps(projects, selected_ids)
    total, by_metric, attribution = dedupe_benefits(projects, items)
    return SolveResult(items=items, decisions=decisions, overlaps=overlaps,
                       benefit_total=total, benefit_by_metric=by_metric,
                       benefit_attribution=attribution)


def validate_items(items: list[ScenarioItem],
                   projects: dict[str, ProjectVersionRecord],
                   caps: dict[str, dict[int, int]]) -> list[str]:
    """对一组入选项目做一致性校验(用于基线调整单应用前复核)。"""
    violations: list[str] = []
    by_pid = {i.project_id: i for i in items}
    graph = dependency_graph(projects)
    cycles = find_cycles(graph)
    if cycles:
        violations.append("项目依赖存在环: " + "; ".join(" -> ".join(c) for c in cycles))

    # 前置依赖: 前置必须入选且完工不晚于后继开工
    for i in items:
        for q in graph.get(i.project_id, []):
            if q not in by_pid:
                violations.append(f"项目 {i.project_id} 的前置项目 {q} 未入选")
            elif by_pid[q].start_year + projects[q].duration > i.start_year:
                violations.append(
                    f"项目 {i.project_id} 开工年 {i.start_year} 早于前置项目 {q} 完工年 "
                    f"{by_pid[q].start_year + projects[q].duration}")

    # 互斥施工
    exclusions = _exclusion_map(projects)
    for i in items:
        for other, scope in exclusions.get(i.project_id, {}).items():
            if other not in by_pid or other <= i.project_id:
                continue
            o = by_pid[other]
            if scope == EXCLUSION_PORTFOLIO:
                violations.append(f"项目 {i.project_id} 与 {other} 互斥, 不得同时入选")
            elif _windows_overlap(i.start_year, projects[i.project_id].duration,
                                  o.start_year, projects[other].duration):
                violations.append(f"项目 {i.project_id} 与 {other} 施工窗口重叠, 互斥施工")

    # 预算占用: 年度分来源合计不得超过额度
    usage: dict[str, dict[int, int]] = {}
    for i in items:
        for year, allocs in i.funding_plan.items():
            for src, amount in allocs.items():
                if src not in projects[i.project_id].funding_sources:
                    violations.append(
                        f"项目 {i.project_id} 使用了其不可用的资金来源 {src}")
                usage.setdefault(src, {}).setdefault(year, 0)
                usage[src][year] += amount
        # 资金计划必须覆盖成本曲线
        for k, cost in enumerate(projects[i.project_id].cost_curve):
            year = i.start_year + k
            planned = sum(i.funding_plan.get(year, {}).values())
            if planned != cost:
                violations.append(
                    f"项目 {i.project_id} {year} 年资金计划 {planned:,} 元 "
                    f"与成本曲线 {cost:,} 元不一致")
    for src, years in usage.items():
        for year, amount in years.items():
            cap = caps.get(src, {}).get(year)
            if cap is None:
                violations.append(f"资金来源 {src} 在 {year} 年没有额度, 已占用 {amount:,} 元")
            elif amount > cap:
                violations.append(
                    f"资金来源 {src} {year} 年超支: 占用 {amount:,} 元 > 额度 {cap:,} 元")
    return violations
