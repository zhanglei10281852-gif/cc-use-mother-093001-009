"""投资组合治理服务：情景生命周期、审批发布、基线调整单、实际回填与偏差预警。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Callable

from . import explain
from .analysis import (
    dependency_graph, detect_overlaps, find_cycles, propagate_removal,
)
from .domain import (
    ASSUME_CAP_OVERRIDE, ASSUME_EXCLUDE, ASSUME_INCLUDE, ASSUME_PIN_START,
    ASSUME_PRIORITY, ActualEntry, AlertRecord, Assumption, BaselineRecord,
    ChangeOrderRecord, ChangeOrderStatus, ChangeOrderType, ConflictError,
    DomainError, FundingSource, InfeasibleError, NotFoundError,
    ProjectVersionRecord, RiskEvidence, BenefitClaim, ScenarioItem,
    ScenarioRecord, ScenarioStatus,
)
from .planner import SolveConfig, solve, validate_items
from .store import Store

DEFAULT_METRIC_VALUES = {
    "leak_reduction_m3": 3.0,
    "risk_events_avoided": 200000.0,
    "people_served": 50.0,
}
ASSUMPTION_KINDS = {ASSUME_INCLUDE, ASSUME_EXCLUDE, ASSUME_PIN_START,
                    ASSUME_CAP_OVERRIDE, ASSUME_PRIORITY}
COST_DEVIATION_THRESHOLD_PCT = 10.0     # 成本偏差预警阈值
MILEAGE_LAG_THRESHOLD_PCT = 15.0        # 里程滞后预警阈值


def _parse_period(period: str) -> tuple[int, int]:
    """'2027' -> (2027, 4); '2027-Q2' -> (2027, 2)。年度口径视为年末。"""
    try:
        if "-Q" in period:
            y, q = period.split("-Q")
            quarter = int(q)
            if not 1 <= quarter <= 4:
                raise ValueError
            return int(y), quarter
        return int(period), 4
    except (ValueError, AttributeError):
        raise DomainError(f"期间格式无效: {period}") from None


class PortfolioService:
    def __init__(self, store: Store, clock: Callable[[], date] = date.today):
        self.store = store
        self._clock = clock

    def _today(self) -> str:
        return self._clock().isoformat()

    # ------------------------------------------------------------------
    # 项目版本
    # ------------------------------------------------------------------
    def submit_project_version(self, payload: dict) -> ProjectVersionRecord:
        required = ["project_id", "version", "name", "segments", "cost_curve",
                    "earliest_start", "funding_sources"]
        missing = [k for k in required if k not in payload]
        if missing:
            raise DomainError(f"项目版本缺少字段: {', '.join(missing)}")
        rec = ProjectVersionRecord(
            project_id=str(payload["project_id"]), version=int(payload["version"]),
            name=str(payload["name"]),
            segments={str(k): float(v) for k, v in payload["segments"].items()},
            cost_curve=tuple(int(x) for x in payload["cost_curve"]),
            earliest_start=int(payload["earliest_start"]),
            risk_evidence=tuple(RiskEvidence.from_dict(x)
                                for x in payload.get("risk_evidence", [])),
            prerequisites=tuple(payload.get("prerequisites", [])),
            funding_sources=tuple(payload.get("funding_sources", [])),
            benefits=tuple(BenefitClaim.from_dict(x) for x in payload.get("benefits", [])),
            exclusive_with=tuple((x[0], x[1]) for x in payload.get("exclusive_with", [])),
            submitted_at=payload.get("submitted_at") or self._today(),
            submitted_by=payload.get("submitted_by", ""))
        self._validate_project(rec)
        self.store.save_project_version(rec)
        self.store.audit(self._today(), rec.submitted_by or "system",
                         "SUBMIT_PROJECT_VERSION", rec.project_id,
                         {"version": rec.version, "total_cost": rec.total_cost})
        return rec

    def _validate_project(self, rec: ProjectVersionRecord) -> None:
        if rec.version < 1:
            raise DomainError("版本号必须 >= 1")
        existing = self.store.list_versions(rec.project_id)
        if any(v.version == rec.version for v in existing):
            raise ConflictError(f"项目 {rec.project_id} 版本 {rec.version} 已存在")
        if existing and rec.version < max(v.version for v in existing):
            raise DomainError(f"项目 {rec.project_id} 新版本号必须大于已有版本")
        if not rec.segments or any(v <= 0 for v in rec.segments.values()):
            raise DomainError("资产范围不能为空且长度必须为正")
        if not rec.cost_curve or any(c <= 0 for c in rec.cost_curve):
            raise DomainError("成本曲线不能为空且各年成本必须为正")
        if rec.earliest_start < 2026:
            raise DomainError("最早开工年无效")
        if not rec.funding_sources:
            raise DomainError("至少需要一个可用资金来源")
        for src in rec.funding_sources:
            if not self.store.get_funding_source(src):
                raise DomainError(f"资金来源 {src} 不存在")
        for e in rec.risk_evidence:
            if not (1 <= e.severity <= 5 and 1 <= e.likelihood <= 5):
                raise DomainError("风险证据的严重度与可能性须在 1-5 之间")
        for _, scope in rec.exclusive_with:
            if scope not in ("same_year", "portfolio"):
                raise DomainError(f"互斥范围无效: {scope}")
        # 与现有最新版本合并后检查依赖环
        pool = self.store.latest_versions()
        pool[rec.project_id] = rec
        cycles = find_cycles(dependency_graph(pool))
        if cycles:
            raise DomainError(
                "项目依赖存在环: " + "; ".join(" -> ".join(c) for c in cycles))

    def list_projects(self) -> list[dict]:
        return [p.to_dict() for p in self.store.latest_versions().values()]

    def get_project_versions(self, project_id: str) -> list[dict]:
        versions = self.store.list_versions(project_id)
        if not versions:
            raise NotFoundError(f"项目 {project_id} 不存在")
        return [v.to_dict() for v in versions]

    # ------------------------------------------------------------------
    # 资金来源
    # ------------------------------------------------------------------
    def upsert_funding_source(self, payload: dict) -> FundingSource:
        src = FundingSource(
            source_id=str(payload["source_id"]), name=str(payload["name"]),
            annual_caps={int(y): int(a) for y, a in payload["annual_caps"].items()},
            updated_at=payload.get("updated_at") or self._today())
        if any(v < 0 for v in src.annual_caps.values()):
            raise DomainError("资金额度不能为负")
        self.store.save_funding_source(src)
        self.store.audit(self._today(), "system", "UPSERT_FUNDING_SOURCE",
                         src.source_id, {"annual_caps": src.to_dict()["annual_caps"]})
        return src

    def list_funding_sources(self) -> list[dict]:
        return [s.to_dict() for s in self.store.list_funding_sources()]

    # ------------------------------------------------------------------
    # 情景
    # ------------------------------------------------------------------
    def create_scenario(self, name: str, horizon: tuple[int, int], created_by: str,
                        project_versions: dict[str, int] | None = None,
                        config: dict | None = None, valid_days: int = 90,
                        notes: str = "") -> ScenarioRecord:
        if horizon[0] > horizon[1]:
            raise DomainError("规划窗口起年不能晚于止年")
        latest = self.store.latest_versions()
        pins = dict(project_versions or {})
        for pid, v in pins.items():
            if not self.store.get_project_version(pid, v):
                raise NotFoundError(f"项目 {pid} 版本 {v} 不存在")
        for pid in latest:
            pins.setdefault(pid, latest[pid].version)
        cfg = {"metric_values": dict(DEFAULT_METRIC_VALUES), "risk_weight": 1.0,
               "benefit_weight": 1.0, "overlap_policy": "dedupe",
               "project_versions": pins}
        cfg.update(config or {})
        rec = ScenarioRecord(
            scenario_id=self.store.next_id("SCN"), revision=1, name=name,
            horizon=horizon, status=ScenarioStatus.DRAFT.value,
            created_by=created_by, created_at=self._today(),
            valid_until=(self._clock() + timedelta(days=valid_days)).isoformat(),
            assumptions=(), items=(), decisions=(), overlaps=(),
            benefit_total=0.0, benefit_by_metric={}, config=cfg, notes=notes)
        self.store.save_scenario(rec)
        self.store.audit(self._today(), created_by, "CREATE_SCENARIO",
                         rec.scenario_id, {"name": name, "horizon": list(horizon)})
        return rec

    def _get_scenario(self, scenario_id: str) -> ScenarioRecord:
        rec = self.store.get_scenario(scenario_id)
        if not rec:
            raise NotFoundError(f"情景 {scenario_id} 不存在")
        return rec

    def get_scenario(self, scenario_id: str) -> dict:
        rec = self._get_scenario(scenario_id)
        d = rec.to_dict()
        d["stale_reasons"] = self.scenario_staleness(scenario_id)
        d["stale"] = bool(d["stale_reasons"])
        return d

    def list_scenarios(self) -> list[dict]:
        out = []
        for rec in self.store.list_scenarios():
            d = rec.to_dict()
            d["stale_reasons"] = self.scenario_staleness(rec.scenario_id)
            d["stale"] = bool(d["stale_reasons"])
            out.append(d)
        return out

    def _require_status(self, rec: ScenarioRecord, *statuses: ScenarioStatus) -> None:
        allowed = [s.value for s in statuses]
        if rec.status not in allowed:
            raise ConflictError(
                f"情景 {rec.scenario_id} 当前状态 {rec.status}, 需要 {'/'.join(allowed)}")

    # -- 假设锁定 ---------------------------------------------------------
    def add_assumption(self, scenario_id: str, kind: str, locked_by: str,
                       project_id: str | None = None, value: dict | None = None,
                       note: str = "") -> ScenarioRecord:
        rec = self._get_scenario(scenario_id)
        self._require_status(rec, ScenarioStatus.DRAFT)
        if kind not in ASSUMPTION_KINDS:
            raise DomainError(f"假设类型无效: {kind}")
        value = value or {}
        if kind in (ASSUME_INCLUDE, ASSUME_EXCLUDE, ASSUME_PIN_START, ASSUME_PRIORITY):
            if not project_id or project_id not in rec.config["project_versions"]:
                raise DomainError(f"假设需要指定情景范围内的项目, 收到: {project_id}")
        if kind == ASSUME_PIN_START and "start_year" not in value:
            raise DomainError("PIN_START 假设需要 value.start_year")
        if kind == ASSUME_PRIORITY and "multiplier" not in value:
            raise DomainError("PRIORITY 假设需要 value.multiplier")
        if kind == ASSUME_CAP_OVERRIDE:
            if not all(k in value for k in ("source", "year", "cap")):
                raise DomainError("CAP_OVERRIDE 假设需要 value.source/year/cap")
        assumption = Assumption(
            assumption_id=self.store.next_id("AS"), kind=kind, project_id=project_id,
            value=value, note=note, locked_by=locked_by, locked_at=self._today())
        # 假设变化后需重新求解，清空旧的求解结果
        rec = replace(rec, assumptions=rec.assumptions + (assumption,),
                      items=(), decisions=(), overlaps=(),
                      benefit_total=0.0, benefit_by_metric={})
        self.store.save_scenario(rec)
        self.store.audit(self._today(), locked_by, "LOCK_ASSUMPTION", scenario_id,
                         {"kind": kind, "project_id": project_id, "value": value})
        return rec

    def remove_assumption(self, scenario_id: str, assumption_id: str) -> ScenarioRecord:
        rec = self._get_scenario(scenario_id)
        self._require_status(rec, ScenarioStatus.DRAFT)
        kept = tuple(a for a in rec.assumptions if a.assumption_id != assumption_id)
        if len(kept) == len(rec.assumptions):
            raise NotFoundError(f"假设 {assumption_id} 不存在")
        rec = replace(rec, assumptions=kept, items=(), decisions=(), overlaps=(),
                      benefit_total=0.0, benefit_by_metric={})
        self.store.save_scenario(rec)
        return rec

    # -- 求解 -------------------------------------------------------------
    def _solve_config(self, rec: ScenarioRecord) -> tuple[dict, SolveConfig]:
        pins: dict[str, int] = rec.config.get("project_versions", {})
        projects: dict[str, ProjectVersionRecord] = {}
        for pid, v in pins.items():
            pv = self.store.get_project_version(pid, v)
            if not pv:
                raise ConflictError(f"情景引用的项目 {pid} 版本 {v} 已不存在")
            projects[pid] = pv
        caps = {s.source_id: dict(s.annual_caps) for s in self.store.list_funding_sources()}
        cfg = SolveConfig(
            horizon=rec.horizon, caps=caps,
            metric_values={k: float(v) for k, v in rec.config.get("metric_values", {}).items()},
            risk_weight=float(rec.config.get("risk_weight", 1.0)),
            benefit_weight=float(rec.config.get("benefit_weight", 1.0)),
            overlap_policy=rec.config.get("overlap_policy", "dedupe"))
        return projects, cfg

    def solve_scenario(self, scenario_id: str) -> ScenarioRecord:
        rec = self._get_scenario(scenario_id)
        self._require_status(rec, ScenarioStatus.DRAFT)
        projects, cfg = self._solve_config(rec)
        result = solve(projects, cfg, list(rec.assumptions))
        rec = replace(rec, items=tuple(result.items), decisions=tuple(result.decisions),
                      overlaps=tuple(result.overlaps), benefit_total=result.benefit_total,
                      benefit_by_metric=result.benefit_by_metric)
        self.store.save_scenario(rec)
        self.store.audit(self._today(), rec.created_by, "SOLVE_SCENARIO", scenario_id, {
            "included": [i.project_id for i in result.items],
            "benefit_total": result.benefit_total})
        return rec

    # -- 审批流 -------------------------------------------------------------
    def submit_scenario(self, scenario_id: str, actor: str) -> ScenarioRecord:
        rec = self._get_scenario(scenario_id)
        self._require_status(rec, ScenarioStatus.DRAFT)
        if not rec.decisions:
            raise ConflictError("情景尚未求解, 不能提交审批")
        rec = replace(rec, status=ScenarioStatus.SUBMITTED.value)
        self.store.save_scenario(rec)
        self.store.audit(self._today(), actor, "SUBMIT_SCENARIO", scenario_id, {})
        return rec

    def approve_scenario(self, scenario_id: str, actor: str, note: str = "") -> ScenarioRecord:
        rec = self._get_scenario(scenario_id)
        self._require_status(rec, ScenarioStatus.SUBMITTED)
        rec = replace(rec, status=ScenarioStatus.APPROVED.value, notes=note or rec.notes)
        self.store.save_scenario(rec)
        self.store.audit(self._today(), actor, "APPROVE_SCENARIO", scenario_id, {"note": note})
        return rec

    def reject_scenario(self, scenario_id: str, actor: str, note: str) -> ScenarioRecord:
        rec = self._get_scenario(scenario_id)
        self._require_status(rec, ScenarioStatus.SUBMITTED)
        rec = replace(rec, status=ScenarioStatus.REJECTED.value, notes=note)
        self.store.save_scenario(rec)
        self.store.audit(self._today(), actor, "REJECT_SCENARIO", scenario_id, {"note": note})
        return rec

    # -- 有效期 -------------------------------------------------------------
    def scenario_staleness(self, scenario_id: str) -> list[str]:
        rec = self._get_scenario(scenario_id)
        reasons: list[str] = []
        if self._today() > rec.valid_until:
            reasons.append(f"情景已超过有效期 {rec.valid_until}")
        for item in rec.items:
            versions = self.store.list_versions(item.project_id)
            latest = max((v.version for v in versions), default=item.version)
            if latest > item.version:
                reasons.append(
                    f"项目 {item.project_id} 已有更新版本 v{latest}(情景基于 v{item.version})")
        for src in self.store.list_funding_sources():
            if src.updated_at > rec.created_at:
                reasons.append(f"资金来源 {src.source_id} 的额度在情景创建后已更新")
        return sorted(set(reasons))

    def refresh_scenario(self, scenario_id: str, actor: str,
                         valid_days: int = 90) -> ScenarioRecord:
        """过期情景重新校核：以当前项目版本与资金额度重新求解，产生新修订。"""
        rec = self._get_scenario(scenario_id)
        latest = self.store.latest_versions()
        pins = {pid: latest[pid].version for pid in rec.config.get("project_versions", {})
                if pid in latest}
        cfg = dict(rec.config)
        cfg["project_versions"] = pins
        new = replace(rec, revision=rec.revision + 1, status=ScenarioStatus.DRAFT.value,
                      created_at=self._today(),
                      valid_until=(self._clock() + timedelta(days=valid_days)).isoformat(),
                      config=cfg, items=(), decisions=(), overlaps=(),
                      benefit_total=0.0, benefit_by_metric={})
        projects, solve_cfg = self._solve_config(new)
        result = solve(projects, solve_cfg, list(new.assumptions))
        new = replace(new, items=tuple(result.items), decisions=tuple(result.decisions),
                      overlaps=tuple(result.overlaps), benefit_total=result.benefit_total,
                      benefit_by_metric=result.benefit_by_metric)
        self.store.save_scenario(new)
        self.store.audit(self._today(), actor, "REFRESH_SCENARIO", scenario_id,
                         {"revision": new.revision,
                          "included": [i.project_id for i in result.items]})
        return new

    # -- 发布为基线 ----------------------------------------------------------
    def publish_scenario(self, scenario_id: str, actor: str) -> BaselineRecord:
        rec = self._get_scenario(scenario_id)
        self._require_status(rec, ScenarioStatus.APPROVED)
        stale = self.scenario_staleness(scenario_id)
        if stale:
            raise ConflictError(
                "过期情景不能直接发布, 请先重新校核(refresh): " + "; ".join(stale))
        baseline = BaselineRecord(
            baseline_id=self.store.next_id("BL"), revision=1,
            scenario_id=rec.scenario_id, scenario_revision=rec.revision,
            approved_by=actor, published_at=self._today(),
            items=rec.items,
            funding_snapshot={s.source_id: dict(s.annual_caps)
                              for s in self.store.list_funding_sources()},
            benefit_total=rec.benefit_total)
        self.store.save_baseline(baseline)
        rec = replace(rec, status=ScenarioStatus.PUBLISHED.value)
        self.store.save_scenario(rec)
        self.store.audit(self._today(), actor, "PUBLISH_BASELINE", baseline.baseline_id,
                         {"scenario_id": scenario_id, "items": len(baseline.items)})
        return baseline

    # ------------------------------------------------------------------
    # 基线与调整单
    # ------------------------------------------------------------------
    def _get_baseline(self, baseline_id: str) -> BaselineRecord:
        rec = self.store.get_baseline(baseline_id)
        if not rec:
            raise NotFoundError(f"基线 {baseline_id} 不存在")
        return rec

    def get_baseline(self, baseline_id: str) -> dict:
        return self._get_baseline(baseline_id).to_dict()

    def current_baseline(self) -> dict | None:
        rec = self.store.current_baseline()
        return rec.to_dict() if rec else None

    def create_change_order(self, baseline_id: str, type: str, payload: dict,
                            reason: str, created_by: str) -> ChangeOrderRecord:
        baseline = self._get_baseline(baseline_id)
        if type not in {t.value for t in ChangeOrderType}:
            raise DomainError(f"调整单类型无效: {type}")
        if not reason:
            raise DomainError("调整单必须说明理由")
        in_baseline = set(baseline.item_map())
        pid = payload.get("project_id")
        if type in (ChangeOrderType.RESCHEDULE.value, ChangeOrderType.REPLACE_VERSION.value,
                    ChangeOrderType.REMOVE_ITEM.value, ChangeOrderType.REALLOCATE.value):
            if pid not in in_baseline:
                raise DomainError(f"项目 {pid} 不在基线 {baseline_id} 中")
        if type == ChangeOrderType.ADD_ITEM.value:
            if pid in in_baseline:
                raise DomainError(f"项目 {pid} 已在基线中")
            version = int(payload.get("version", 0))
            if not self.store.get_project_version(pid, version):
                raise NotFoundError(f"项目 {pid} 版本 {version} 不存在")
            if "start_year" not in payload:
                raise DomainError("ADD_ITEM 需要 payload.start_year")
        if type == ChangeOrderType.REPLACE_VERSION.value:
            version = int(payload.get("new_version", 0))
            if not self.store.get_project_version(pid, version):
                raise NotFoundError(f"项目 {pid} 版本 {version} 不存在")
        co = ChangeOrderRecord(
            change_order_id=self.store.next_id("CO"), baseline_id=baseline_id,
            type=type, payload=dict(payload), reason=reason,
            status=ChangeOrderStatus.DRAFT.value, created_by=created_by,
            created_at=self._today())
        self.store.save_change_order(co)
        self.store.audit(self._today(), created_by, "CREATE_CHANGE_ORDER",
                         co.change_order_id, {"baseline_id": baseline_id, "type": type})
        return co

    def _get_change_order(self, co_id: str) -> ChangeOrderRecord:
        co = self.store.get_change_order(co_id)
        if not co:
            raise NotFoundError(f"调整单 {co_id} 不存在")
        return co

    def _transition_co(self, co_id: str, actor: str, action: str,
                       from_status: ChangeOrderStatus, to_status: ChangeOrderStatus,
                       note: str = "") -> ChangeOrderRecord:
        co = self._get_change_order(co_id)
        if co.status != from_status.value:
            raise ConflictError(
                f"调整单 {co_id} 当前状态 {co.status}, 需要 {from_status.value}")
        co = replace(co, status=to_status.value, decided_by=actor,
                     decided_at=self._today(), decision_note=note)
        self.store.save_change_order(co)
        self.store.audit(self._today(), actor, action, co_id, {"note": note})
        return co

    def submit_change_order(self, co_id: str, actor: str) -> ChangeOrderRecord:
        co = self._get_change_order(co_id)
        if co.status != ChangeOrderStatus.DRAFT.value:
            raise ConflictError(f"调整单 {co_id} 当前状态 {co.status}, 需要 DRAFT")
        co = replace(co, status=ChangeOrderStatus.SUBMITTED.value)
        self.store.save_change_order(co)
        self.store.audit(self._today(), actor, "SUBMIT_CHANGE_ORDER", co_id, {})
        return co

    def approve_change_order(self, co_id: str, actor: str, note: str = "") -> ChangeOrderRecord:
        return self._transition_co(co_id, actor, "APPROVE_CHANGE_ORDER",
                                   ChangeOrderStatus.SUBMITTED,
                                   ChangeOrderStatus.APPROVED, note)

    def reject_change_order(self, co_id: str, actor: str, note: str) -> ChangeOrderRecord:
        return self._transition_co(co_id, actor, "REJECT_CHANGE_ORDER",
                                   ChangeOrderStatus.SUBMITTED,
                                   ChangeOrderStatus.REJECTED, note)

    def _allocate(self, project: ProjectVersionRecord, start_year: int,
                  remaining: dict[str, dict[int, int]],
                  strict: bool = True) -> dict[int, dict[str, int]]:
        """按项目可用资金来源顺序为某开工年分配年度资金。

        strict=False 时尽量分配、允许缺口, 由 validate_items 统一汇总违规。
        """
        plan: dict[int, dict[str, int]] = {}
        for i, cost in enumerate(project.cost_curve):
            year = start_year + i
            need = cost
            alloc: dict[str, int] = {}
            for src in project.funding_sources:
                avail = remaining.get(src, {}).get(year, 0)
                take = min(avail, need)
                if take > 0:
                    alloc[src] = take
                    need -= take
                if need == 0:
                    break
            if need > 0 and strict:
                raise DomainError(
                    f"项目 {project.project_id} {year} 年资金不足, 缺口 {need:,} 元")
            plan[year] = alloc
        return plan

    def _apply_change_order(self, co: ChangeOrderRecord,
                            baseline: BaselineRecord) -> list[ScenarioItem]:
        items = {i.project_id: i for i in baseline.items}
        projects: dict[str, ProjectVersionRecord] = {}
        for i in baseline.items:
            pv = self.store.get_project_version(i.project_id, i.version)
            if pv:
                projects[i.project_id] = pv
        caps = baseline.funding_snapshot
        p = co.payload
        pid = p.get("project_id")

        def remaining_without(*exclude: str) -> dict[str, dict[int, int]]:
            rem = {s: dict(years) for s, years in caps.items()}
            for ipid, item in items.items():
                if ipid in exclude:
                    continue
                for year, allocs in item.funding_plan.items():
                    for src, amount in allocs.items():
                        rem.setdefault(src, {}).setdefault(year, 0)
                        rem[src][year] -= amount
            return rem

        if co.type == ChangeOrderType.RESCHEDULE.value:
            new_start = int(p["new_start_year"])
            item = items[pid]
            project = projects[pid]
            plan = self._allocate(project, new_start, remaining_without(pid), strict=False)
            items[pid] = replace(item, start_year=new_start, funding_plan=plan)
        elif co.type == ChangeOrderType.REPLACE_VERSION.value:
            new_version = int(p["new_version"])
            project = self.store.get_project_version(pid, new_version)
            projects[pid] = project
            item = items[pid]
            plan = self._allocate(project, item.start_year, remaining_without(pid), strict=False)
            items[pid] = replace(item, version=new_version, funding_plan=plan)
        elif co.type == ChangeOrderType.ADD_ITEM.value:
            version = int(p["version"])
            project = self.store.get_project_version(pid, version)
            projects[pid] = project
            start = int(p["start_year"])
            plan = self._allocate(project, start, remaining_without(), strict=False)
            items[pid] = ScenarioItem(project_id=pid, version=version, start_year=start,
                                      funding_plan=plan, score=0.0, locked=True)
        elif co.type == ChangeOrderType.REMOVE_ITEM.value:
            cascade = bool(p.get("cascade", False))
            graph = dependency_graph(projects)
            affected = propagate_removal(graph, list(items.values()), pid)
            if affected and not cascade:
                raise DomainError(
                    "移除将使以下依赖项目失效(使用 cascade=true 一并移除): "
                    + ", ".join(a["project_id"] for a in affected))
            for a in affected:
                items.pop(a["project_id"], None)
            items.pop(pid, None)
        elif co.type == ChangeOrderType.REALLOCATE.value:
            item = items[pid]
            year = int(p["year"])
            plan = {y: dict(a) for y, a in item.funding_plan.items()}
            for move in p["moves"]:
                frm, to, amount = move["from"], move["to"], int(move["amount"])
                if plan.get(year, {}).get(frm, 0) < amount:
                    raise DomainError(
                        f"项目 {pid} {year} 年来源 {frm} 可用余额不足, 无法调出 {amount:,} 元")
                plan.setdefault(year, {}).setdefault(frm, 0)
                plan[year][frm] -= amount
                plan[year][to] = plan[year].get(to, 0) + amount
            items[pid] = replace(item, funding_plan=plan)
        else:  # pragma: no cover - 类型在创建时已校验
            raise DomainError(f"不支持的调整单类型: {co.type}")
        return list(items.values()), projects

    def apply_change_order(self, co_id: str, actor: str) -> BaselineRecord:
        """应用调整单：一致性校验通过后产生新的基线修订。"""
        co = self._get_change_order(co_id)
        if co.status != ChangeOrderStatus.APPROVED.value:
            raise ConflictError(f"调整单 {co_id} 当前状态 {co.status}, 需要 APPROVED")
        baseline = self._get_baseline(co.baseline_id)
        new_items, projects = self._apply_change_order(co, baseline)
        violations = validate_items(new_items, projects, baseline.funding_snapshot)
        if violations:
            co = replace(co, validation=tuple(violations))
            self.store.save_change_order(co)
            raise DomainError("调整单一致性校验未通过: " + "; ".join(violations))
        new_baseline = replace(
            baseline, revision=baseline.revision + 1, items=tuple(new_items),
            change_orders_applied=baseline.change_orders_applied + (co.change_order_id,))
        self.store.save_baseline(new_baseline)
        co = replace(co, status=ChangeOrderStatus.APPLIED.value, decided_by=actor,
                     decided_at=self._today(), validation=("校验通过",))
        self.store.save_change_order(co)
        self.store.audit(self._today(), actor, "APPLY_CHANGE_ORDER", co_id,
                         {"baseline_id": baseline.baseline_id,
                          "new_revision": new_baseline.revision})
        return new_baseline

    def list_change_orders(self, baseline_id: str) -> list[dict]:
        return [co.to_dict() for co in self.store.list_change_orders(baseline_id)]

    # ------------------------------------------------------------------
    # 实际回填与偏差预警
    # ------------------------------------------------------------------
    def backfill_actual(self, baseline_id: str, project_id: str, period: str,
                        actual_spend: int, mileage_completed_m: float,
                        source: str = "manual", note: str = "") -> ActualEntry:
        baseline = self._get_baseline(baseline_id)
        if project_id not in baseline.item_map():
            raise DomainError(f"项目 {project_id} 不在基线 {baseline_id} 中")
        _parse_period(period)
        if actual_spend < 0 or mileage_completed_m < 0:
            raise DomainError("实际支出与里程不能为负")
        entry = ActualEntry(
            entry_id=self.store.next_id("EN"), baseline_id=baseline_id,
            project_id=project_id, period=period, actual_spend=int(actual_spend),
            mileage_completed_m=float(mileage_completed_m), source=source, note=note,
            recorded_at=self._today())
        self.store.save_actual(entry)
        self.store.audit(self._today(), source, "BACKFILL_ACTUAL", project_id,
                         {"baseline_id": baseline_id, "period": period,
                          "actual_spend": actual_spend,
                          "mileage_completed_m": mileage_completed_m})
        self._recompute_alerts(baseline, project_id, period)
        return entry

    def list_actuals(self, baseline_id: str, project_id: str | None = None) -> list[dict]:
        return [a.to_dict() for a in self.store.list_actuals(baseline_id, project_id)]

    def _planned_cumulative(self, item: ScenarioItem, project: ProjectVersionRecord,
                            period: tuple[int, int]) -> tuple[float, float]:
        """截至某期间的计划累计(成本, 里程)。里程按成本进度折算。"""
        year, quarter = period
        planned_cost = 0.0
        for y, allocs in item.funding_plan.items():
            amount = sum(allocs.values())
            if y < year:
                planned_cost += amount
            elif y == year:
                planned_cost += amount * quarter / 4
        total = max(project.total_cost, 1)
        planned_mileage = project.scope_length_m * planned_cost / total
        return planned_cost, planned_mileage

    def _recompute_alerts(self, baseline: BaselineRecord, project_id: str,
                          period: str) -> None:
        item = baseline.item_map()[project_id]
        project = self.store.get_project_version(project_id, item.version)
        yq = _parse_period(period)
        actuals = [a for a in self.store.list_actuals(baseline.baseline_id, project_id)
                   if _parse_period(a.period) <= yq]
        cum_spend = sum(a.actual_spend for a in actuals)
        latest_mileage = max((a.mileage_completed_m for a in actuals), default=0.0)
        planned_cost, planned_mileage = self._planned_cumulative(item, project, yq)
        alerts: list[AlertRecord] = []

        def make(alert_type: str, planned: float, actual: float, dev_pct: float,
                 threshold: float, message: str) -> AlertRecord:
            severity = "CRITICAL" if abs(dev_pct) >= 2 * threshold else "WARNING"
            return AlertRecord(
                alert_id=self.store.next_id("AL"), baseline_id=baseline.baseline_id,
                project_id=project_id, period=period, alert_type=alert_type,
                severity=severity, planned=round(planned, 2), actual=round(actual, 2),
                deviation_pct=round(dev_pct, 2), message=message,
                created_at=self._today())

        if planned_cost > 0:
            dev = (cum_spend - planned_cost) / planned_cost * 100
            if abs(dev) >= COST_DEVIATION_THRESHOLD_PCT:
                direction = "超支" if dev > 0 else "低于计划"
                alerts.append(make(
                    "COST_DEVIATION", planned_cost, cum_spend, dev,
                    COST_DEVIATION_THRESHOLD_PCT,
                    f"项目 {project_id} 截至 {period} 累计支出 {cum_spend:,.0f} 元, "
                    f"计划 {planned_cost:,.0f} 元, {direction} {abs(dev):.1f}%"))
        if planned_mileage > 0:
            lag = (planned_mileage - latest_mileage) / planned_mileage * 100
            if lag >= MILEAGE_LAG_THRESHOLD_PCT:
                alerts.append(make(
                    "MILEAGE_LAG", planned_mileage, latest_mileage, lag,
                    MILEAGE_LAG_THRESHOLD_PCT,
                    f"项目 {project_id} 截至 {period} 完成里程 {latest_mileage:,.0f} 米, "
                    f"计划 {planned_mileage:,.0f} 米, 滞后 {lag:.1f}%"))
        self.store.replace_alerts(baseline.baseline_id, project_id, period, alerts)

    def list_alerts(self, baseline_id: str) -> list[dict]:
        return [a.to_dict() for a in self.store.list_alerts(baseline_id)]

    def deviation_report(self, baseline_id: str) -> dict:
        baseline = self._get_baseline(baseline_id)
        rows = []
        for item in sorted(baseline.items, key=lambda i: i.project_id):
            project = self.store.get_project_version(item.project_id, item.version)
            actuals = self.store.list_actuals(baseline_id, item.project_id)
            if actuals:
                latest_period = max(actuals, key=lambda a: _parse_period(a.period)).period
                yq = _parse_period(latest_period)
                cum_spend = sum(a.actual_spend for a in actuals
                                if _parse_period(a.period) <= yq)
                mileage = max(a.mileage_completed_m for a in actuals
                              if _parse_period(a.period) <= yq)
            else:
                latest_period, cum_spend, mileage = None, 0, 0.0
                yq = (item.start_year, 0)
            planned_cost, planned_mileage = self._planned_cumulative(item, project, yq)
            rows.append({
                "project_id": item.project_id, "start_year": item.start_year,
                "as_of": latest_period,
                "planned_cost_to_date": round(planned_cost, 2),
                "actual_cost_to_date": cum_spend,
                "cost_deviation_pct": (round((cum_spend - planned_cost) / planned_cost * 100, 2)
                                       if planned_cost > 0 else None),
                "planned_mileage_m": round(planned_mileage, 1),
                "actual_mileage_m": mileage,
                "mileage_lag_pct": (round((planned_mileage - mileage) / planned_mileage * 100, 2)
                                    if planned_mileage > 0 else None),
            })
        return {"baseline_id": baseline_id, "revision": baseline.revision, "items": rows}

    # ------------------------------------------------------------------
    # 解释接口(委托 explain 模块)
    # ------------------------------------------------------------------
    def explain_selection(self, scenario_id: str) -> dict:
        return explain.selection(self._get_scenario(scenario_id))

    def explain_dependencies(self, scenario_id: str, project_id: str) -> dict:
        rec = self._get_scenario(scenario_id)
        projects, _ = self._solve_config(rec)
        return explain.dependencies(rec, projects, project_id)

    def explain_funding(self, scenario_id: str) -> dict:
        rec = self._get_scenario(scenario_id)
        caps = {s.source_id: dict(s.annual_caps) for s in self.store.list_funding_sources()}
        for a in rec.assumptions:
            if a.kind == ASSUME_CAP_OVERRIDE:
                caps.setdefault(a.value["source"], {})[int(a.value["year"])] = int(a.value["cap"])
        return explain.funding(rec, caps)

    def compare_scenarios(self, a_id: str, b_id: str) -> dict:
        return explain.diff(self._get_scenario(a_id), self._get_scenario(b_id))

    def audit_trail(self, entity: str | None = None) -> list[dict]:
        return self.store.audit_trail(entity)
