"""SQLite 持久化层：所有实体以 JSON 载荷存储，并记录审计日志。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .domain import (
    ActualEntry, AlertRecord, BaselineRecord, ChangeOrderRecord, FundingSource,
    ProjectVersionRecord, ScenarioRecord,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS project_versions (
  project_id TEXT NOT NULL, version INTEGER NOT NULL,
  payload TEXT NOT NULL, submitted_at TEXT NOT NULL,
  PRIMARY KEY (project_id, version));
CREATE TABLE IF NOT EXISTS funding_sources (
  source_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scenarios (
  scenario_id TEXT NOT NULL, revision INTEGER NOT NULL,
  status TEXT NOT NULL, payload TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (scenario_id, revision));
CREATE TABLE IF NOT EXISTS baselines (
  baseline_id TEXT NOT NULL, revision INTEGER NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (baseline_id, revision));
CREATE TABLE IF NOT EXISTS change_orders (
  change_order_id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL,
  status TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actuals (
  entry_id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL,
  project_id TEXT NOT NULL, period TEXT NOT NULL,
  superseded INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
  alert_id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL,
  project_id TEXT NOT NULL, period TEXT NOT NULL,
  alert_type TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
  actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL, detail TEXT NOT NULL);
"""


class Store:
    """薄持久化层：每个方法一次事务，载荷为领域对象的 JSON。"""

    def __init__(self, path: str | Path = ":memory:"):
        # check_same_thread=False: FastAPI 在线程池中处理请求, 服务层自身保证单写者
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- 编号 ---------------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        cur = self._conn.execute(
            "INSERT INTO counters(name, value) VALUES(?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1 RETURNING value",
            (prefix,))
        value = cur.fetchone()["value"]
        self._conn.commit()
        return f"{prefix}-{value:04d}"

    # -- 审计 ---------------------------------------------------------------
    def audit(self, at: str, actor: str, action: str, entity: str, detail: dict) -> None:
        self._conn.execute(
            "INSERT INTO audit_log(at, actor, action, entity, detail) VALUES(?,?,?,?,?)",
            (at, actor, action, entity, json.dumps(detail, ensure_ascii=False, sort_keys=True)))
        self._conn.commit()

    def audit_trail(self, entity: str | None = None) -> list[dict]:
        sql = "SELECT * FROM audit_log"
        args: tuple = ()
        if entity:
            sql += " WHERE entity = ?"
            args = (entity,)
        sql += " ORDER BY seq"
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    # -- 项目版本 -------------------------------------------------------------
    def save_project_version(self, rec: ProjectVersionRecord) -> None:
        self._conn.execute(
            "INSERT INTO project_versions(project_id, version, payload, submitted_at) "
            "VALUES(?,?,?,?)",
            (rec.project_id, rec.version, json.dumps(rec.to_dict(), ensure_ascii=False),
             rec.submitted_at))
        self._conn.commit()

    def get_project_version(self, project_id: str, version: int) -> ProjectVersionRecord | None:
        row = self._conn.execute(
            "SELECT payload FROM project_versions WHERE project_id=? AND version=?",
            (project_id, version)).fetchone()
        return ProjectVersionRecord.from_dict(json.loads(row["payload"])) if row else None

    def latest_versions(self) -> dict[str, ProjectVersionRecord]:
        """每个项目的最新版本。"""
        rows = self._conn.execute(
            "SELECT project_id, MAX(version) AS v FROM project_versions GROUP BY project_id"
        ).fetchall()
        out: dict[str, ProjectVersionRecord] = {}
        for r in rows:
            rec = self.get_project_version(r["project_id"], r["v"])
            if rec:
                out[r["project_id"]] = rec
        return out

    def list_versions(self, project_id: str) -> list[ProjectVersionRecord]:
        rows = self._conn.execute(
            "SELECT payload FROM project_versions WHERE project_id=? ORDER BY version",
            (project_id,)).fetchall()
        return [ProjectVersionRecord.from_dict(json.loads(r["payload"])) for r in rows]

    # -- 资金来源 -------------------------------------------------------------
    def save_funding_source(self, src: FundingSource) -> None:
        self._conn.execute(
            "INSERT INTO funding_sources(source_id, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (src.source_id, json.dumps(src.to_dict(), ensure_ascii=False), src.updated_at))
        self._conn.commit()

    def get_funding_source(self, source_id: str) -> FundingSource | None:
        row = self._conn.execute(
            "SELECT payload FROM funding_sources WHERE source_id=?", (source_id,)).fetchone()
        return FundingSource.from_dict(json.loads(row["payload"])) if row else None

    def list_funding_sources(self) -> list[FundingSource]:
        rows = self._conn.execute("SELECT payload FROM funding_sources ORDER BY source_id").fetchall()
        return [FundingSource.from_dict(json.loads(r["payload"])) for r in rows]

    # -- 情景 ---------------------------------------------------------------
    def save_scenario(self, rec: ScenarioRecord) -> None:
        self._conn.execute(
            "INSERT INTO scenarios(scenario_id, revision, status, payload, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(scenario_id, revision) DO UPDATE SET "
            "status=excluded.status, payload=excluded.payload, updated_at=excluded.updated_at",
            (rec.scenario_id, rec.revision, rec.status,
             json.dumps(rec.to_dict(), ensure_ascii=False), rec.created_at, rec.created_at))
        self._conn.commit()

    def get_scenario(self, scenario_id: str, revision: int | None = None) -> ScenarioRecord | None:
        if revision is None:
            row = self._conn.execute(
                "SELECT payload FROM scenarios WHERE scenario_id=? ORDER BY revision DESC LIMIT 1",
                (scenario_id,)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT payload FROM scenarios WHERE scenario_id=? AND revision=?",
                (scenario_id, revision)).fetchone()
        return ScenarioRecord.from_dict(json.loads(row["payload"])) if row else None

    def list_scenarios(self) -> list[ScenarioRecord]:
        rows = self._conn.execute(
            "SELECT s.payload FROM scenarios s JOIN ("
            "  SELECT scenario_id, MAX(revision) AS r FROM scenarios GROUP BY scenario_id"
            ") t ON s.scenario_id=t.scenario_id AND s.revision=t.r ORDER BY s.scenario_id"
        ).fetchall()
        return [ScenarioRecord.from_dict(json.loads(r["payload"])) for r in rows]

    # -- 基线 ---------------------------------------------------------------
    def save_baseline(self, rec: BaselineRecord) -> None:
        self._conn.execute(
            "INSERT INTO baselines(baseline_id, revision, payload, created_at) VALUES(?,?,?,?)",
            (rec.baseline_id, rec.revision, json.dumps(rec.to_dict(), ensure_ascii=False),
             rec.published_at))
        self._conn.commit()

    def get_baseline(self, baseline_id: str, revision: int | None = None) -> BaselineRecord | None:
        if revision is None:
            row = self._conn.execute(
                "SELECT payload FROM baselines WHERE baseline_id=? ORDER BY revision DESC LIMIT 1",
                (baseline_id,)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT payload FROM baselines WHERE baseline_id=? AND revision=?",
                (baseline_id, revision)).fetchone()
        return BaselineRecord.from_dict(json.loads(row["payload"])) if row else None

    def current_baseline(self) -> BaselineRecord | None:
        row = self._conn.execute(
            "SELECT payload FROM baselines ORDER BY created_at DESC, baseline_id DESC, "
            "revision DESC LIMIT 1").fetchone()
        return BaselineRecord.from_dict(json.loads(row["payload"])) if row else None

    # -- 调整单 ---------------------------------------------------------------
    def save_change_order(self, rec: ChangeOrderRecord) -> None:
        self._conn.execute(
            "INSERT INTO change_orders(change_order_id, baseline_id, status, payload, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(change_order_id) DO UPDATE SET "
            "status=excluded.status, payload=excluded.payload, updated_at=excluded.updated_at",
            (rec.change_order_id, rec.baseline_id, rec.status,
             json.dumps(rec.to_dict(), ensure_ascii=False), rec.created_at))
        self._conn.commit()

    def get_change_order(self, change_order_id: str) -> ChangeOrderRecord | None:
        row = self._conn.execute(
            "SELECT payload FROM change_orders WHERE change_order_id=?",
            (change_order_id,)).fetchone()
        return ChangeOrderRecord.from_dict(json.loads(row["payload"])) if row else None

    def list_change_orders(self, baseline_id: str) -> list[ChangeOrderRecord]:
        rows = self._conn.execute(
            "SELECT payload FROM change_orders WHERE baseline_id=? ORDER BY change_order_id",
            (baseline_id,)).fetchall()
        return [ChangeOrderRecord.from_dict(json.loads(r["payload"])) for r in rows]

    # -- 实际回填 ---------------------------------------------------------------
    def save_actual(self, rec: ActualEntry) -> None:
        self._conn.execute(
            "UPDATE actuals SET superseded=1 WHERE baseline_id=? AND project_id=? AND period=?",
            (rec.baseline_id, rec.project_id, rec.period))
        payload = rec.to_dict()
        self._conn.execute(
            "INSERT INTO actuals(entry_id, baseline_id, project_id, period, superseded, payload) "
            "VALUES(?,?,?,?,0,?)",
            (rec.entry_id, rec.baseline_id, rec.project_id, rec.period,
             json.dumps(payload, ensure_ascii=False)))
        self._conn.commit()

    def list_actuals(self, baseline_id: str, project_id: str | None = None,
                     active_only: bool = True) -> list[ActualEntry]:
        sql = "SELECT payload FROM actuals WHERE baseline_id=?"
        args: list = [baseline_id]
        if project_id:
            sql += " AND project_id=?"
            args.append(project_id)
        if active_only:
            sql += " AND superseded=0"
        sql += " ORDER BY project_id, period, entry_id"
        rows = self._conn.execute(sql, args).fetchall()
        return [ActualEntry.from_dict(json.loads(r["payload"])) for r in rows]

    # -- 预警 ---------------------------------------------------------------
    def replace_alerts(self, baseline_id: str, project_id: str, period: str,
                       alerts: list[AlertRecord]) -> None:
        self._conn.execute(
            "DELETE FROM alerts WHERE baseline_id=? AND project_id=? AND period=?",
            (baseline_id, project_id, period))
        for a in alerts:
            self._conn.execute(
                "INSERT INTO alerts(alert_id, baseline_id, project_id, period, alert_type, payload) "
                "VALUES(?,?,?,?,?,?)",
                (a.alert_id, a.baseline_id, a.project_id, a.period, a.alert_type,
                 json.dumps(a.to_dict(), ensure_ascii=False)))
        self._conn.commit()

    def list_alerts(self, baseline_id: str) -> list[AlertRecord]:
        rows = self._conn.execute(
            "SELECT payload FROM alerts WHERE baseline_id=? ORDER BY alert_id",
            (baseline_id,)).fetchall()
        return [AlertRecord.from_dict(json.loads(r["payload"])) for r in rows]
