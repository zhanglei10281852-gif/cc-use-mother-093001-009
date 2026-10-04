"""范围重叠与收益冲突识别、依赖图与依赖传播分析。"""
from __future__ import annotations

from .domain import DomainError, ProjectVersionRecord, ScenarioItem


# ---------------------------------------------------------------------------
# 范围重叠与收益冲突
# ---------------------------------------------------------------------------

def detect_overlaps(projects: dict[str, ProjectVersionRecord],
                    selected: set[str] | None = None) -> list[dict]:
    """识别管段范围重叠与收益重复计算。

    - 范围重叠：同一管段被多个项目覆盖；
    - 收益冲突：同一 (管段, 指标) 的收益被多个项目重复申报。
    selected 给定时不限于全部项目，只评估入选集合内的冲突。
    """
    pool = {pid: p for pid, p in projects.items() if selected is None or pid in selected}
    findings: list[dict] = []

    seg_owners: dict[str, list[tuple[str, float]]] = {}
    for pid, p in pool.items():
        for seg, length in p.segments.items():
            seg_owners.setdefault(seg, []).append((pid, length))
    for seg, owners in sorted(seg_owners.items()):
        if len(owners) > 1:
            findings.append({
                "kind": "SCOPE_OVERLAP", "segment_id": seg,
                "projects": sorted(pid for pid, _ in owners),
                "overlap_length_m": min(length for _, length in owners),
                "message": f"管段 {seg} 被 {len(owners)} 个项目重复覆盖: "
                           + ", ".join(sorted(pid for pid, _ in owners)),
            })

    benefit_claims: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for pid, p in pool.items():
        for b in p.benefits:
            benefit_claims.setdefault((b.segment_id, b.metric), []).append((pid, b.value))
    for (seg, metric), claims in sorted(benefit_claims.items()):
        if len(claims) > 1:
            claimed = sum(v for _, v in claims)
            attributed = max(v for _, v in claims)
            findings.append({
                "kind": "BENEFIT_CONFLICT", "segment_id": seg, "metric": metric,
                "claims": [{"project_id": pid, "value": v} for pid, v in sorted(claims)],
                "claimed_total": claimed, "deduplicated": attributed,
                "double_counted": claimed - attributed,
                "message": f"管段 {seg} 指标 {metric} 收益被重复申报 "
                           f"{claimed:,.0f}, 去重后计 {attributed:,.0f}",
            })
    return findings


def dedupe_benefits(projects: dict[str, ProjectVersionRecord],
                    items: list[ScenarioItem]) -> tuple[float, dict[str, float], list[dict]]:
    """对入选集合计算去重后的收益。

    同一 (管段, 指标) 的多个申报归属开工最早(并列取项目号小)的项目，
    其余记为重复计算。返回 (总收益, 分指标收益, 归属明细)。
    """
    starts = {i.project_id: i.start_year for i in items}
    claims: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for i in items:
        p = projects[i.project_id]
        for b in p.benefits:
            claims.setdefault((b.segment_id, b.metric), []).append((i.project_id, b.value))

    total = 0.0
    by_metric: dict[str, float] = {}
    attribution: list[dict] = []
    for (seg, metric), cs in sorted(claims.items()):
        owner = min(cs, key=lambda c: (starts[c[0]], c[0]))[0]
        for pid, value in sorted(cs):
            if pid == owner:
                total += value
                by_metric[metric] = by_metric.get(metric, 0.0) + value
            attribution.append({
                "segment_id": seg, "metric": metric, "project_id": pid,
                "value": value, "counted": pid == owner,
                "note": "" if pid == owner else f"重复计算, 收益归属 {owner}",
            })
    return total, by_metric, attribution


# ---------------------------------------------------------------------------
# 依赖图
# ---------------------------------------------------------------------------

