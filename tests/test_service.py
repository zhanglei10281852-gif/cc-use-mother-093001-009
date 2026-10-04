import unittest
from datetime import date, timedelta

from helpers import make_service
from capital_portfolio.domain import ConflictError, DomainError, NotFoundError

M = 1_000_000


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()

    def _solved_scenario(self, name="方案A", **kwargs):
        scn = self.svc.create_scenario(name, (2027, 2029), "评审员", **kwargs)
        self.svc.solve_scenario(scn.scenario_id)
        return scn.scenario_id

    def _published_baseline(self):
        sid = self._solved_scenario()
        self.svc.submit_scenario(sid, "评审员")
        self.svc.approve_scenario(sid, "处长")
        return self.svc.publish_scenario(sid, "处长")


class ProjectSubmissionTests(ServiceTestCase):
    def test_new_version_must_increase(self):
        with self.assertRaises(DomainError):
            self.svc.submit_project_version({
                "project_id": "P-001", "version": 1, "name": "重复版本",
                "segments": {"SEG-X": 100}, "cost_curve": [M], "earliest_start": 2027,
                "funding_sources": ["F-CITY"]})

    def test_unknown_funding_source_rejected(self):
        with self.assertRaises(DomainError):
            self.svc.submit_project_version({
                "project_id": "P-100", "version": 1, "name": "x",
                "segments": {"SEG-X": 100}, "cost_curve": [M], "earliest_start": 2027,
                "funding_sources": ["F-GHOST"]})

    def test_dependency_cycle_rejected_at_submission(self):
        self.svc.submit_project_version({
            "project_id": "P-201", "version": 1, "name": "甲",
            "segments": {"SEG-A": 100}, "cost_curve": [M], "earliest_start": 2027,
            "funding_sources": ["F-CITY"]})
        self.svc.submit_project_version({
            "project_id": "P-202", "version": 1, "name": "乙",
            "segments": {"SEG-B": 100}, "cost_curve": [M], "earliest_start": 2027,
            "funding_sources": ["F-CITY"], "prerequisites": ["P-201"]})
        # 新版本引入回边, 构成 P-201 -> P-202 -> P-201 依赖环
        with self.assertRaises(DomainError):
            self.svc.submit_project_version({
                "project_id": "P-201", "version": 2, "name": "甲",
                "segments": {"SEG-A": 100}, "cost_curve": [M], "earliest_start": 2027,
                "funding_sources": ["F-CITY"], "prerequisites": ["P-202"]})


class ScenarioLifecycleTests(ServiceTestCase):
    def test_solve_produces_decisions_for_every_project(self):
        sid = self._solved_scenario()
        scn = self.svc.get_scenario(sid)
        self.assertEqual(len(scn["decisions"]), 8)
        included = {i["project_id"] for i in scn["items"]}
        self.assertIn("P-001", included)
        self.assertIn("P-005", included)   # 依赖 P-001 排在其完工后
        self.assertNotIn("P-008", included)  # 与 P-007 组合互斥

    def test_cannot_submit_unsolved_scenario(self):
        scn = self.svc.create_scenario("空方案", (2027, 2029), "评审员")
        with self.assertRaises(ConflictError):
            self.svc.submit_scenario(scn.scenario_id, "评审员")

    def test_assumption_change_requires_resolve(self):
        sid = self._solved_scenario()
        self.svc.add_assumption(sid, "EXCLUDE", "评审员", project_id="P-001")
        scn = self.svc.get_scenario(sid)
        self.assertEqual(scn["items"], [])  # 旧求解结果被清空
        with self.assertRaises(ConflictError):
            self.svc.submit_scenario(sid, "评审员")

    def test_locked_assumption_changes_outcome(self):
        sid = self._solved_scenario()
        self.svc.add_assumption(sid, "EXCLUDE", "评审员", project_id="P-001")
        self.svc.solve_scenario(sid)
        scn = self.svc.get_scenario(sid)
        included = {i["project_id"] for i in scn["items"]}
        self.assertNotIn("P-001", included)
        self.assertNotIn("P-005", included)  # 前置未入选, 连带落选
        reasons = {d["project_id"]: d["reason_code"] for d in scn["decisions"]}
        self.assertEqual(reasons["P-005"], "DEPENDENCY_UNMET")

    def test_full_approval_flow_to_baseline(self):
        baseline = self._published_baseline()
        self.assertEqual(baseline.revision, 1)
        self.assertEqual(self.svc.current_baseline()["baseline_id"],
                         baseline.baseline_id)
        scn = self.svc.get_scenario(baseline.scenario_id)
        self.assertEqual(scn["status"], "PUBLISHED")

    def test_wrong_status_transitions_rejected(self):
        sid = self._solved_scenario()
        with self.assertRaises(ConflictError):
            self.svc.approve_scenario(sid, "处长")  # 未提交不能审批
        with self.assertRaises(ConflictError):
            self.svc.publish_scenario(sid, "处长")  # 未审批不能发布


