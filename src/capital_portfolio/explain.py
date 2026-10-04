"""可复核解释：入选/落选决定、依赖传播、资金余量与情景差异。"""
from __future__ import annotations

from .analysis import (
    dependency_graph, propagate_delay, propagate_removal, transitive_dependents,
    transitive_prerequisites,
)
from .domain import Decision, ProjectVersionRecord, ScenarioRecord


def selection(scenario: ScenarioRecord) -> dict:
    """每个项目入选或落选的决定与原因。"""
    included, excluded = [], []
    for d in sorted(scenario.decisions, key=lambda x: x.details.get("rank", 9999)):
        row = {"project_id": d.project_id, "decision": d.decision,
               "reason_code": d.reason_code, "reason": d.reason,
               "details": d.details}
        (included if d.decision == Decision.INCLUDED.value else excluded).append(row)
    return {
        "scenario_id": scenario.scenario_id, "revision": scenario.revision,
        "summary": {
            "included": len(included), "excluded": len(excluded),
            "total_cost": sum(
                sum(sum(a.values()) for a in i.funding_plan.values())
                for i in scenario.items),
            "benefit_total": scenario.benefit_total,
            "benefit_by_metric": scenario.benefit_by_metric,
        },
        "included": included, "excluded": excluded,
        "overlaps": list(scenario.overlaps),
        "assumptions": [a.to_dict() for a in scenario.assumptions],
    }


def dependencies(scenario: ScenarioRecord,
                 projects: dict[str, ProjectVersionRecord],
                 project_id: str) -> dict:
    """某项目的依赖链与延后/移除的传播影响。"""
    graph = dependency_graph(projects)
    items = list(scenario.items)
    starts = {i.project_id: i.start_year for i in items}
    upstream = transitive_prerequisites(graph, project_id)
    downstream = transitive_dependents(graph, project_id)
    removal = propagate_removal(graph, items, project_id)
    delay = propagate_delay(graph, projects, items, project_id, 1) \
        if project_id in starts else []
    return {
        "scenario_id": scenario.scenario_id, "project_id": project_id,
        "selected": project_id in starts,
        "start_year": starts.get(project_id),
        "direct_prerequisites": sorted(graph.get(project_id, [])),
        "all_prerequisites": upstream,
        "direct_dependents": sorted(
            pid for pid, pres in graph.items() if project_id in pres),
        "all_dependents": downstream,
        "if_removed": {
            "affected_projects": removal,
            "message": (f"若 {project_id} 被移出组合, 以下入选项目的依赖将失效"
                        if removal else "移除该项目不影响其他入选项目"),
        },
        "if_delayed_one_year": {
            "affected_projects": delay,
            "message": (f"若 {project_id} 延后一年, 以下项目开工年需顺延"
                        if delay else "该项目延后一年不影响其他入选项目的排程"),
        },
    }


def funding(scenario: ScenarioRecord, caps: dict[str, dict[int, int]]) -> dict:
    """分年度分来源的预算占用与资金余量。"""
    start, end = scenario.horizon
    usage: dict[str, dict[int, int]] = {}
    for item in scenario.items:
        for year, allocs in item.funding_plan.items():
            for src, amount in allocs.items():
                usage.setdefault(src, {}).setdefault(year, 0)
                usage[src][year] += amount
    years = []
    for year in range(start, end + 1):
        sources = []
        for src in sorted(set(caps) | set(usage)):
            cap = caps.get(src, {}).get(year, 0)
            used = usage.get(src, {}).get(year, 0)
            sources.append({
                "source_id": src, "cap": cap, "allocated": used, "margin": cap - used,
                "margin_pct": round((cap - used) / cap * 100, 2) if cap else None,
                "items": [
                    {"project_id": i.project_id,
                     "amount": i.funding_plan.get(year, {}).get(src, 0)}
                    for i in scenario.items
                    if i.funding_plan.get(year, {}).get(src, 0) > 0],
            })
        years.append({"year": year, "sources": sources,
                      "total_cap": sum(s["cap"] for s in sources),
                      "total_allocated": sum(s["allocated"] for s in sources),
                      "total_margin": sum(s["margin"] for s in sources)})
    return {"scenario_id": scenario.scenario_id, "revision": scenario.revision,
            "years": years}


def diff(a: ScenarioRecord, b: ScenarioRecord) -> dict:
    """两个情景之间的可复核差异。"""
    a_items, b_items = a.item_map(), b.item_map()
    added = sorted(set(b_items) - set(a_items))
    removed = sorted(set(a_items) - set(b_items))
    changed = []
    for pid in sorted(set(a_items) & set(b_items)):
        ia, ib = a_items[pid], b_items[pid]
        delta = {}
        if ia.version != ib.version:
            delta["version"] = {"from": ia.version, "to": ib.version}
        if ia.start_year != ib.start_year:
            delta["start_year"] = {"from": ia.start_year, "to": ib.start_year}
        if ia.funding_plan != ib.funding_plan:
            delta["funding_plan"] = {
                "from": {str(y): v for y, v in ia.funding_plan.items()},
                "to": {str(y): v for y, v in ib.funding_plan.items()}}
        if delta:
            changed.append({"project_id": pid, **delta})

    def year_totals(rec: ScenarioRecord) -> dict[int, int]:
        totals: dict[int, int] = {}
        for i in rec.items:
            for year, allocs in i.funding_plan.items():
                totals[year] = totals.get(year, 0) + sum(allocs.values())
        return totals

    ta, tb = year_totals(a), year_totals(b)
    budget_delta = [{"year": y, "from": ta.get(y, 0), "to": tb.get(y, 0),
                     "delta": tb.get(y, 0) - ta.get(y, 0)}
                    for y in sorted(set(ta) | set(tb)) if ta.get(y, 0) != tb.get(y, 0)]

    a_dec = {d.project_id: d for d in a.decisions}
    b_dec = {d.project_id: d for d in b.decisions}
    decision_changes = [
        {"project_id": pid,
         "from": {"decision": a_dec[pid].decision, "reason": a_dec[pid].reason},
         "to": {"decision": b_dec[pid].decision, "reason": b_dec[pid].reason}}
        for pid in sorted(set(a_dec) & set(b_dec))
        if a_dec[pid].decision != b_dec[pid].decision]

    a_asm = {(x.kind, x.project_id, str(sorted(x.value.items()))) for x in a.assumptions}
    b_asm = {(x.kind, x.project_id, str(sorted(x.value.items()))) for x in b.assumptions}
    return {
        "from_scenario": {"id": a.scenario_id, "revision": a.revision, "name": a.name},
        "to_scenario": {"id": b.scenario_id, "revision": b.revision, "name": b.name},
        "items_added": added, "items_removed": removed, "items_changed": changed,
        "decision_changes": decision_changes,
        "budget_delta_by_year": budget_delta,
        "benefit_delta": {"from": a.benefit_total, "to": b.benefit_total,
                          "delta": b.benefit_total - a.benefit_total},
        "assumptions_added": sorted(b_asm - a_asm),
        "assumptions_removed": sorted(a_asm - b_asm),
    }