def dependency_graph(projects: dict[str, ProjectVersionRecord]) -> dict[str, list[str]]:
    """项目 -> 前置项目列表，并校验前置项目存在。"""
    graph: dict[str, list[str]] = {}
    for pid, p in projects.items():
        missing = [q for q in p.prerequisites if q not in projects]
        if missing:
            raise DomainError(f"项目 {pid} 的前置项目不存在: {', '.join(missing)}")
        graph[pid] = list(p.prerequisites)
    return graph


def find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """检测依赖环，返回环路径列表。"""
    cycles: list[list[str]] = []
    state: dict[str, int] = {}  # 0=未访问 1=在栈中 2=完成
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt, 0) == 0:
                visit(nxt)
            elif state.get(nxt) == 1:
                cycles.append(stack[stack.index(nxt):] + [nxt])
        stack.pop()
        state[node] = 2

    for n in graph:
        if state.get(n, 0) == 0:
            visit(n)
    return cycles


def dependents_map(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    """项目 -> 直接后继(依赖它的项目)。"""
    out: dict[str, list[str]] = {pid: [] for pid in graph}
    for pid, prereqs in graph.items():
        for q in prereqs:
            out.setdefault(q, []).append(pid)
    return out


def transitive_dependents(graph: dict[str, list[str]], project_id: str) -> list[str]:
    """依赖该项目的全部下游项目(拓扑序)。"""
    deps = dependents_map(graph)
    seen: list[str] = []
    queue = list(deps.get(project_id, []))
    while queue:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.append(node)
        queue.extend(deps.get(node, []))
    return seen


def transitive_prerequisites(graph: dict[str, list[str]], project_id: str) -> list[str]:
    """该项目的全部间接前置项目。"""
    seen: list[str] = []
    queue = list(graph.get(project_id, []))
    while queue:
        node = queue.pop(0)
        if node in seen:
            continue
        seen.append(node)
        queue.extend(graph.get(node, []))
    return seen


# ---------------------------------------------------------------------------
# 依赖传播
# ---------------------------------------------------------------------------

def propagate_removal(graph: dict[str, list[str]], items: list[ScenarioItem],
                      project_id: str) -> list[dict]:
    """模拟某项目被移出组合：哪些入选项目的依赖随之失效。"""
    selected = {i.project_id for i in items}
    affected: list[dict] = []
    removed = {project_id}
    for pid in transitive_dependents(graph, project_id):
        if pid not in selected:
            continue
        missing = [q for q in graph.get(pid, []) if q in removed]
        if missing:
            affected.append({
                "project_id": pid,
                "cause": f"前置项目 {', '.join(sorted(missing))} 被移除",
                "missing_prerequisites": sorted(missing),
            })
            removed.add(pid)
    return affected


def propagate_delay(graph: dict[str, list[str]], projects: dict[str, ProjectVersionRecord],
                    items: list[ScenarioItem], project_id: str,
                    delay_years: int) -> list[dict]:
    """模拟某项目延后 delay_years 年：下游项目开工年需要顺延的量。

    约束为 后继开工年 >= 前置完工年(前置开工年 + 工期)。
    """
    starts = {i.project_id: i.start_year for i in items}
    ends = {pid: starts[pid] + projects[pid].duration for pid in starts}
    ends[project_id] += delay_years
    shifts: dict[str, int] = {}
    queue = [project_id]
    while queue:
        node = queue.pop(0)
        for nxt in dependents_map(graph).get(node, []):
            if nxt not in starts:
                continue
            required_start = ends[node]
            current_start = starts[nxt] + shifts.get(nxt, 0)
            if current_start < required_start:
                shift = required_start - current_start
                shifts[nxt] = shifts.get(nxt, 0) + shift
                ends[nxt] = starts[nxt] + shifts[nxt] + projects[nxt].duration
                queue.append(nxt)
    return [
        {"project_id": pid, "original_start": starts[pid],
         "required_start": starts[pid] + shift, "delay_years": shift,
         "cause": f"上游项目 {project_id} 延后 {delay_years} 年"}
        for pid, shift in sorted(shifts.items())
    ]
