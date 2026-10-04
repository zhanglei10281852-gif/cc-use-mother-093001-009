"""FastAPI 接口层：将领域错误映射为 HTTP 状态码，薄封装服务层。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .domain import ConflictError, DomainError, InfeasibleError, NotFoundError
from .service import PortfolioService
from .store import Store


class ProjectVersionIn(BaseModel):
    project_id: str
    version: int
    name: str
    segments: dict[str, float]
    cost_curve: list[int]
    earliest_start: int
    risk_evidence: list[dict] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    funding_sources: list[str]
    benefits: list[dict] = Field(default_factory=list)
    exclusive_with: list[list] = Field(default_factory=list)
    submitted_by: str = ""


class FundingSourceIn(BaseModel):
    source_id: str
    name: str
    annual_caps: dict[int, int]


class ScenarioIn(BaseModel):
    name: str
    horizon: tuple[int, int]
    created_by: str
    project_versions: dict[str, int] | None = None
    config: dict | None = None
    valid_days: int = 90
    notes: str = ""


class AssumptionIn(BaseModel):
    kind: str
    locked_by: str
    project_id: str | None = None
    value: dict = Field(default_factory=dict)
    note: str = ""


class ActorIn(BaseModel):
    actor: str
    note: str = ""


class ChangeOrderIn(BaseModel):
    baseline_id: str
    type: str
    payload: dict = Field(default_factory=dict)
    reason: str
    created_by: str


class ActualIn(BaseModel):
    project_id: str
    period: str
    actual_spend: int
    mileage_completed_m: float
    source: str = "manual"
    note: str = ""


def create_app(db_path: str = ":memory:", service: PortfolioService | None = None) -> FastAPI:
    svc = service or PortfolioService(Store(db_path))
    app = FastAPI(title="管网改造投资组合治理服务", version="1.0.0")
    app.state.service = svc

    @app.exception_handler(NotFoundError)
    async def _404(_, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def _409(_, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InfeasibleError)
    async def _422_infeasible(_, exc: InfeasibleError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(DomainError)
    async def _422(_, exc: DomainError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    # -- 项目与资金 ---------------------------------------------------------
    @app.post("/projects/versions", status_code=201)
    def submit_project(body: ProjectVersionIn):
        return svc.submit_project_version(body.model_dump()).to_dict()

    @app.get("/projects")
    def list_projects():
        return svc.list_projects()

    @app.get("/projects/{project_id}/versions")
    def project_versions(project_id: str):
        return svc.get_project_versions(project_id)

    @app.post("/funding-sources", status_code=201)
    def upsert_funding(body: FundingSourceIn):
        return svc.upsert_funding_source(body.model_dump()).to_dict()

    @app.get("/funding-sources")
    def list_funding():
        return svc.list_funding_sources()

    # -- 情景 ---------------------------------------------------------------
    @app.post("/scenarios", status_code=201)
    def create_scenario(body: ScenarioIn):
        return svc.create_scenario(**body.model_dump()).to_dict()

    @app.get("/scenarios")
    def list_scenarios():
        return svc.list_scenarios()

    @app.get("/scenarios/{scenario_id}")
    def get_scenario(scenario_id: str):
        return svc.get_scenario(scenario_id)

    @app.post("/scenarios/{scenario_id}/solve")
    def solve_scenario(scenario_id: str):
        return svc.solve_scenario(scenario_id).to_dict()

    @app.post("/scenarios/{scenario_id}/assumptions", status_code=201)
    def add_assumption(scenario_id: str, body: AssumptionIn):
        return svc.add_assumption(scenario_id, **body.model_dump()).to_dict()

    @app.delete("/scenarios/{scenario_id}/assumptions/{assumption_id}")
    def remove_assumption(scenario_id: str, assumption_id: str):
        return svc.remove_assumption(scenario_id, assumption_id).to_dict()

    @app.post("/scenarios/{scenario_id}/submit")
    def submit_scenario(scenario_id: str, body: ActorIn):
        return svc.submit_scenario(scenario_id, body.actor).to_dict()

    @app.post("/scenarios/{scenario_id}/approve")
    def approve_scenario(scenario_id: str, body: ActorIn):
        return svc.approve_scenario(scenario_id, body.actor, body.note).to_dict()

    @app.post("/scenarios/{scenario_id}/reject")
    def reject_scenario(scenario_id: str, body: ActorIn):
        return svc.reject_scenario(scenario_id, body.actor, body.note).to_dict()

    @app.post("/scenarios/{scenario_id}/refresh")
    def refresh_scenario(scenario_id: str, body: ActorIn):
        return svc.refresh_scenario(scenario_id, body.actor).to_dict()

    @app.post("/scenarios/{scenario_id}/publish", status_code=201)
    def publish_scenario(scenario_id: str, body: ActorIn):
        return svc.publish_scenario(scenario_id, body.actor).to_dict()

    # -- 解释 ---------------------------------------------------------------
    @app.get("/scenarios/{scenario_id}/explain/selection")
    def explain_selection(scenario_id: str):
        return svc.explain_selection(scenario_id)

    @app.get("/scenarios/{scenario_id}/explain/dependencies/{project_id}")
    def explain_dependencies(scenario_id: str, project_id: str):
        return svc.explain_dependencies(scenario_id, project_id)

    @app.get("/scenarios/{scenario_id}/explain/funding")
    def explain_funding(scenario_id: str):
        return svc.explain_funding(scenario_id)

    @app.get("/compare")
    def compare(a: str, b: str):
        return svc.compare_scenarios(a, b)

    # -- 基线与调整单 ----------------------------------------------------------
    @app.get("/baselines/current")
    def current_baseline():
        return svc.current_baseline() or {}

    @app.get("/baselines/{baseline_id}")
    def get_baseline(baseline_id: str):
        return svc.get_baseline(baseline_id)

    @app.get("/baselines/{baseline_id}/deviation")
    def deviation(baseline_id: str):
        return svc.deviation_report(baseline_id)

    @app.get("/baselines/{baseline_id}/alerts")
    def alerts(baseline_id: str):
        return svc.list_alerts(baseline_id)

    @app.get("/baselines/{baseline_id}/actuals")
    def actuals(baseline_id: str, project_id: str | None = None):
        return svc.list_actuals(baseline_id, project_id)

    @app.post("/baselines/{baseline_id}/actuals", status_code=201)
    def backfill(baseline_id: str, body: ActualIn):
        return svc.backfill_actual(baseline_id, **body.model_dump()).to_dict()

    @app.post("/change-orders", status_code=201)
    def create_change_order(body: ChangeOrderIn):
        return svc.create_change_order(**body.model_dump()).to_dict()

    @app.get("/baselines/{baseline_id}/change-orders")
    def list_change_orders(baseline_id: str):
        return svc.list_change_orders(baseline_id)

    @app.post("/change-orders/{co_id}/submit")
    def submit_co(co_id: str, body: ActorIn):
        return svc.submit_change_order(co_id, body.actor).to_dict()

    @app.post("/change-orders/{co_id}/approve")
    def approve_co(co_id: str, body: ActorIn):
        return svc.approve_change_order(co_id, body.actor, body.note).to_dict()

    @app.post("/change-orders/{co_id}/reject")
    def reject_co(co_id: str, body: ActorIn):
        return svc.reject_change_order(co_id, body.actor, body.note).to_dict()

    @app.post("/change-orders/{co_id}/apply")
    def apply_co(co_id: str, body: ActorIn):
        return svc.apply_change_order(co_id, body.actor).to_dict()

    # -- 审计 ---------------------------------------------------------------
    @app.get("/audit")
    def audit(entity: str | None = None):
        return svc.audit_trail(entity)

    return app


app = create_app()
