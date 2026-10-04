"""治理状态的轻量持久化。

持久化内容：情景假设与生命周期状态、审批基线（含调整记录、滚动实际值）、
预警记录。项目目录、预算与资金池视为外部登记输入，由服务启动时重新装载；
候选方案不入库，在需要时按已锁定假设重算（保证可复现）。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .governance import Baseline, Scenario, ScenarioAssumptions
from .engine import PlanWeights
from .service import PortfolioService

STATE_VERSION = 1


def save_state(svc: PortfolioService, path: str | Path) -> None:
    payload = {
        "state_version": STATE_VERSION,
        "scenarios": [_scenario_to_dict(s) for s in svc.scenarios.values()],
        "baselines": [asdict(b) for b in svc.baselines.values()],
        "alerts": [asdict(a) for a in svc.alerts],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def load_state(svc: PortfolioService, path: str | Path) -> bool:
    """把磁盘状态装入服务；文件不存在时返回 False。"""
    path = Path(path)
    if not path.exists():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("state_version") != STATE_VERSION:
        raise ValueError(f"状态文件版本不受支持：{payload.get('state_version')}")

    svc.scenarios = {}
    for raw in payload["scenarios"]:
        w = raw["assumptions"].pop("weights")
        assumptions = ScenarioAssumptions(
            weights=PlanWeights(
                risk_value_per_point=w["risk_value_per_point"],
                enablement_bonus_per_dependent=w["enablement_bonus_per_dependent"],
                manual_boost={k: float(v) for k, v in w["manual_boost"].items()},
            ),
            **raw["assumptions"],
        )
        svc.scenarios[raw["scenario_id"]] = Scenario(
            scenario_id=raw["scenario_id"],
            assumptions=assumptions,
            status=raw["status"],
            plan=None,
            baseline_id=raw.get("baseline_id"),
            parent_scenario_id=raw.get("parent_scenario_id"),
            revision_of_baseline=raw.get("revision_of_baseline"),
        )

    svc.baselines = {}
    int_keyed = ("budgets", "budget_committed")
    for raw in payload["baselines"]:
        for field_name in int_keyed:
            raw[field_name] = {int(y): v for y, v in raw[field_name].items()}
        svc.baselines[raw["baseline_id"]] = Baseline(**raw)

    from .governance import VarianceAlert
    svc.alerts = [VarianceAlert(**a) for a in payload.get("alerts", [])]
    return True


def _scenario_to_dict(scenario: Scenario) -> dict:
    a = scenario.assumptions
    return {
        "scenario_id": scenario.scenario_id,
        "status": scenario.status,
        "baseline_id": scenario.baseline_id,
        "parent_scenario_id": scenario.parent_scenario_id,
        "revision_of_baseline": scenario.revision_of_baseline,
        "assumptions": {
            "name": a.name,
            "notes": a.notes,
            "created_on": a.created_on,
            "expires_on": a.expires_on,
            "forced_in": dict(a.forced_in),
            "excluded": list(a.excluded),
            "weights": asdict(a.weights),
        },
    }
