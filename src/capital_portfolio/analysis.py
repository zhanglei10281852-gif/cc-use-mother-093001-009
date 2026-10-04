"""范围重叠、收益冲突、依赖传播与互斥分析。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from .contracts import ProjectVersion


@dataclass(frozen=True)
class AssetOverlap:
    """同一管段被多个项目版本申报。"""

    asset_id: str
    project_ids: tuple[str, ...]
    claimed_by_project: Mapping[str, int]
    claimed_total: int
    duplicated_benefit: int


def _latest_map(projects: Iterable[ProjectVersion]) -> dict[str, ProjectVersion]:
    latest: dict[str, ProjectVersion] = {}
    for pv in projects:
        old = latest.get(pv.project_id)
        if old is None or pv.version > old.version:
            latest[pv.project_id] = pv
    return latest


def find_scope_overlaps(projects: Iterable[ProjectVersion]) -> list[AssetOverlap]:
    """按管段归集，找出资产范围重叠及由此产生的重复申报收益。"""
    latest = _latest_map(projects)
    asset_claims: dict[str, dict[str, int]] = defaultdict(dict)
    asset_members: dict[str, set[str]] = defaultdict(set)
    for pv in latest.values():
        for asset in pv.asset_ids:
            asset_members[asset].add(pv.project_id)
            ben = pv.benefit_by_asset.get(asset, 0)
            if ben:
                asset_claims[asset][pv.project_id] = ben

    overlaps: list[AssetOverlap] = []
    for asset in sorted(asset_members):
        members = asset_members[asset]
        if len(members) <= 1:
            continue
        claims = asset_claims.get(asset, {})
        claimed_total = sum(claims.values())
        # 同一管段只认可一次收益：保留最高单笔申报，其余为重复计算。
        duplicated = claimed_total - max(claims.values(), default=0)
        overlaps.append(
            AssetOverlap(
                asset_id=asset,
                project_ids=tuple(sorted(members)),
                claimed_by_project=dict(sorted(claims.items())),
                claimed_total=claimed_total,
                duplicated_benefit=duplicated,
            )
        )
    return overlaps


def attribute_benefits(
    ordered_projects: Iterable[ProjectVersion],
) -> tuple[dict[str, int], dict[str, str], int]:
    """按优先级顺序把每条管段的收益只归属给第一个申报项目。

    返回 ``(各项目认可收益, 管段->胜出项目, 被消除的重复收益合计)``。
    未申报收益或零收益的管段不参与归属。
    """
    recognized: dict[str, int] = {}
    winner: dict[str, str] = {}
    claimed_total = 0
    for pv in ordered_projects:
        recognized.setdefault(pv.project_id, 0)
        for asset, benefit in pv.benefit_by_asset.items():
            if benefit <= 0:
                continue
            claimed_total += benefit
            if asset not in winner:
                winner[asset] = pv.project_id
                recognized[pv.project_id] += benefit
    duplicated = claimed_total - sum(recognized.values())
    return recognized, winner, duplicated


def build_reverse_dependencies(
    projects: Iterable[ProjectVersion],
) -> dict[str, set[str]]:
    """project_id -> 直接（或间接）依赖它的项目集合。"""
    latest = _latest_map(projects)
    direct: dict[str, set[str]] = defaultdict(set)
    for pv in latest.values():
        for dep in pv.depends_on:
            direct[dep.project_id].add(pv.project_id)
    reverse: dict[str, set[str]] = {}

    def closure(pid: str) -> set[str]:
        if pid in reverse:
            return reverse[pid]
        result: set[str] = set()
        stack = list(direct.get(pid, ()))
        seen = {pid}
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            result.add(cur)
            stack.extend(direct.get(cur, ()))
        reverse[pid] = result
        return result

    for pid in latest:
        closure(pid)
    return reverse


def transitive_dependents(pid: str, reverse: Mapping[str, Iterable[str]]) -> set[str]:
    return set(reverse.get(pid, ()))


def find_dependency_cycles(projects: Iterable[ProjectVersion]) -> list[list[str]]:
    """返回依赖图中的简单环（Tarjan 强连通分量，大小>1 或自环）。"""
    latest = _latest_map(projects)
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    cycles: list[list[str]] = []

    adj = {pid: [d.project_id for d in pv.depends_on if d.project_id in latest]
           for pid, pv in latest.items()}

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj[v]:
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1 or v in adj[v]:
                cycles.append(sorted(component))

    import sys
    sys.setrecursionlimit(max(1000, len(latest) * 10 + 100))
    for pid in sorted(latest):
        if pid not in indices:
            strongconnect(pid)
    return sorted(cycles, key=lambda c: (len(c), c))


def mutex_pairs(projects: Iterable[ProjectVersion]) -> dict[str, tuple[str, ...]]:
    """互斥组 -> 组内项目标识。"""
    latest = _latest_map(projects)
    groups: dict[str, set[str]] = defaultdict(set)
    for pv in latest.values():
        for group in pv.mutex_groups:
            groups[group].add(pv.project_id)
    return {g: tuple(sorted(members)) for g, members in sorted(groups.items())
            if members}