class StalenessTests(ServiceTestCase):
    def test_expired_scenario_cannot_publish(self):
        sid = self._solved_scenario(valid_days=10)
        self.svc.submit_scenario(sid, "评审员")
        self.svc.approve_scenario(sid, "处长")
        self.clock.day += timedelta(days=30)  # 超过有效期
        with self.assertRaises(ConflictError) as ctx:
            self.svc.publish_scenario(sid, "处长")
        self.assertIn("过期情景不能直接发布", str(ctx.exception))

    def test_new_project_version_makes_scenario_stale(self):
        sid = self._solved_scenario()
        self.svc.submit_project_version({
            "project_id": "P-001", "version": 2, "name": "老城区供水主干管改造(修编)",
            "segments": {"SEG-001": 1200, "SEG-002": 800},
            "cost_curve": [9 * M, 9 * M], "earliest_start": 2027,
            "funding_sources": ["F-CENTRAL", "F-CITY"]})
        stale = self.svc.scenario_staleness(sid)
        self.assertTrue(any("P-001" in r and "v2" in r for r in stale))

    def test_funding_update_makes_scenario_stale(self):
        sid = self._solved_scenario()
        self.clock.day += timedelta(days=1)
        self.svc.upsert_funding_source({"source_id": "F-CITY", "name": "市财政资金",
                                        "annual_caps": {"2027": 3 * M}})
        stale = self.svc.scenario_staleness(sid)
        self.assertTrue(any("F-CITY" in r for r in stale))

    def test_refresh_produces_new_revision_and_clears_staleness(self):
        sid = self._solved_scenario(valid_days=10)
        self.clock.day += timedelta(days=30)
        self.assertTrue(self.svc.scenario_staleness(sid))
        new = self.svc.refresh_scenario(sid, "评审员")
        self.assertEqual(new.revision, 2)
        self.assertEqual(self.svc.scenario_staleness(sid), [])
        self.assertTrue(new.items)  # 重新求解出方案


class ChangeOrderTests(ServiceTestCase):
    def test_reschedule_change_order_flow(self):
        baseline = self._published_baseline()
        before = baseline.item_map()["P-003"].start_year
        co = self.svc.create_change_order(
            baseline.baseline_id, "RESCHEDULE",
            {"project_id": "P-003", "new_start_year": before + 1},
            "与道路大修同步", "评审员")
        with self.assertRaises(ConflictError):
            self.svc.apply_change_order(co.change_order_id, "处长")  # 未审批
        self.svc.submit_change_order(co.change_order_id, "评审员")
        self.svc.approve_change_order(co.change_order_id, "处长")
        new_baseline = self.svc.apply_change_order(co.change_order_id, "处长")
        self.assertEqual(new_baseline.revision, 2)
        self.assertEqual(new_baseline.item_map()["P-003"].start_year, before + 1)
        self.assertIn(co.change_order_id, new_baseline.change_orders_applied)

    def test_invalid_change_order_blocked_by_validation(self):
        baseline = self._published_baseline()
        co = self.svc.create_change_order(
            baseline.baseline_id, "RESCHEDULE",
            {"project_id": "P-005", "new_start_year": 2027},  # 早于前置 P-001 完工
            "提前实施", "评审员")
        self.svc.submit_change_order(co.change_order_id, "评审员")
        self.svc.approve_change_order(co.change_order_id, "处长")
        with self.assertRaises(DomainError) as ctx:
            self.svc.apply_change_order(co.change_order_id, "处长")
        self.assertIn("前置项目", str(ctx.exception))
        # 校验失败不产生新基线修订
        self.assertEqual(self.svc.get_baseline(baseline.baseline_id)["revision"], 1)

    def test_remove_item_requires_cascade_for_dependents(self):
        baseline = self._published_baseline()
        co = self.svc.create_change_order(
            baseline.baseline_id, "REMOVE_ITEM", {"project_id": "P-001"},
            "暂缓主干管", "评审员")
        with self.assertRaises(DomainError) as ctx:
            self.svc._apply_change_order(co, baseline)
        self.assertIn("P-005", str(ctx.exception))
        co_cascade = self.svc.create_change_order(
            baseline.baseline_id, "REMOVE_ITEM",
            {"project_id": "P-001", "cascade": True}, "暂缓主干管", "评审员")
        items, _ = self.svc._apply_change_order(co_cascade, baseline)
        remaining = {i.project_id for i in items}
        self.assertNotIn("P-001", remaining)
        self.assertNotIn("P-005", remaining)

    def test_change_order_for_unknown_project_rejected(self):
        baseline = self._published_baseline()
        with self.assertRaises(DomainError):
            self.svc.create_change_order(
                baseline.baseline_id, "RESCHEDULE",
                {"project_id": "P-999", "new_start_year": 2028}, "x", "评审员")


class ActualsAndAlertTests(ServiceTestCase):
    def test_backfill_triggers_cost_overrun_alert(self):
        baseline = self._published_baseline()
        # P-001 2027 年计划 800 万, 回填 900 万 -> 超支 12.5%
        self.svc.backfill_actual(baseline.baseline_id, "P-001", "2027",
                                 9 * M, 1500, source="计量系统")
        alerts = self.svc.list_alerts(baseline.baseline_id)
        cost_alerts = [a for a in alerts if a["alert_type"] == "COST_DEVIATION"]
        self.assertEqual(len(cost_alerts), 1)
        self.assertEqual(cost_alerts[0]["severity"], "WARNING")
        self.assertIn("超支", cost_alerts[0]["message"])

    def test_critical_severity_at_double_threshold(self):
        baseline = self._published_baseline()
        self.svc.backfill_actual(baseline.baseline_id, "P-001", "2027",
                                 10_500_000, 100)  # 里程 100 米 vs 计划 1000 米
        alerts = self.svc.list_alerts(baseline.baseline_id)
        mileage = [a for a in alerts if a["alert_type"] == "MILEAGE_LAG"]
        self.assertEqual(mileage[0]["severity"], "CRITICAL")

    def test_rolling_backfill_supersedes_and_clears_alert(self):
        baseline = self._published_baseline()
        self.svc.backfill_actual(baseline.baseline_id, "P-001", "2027", 10 * M, 100)
        self.assertTrue(self.svc.list_alerts(baseline.baseline_id))
        # 更正回填: 支出与里程回归正常 -> 预警消除
        self.svc.backfill_actual(baseline.baseline_id, "P-001", "2027", 8 * M, 1000)
        self.assertEqual(self.svc.list_alerts(baseline.baseline_id), [])
        active = self.svc.list_actuals(baseline.baseline_id, "P-001")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["actual_spend"], 8 * M)

    def test_backfill_rejects_project_outside_baseline(self):
        baseline = self._published_baseline()
        with self.assertRaises(DomainError):
            self.svc.backfill_actual(baseline.baseline_id, "P-006", "2027", M, 0)

    def test_deviation_report_tracks_planned_vs_actual(self):
        baseline = self._published_baseline()
        self.svc.backfill_actual(baseline.baseline_id, "P-001", "2027", 9 * M, 1100)
        report = self.svc.deviation_report(baseline.baseline_id)
        row = next(r for r in report["items"] if r["project_id"] == "P-001")
        self.assertEqual(row["planned_cost_to_date"], 8 * M)
        self.assertEqual(row["actual_cost_to_date"], 9 * M)
        self.assertAlmostEqual(row["cost_deviation_pct"], 12.5)


class ExplainTests(ServiceTestCase):
    def test_selection_explanation_covers_all_projects(self):
        sid = self._solved_scenario()
        exp = self.svc.explain_selection(sid)
        total = exp["summary"]["included"] + exp["summary"]["excluded"]
        self.assertEqual(total, 8)
        for row in exp["included"] + exp["excluded"]:
            self.assertTrue(row["reason"])

    def test_dependency_explanation_shows_propagation(self):
        sid = self._solved_scenario()
        exp = self.svc.explain_dependencies(sid, "P-001")
        self.assertIn("P-005", exp["all_dependents"])
        affected = exp["if_removed"]["affected_projects"]
        self.assertEqual([a["project_id"] for a in affected], ["P-005"])
        delayed = exp["if_delayed_one_year"]["affected_projects"]
        self.assertEqual(delayed[0]["project_id"], "P-005")

    def test_funding_explanation_margins_never_negative(self):
        sid = self._solved_scenario()
        exp = self.svc.explain_funding(sid)
        for year in exp["years"]:
            for src in year["sources"]:
                self.assertGreaterEqual(src["margin"], 0)
                self.assertEqual(src["cap"] - src["allocated"], src["margin"])

    def test_scenario_diff_is_reviewable(self):
        a = self._solved_scenario("方案A")
        b_scn = self.svc.create_scenario("方案B", (2027, 2029), "评审员",
                                         config={"overlap_policy": "forbid"})
        self.svc.solve_scenario(b_scn.scenario_id)
        diff = self.svc.compare_scenarios(a, b_scn.scenario_id)
        self.assertIn("P-003", diff["items_removed"])  # 禁止重叠策略下 P-003 落选
        self.assertEqual(diff["from_scenario"]["id"], a)
        self.assertTrue(diff["decision_changes"])


class AuditTests(ServiceTestCase):
    def test_audit_trail_records_key_actions(self):
        baseline = self._published_baseline()
        actions = [r["action"] for r in self.svc.audit_trail()]
        for expected in ("CREATE_SCENARIO", "SOLVE_SCENARIO", "SUBMIT_SCENARIO",
                         "APPROVE_SCENARIO", "PUBLISH_BASELINE"):
            self.assertIn(expected, actions)
        self.assertTrue(self.svc.audit_trail(baseline.baseline_id))


if __name__ == "__main__":
    unittest.main()
