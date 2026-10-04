"""改造投资组合治理服务的端到端测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from capital_portfolio import analysis
from capital_portfolio.contracts import (
    BudgetEnvelope,
    Dependency,
    FundingCommitment,
    FundingPool,
    ProjectVersion,
    RiskEvidence,
)
from capital_portfolio.engine import PlanError, PlanWeights, build_plan
from capital_portfolio.governance import (
    AdjustmentItem,
    GovernanceError,
    ScenarioAssumptions,
    compare_scenarios,
)
from capital_portfolio.service import PortfolioService

ZERO_WEIGHTS = PlanWeights(0, 0)


def pv(pid, assets, cost, *, version=1, benefit=None, deps=(), funds=(),
       mutex=(), curve=None, base=2027, mileage=0.0, risks=()):
    return ProjectVersion(
        pid, version, tuple(assets), cost,
        base_year=base, mileage=mileage,
        benefit_by_asset=benefit or {},
        depends_on=tuple(Dependency(d) if isinstance(d, str) else d for d in deps),
        funding_sources=tuple(FundingCommitment(f, 1.0) if isinstance(f, str) else f
                              for f in funds),
        mutex_groups=tuple(mutex),
        cost_curve=curve or {},
        risk_evidence=tuple(risks),
    )


class ContractTests(unittest.TestCase):
    def test_cost_curve_must_match_total(self):
        with self.assertRaises(ValueError):
            pv("X", ["a"], 100, curve={0: 60, 1: 30})

    def test_cost_curve_defaults_to_single_year(self):
        self.assertEqual(pv("X", ["a"], 100).scheduled_curve(2028), {2028: 100})

    def test_funding_share_caps_at_one(self):
        with self.assertRaises(ValueError):
            pv("X", ["a"], 100,
               funds=(FundingCommitment("g", 0.6), FundingCommitment("b", 0.6)))

    def test_benefit_asset_must_be_in_scope(self):
        with self.assertRaises(ValueError):
            pv("X", ["a"], 100, benefit={"other": 10})

    def test_self_dependency_rejected(self):
        with self.assertRaises(ValueError):
            ProjectVersion("X", 1, ("a",), 10, depends_on=(Dependency("X"),))

    def test_risk_severity_range(self):
        with self.assertRaises(ValueError):
            RiskEvidence("r", "渗漏", 9, "src", "2026-01-01")


class OverlapTests(unittest.TestCase):
    def test_duplicate_benefit_identified_per_asset(self):
        projects = [
            pv("X", ["s1"], 10, benefit={"s1": 100}),
            pv("Y", ["s1", "s2"], 10, benefit={"s1": 80, "s2": 30}),
        ]
        overlaps = analysis.find_scope_overlaps(projects)
        self.assertEqual([o.asset_id for o in overlaps], ["s1"])
        self.assertEqual(overlaps[0].duplicated_benefit, 80)

    def test_benefit_attributed_once_in_priority_order(self):
        x = pv("X", ["s1"], 10, benefit={"s1": 100})
        y = pv("Y", ["s1"], 10, benefit={"s1": 80})
        recognized, winner, dup = analysis.attribute_benefits([x, y])
        self.assertEqual(recognized, {"X": 100, "Y": 0})
        self.assertEqual(winner, {"s1": "X"})
        self.assertEqual(dup, 80)
        # 调换优先级顺序，归属随之改变
        recognized2, _, _ = analysis.attribute_benefits([y, x])
        self.assertEqual(recognized2, {"Y": 80, "X": 0})

    def test_only_latest_version_counts(self):
        old = pv("X", ["s1"], 10, version=1, benefit={"s1": 100})
        new = pv("X", ["s2"], 10, version=2, benefit={"s2": 100})
        y = pv("Y", ["s1"], 10, benefit={"s1": 5})
        # 最新版 X 的范围已不含 s1，故与 Y 不构成重叠
        self.assertEqual(analysis.find_scope_overlaps([old, new, y]), [])


class DependencyAnalysisTests(unittest.TestCase):
    def test_reverse_closure_and_path(self):
        projects = [
            pv("A", ["a"], 10), pv("B", ["b"], 10, deps=("A",)),
            pv("C", ["c"], 10, deps=("B",)),
        ]
        reverse = analysis.build_reverse_dependencies(projects)
        self.assertEqual(reverse["A"], {"B", "C"})

    def test_cycle_detection(self):
        projects = [
            pv("A", ["a"], 10, deps=("B",)),
            pv("B", ["b"], 10, deps=("A",)),
        ]
        self.assertEqual(analysis.find_dependency_cycles(projects), [["A", "B"]])


def fixture_service() -> PortfolioService:
    """A(seg1) <- B(seg1,seg2); C(seg2) 与 D(seg3) 道路互斥；
    E 远期；F(seg9) 小项目。预算 2027/2028 各 300。"""
    svc = PortfolioService()
    for y in (2027, 2028):
        svc.set_budget(BudgetEnvelope(y, 300))
        svc.add_funding_pool(FundingPool("一般财政", 300, y))
    for p in (
        pv("A", ["seg1"], 100, benefit={"seg1": 50}, funds=("一般财政",)),
        pv("B", ["seg1", "seg2"], 200, benefit={"seg1": 40, "seg2": 30},
           deps=("A",), funds=("一般财政",)),
        pv("C", ["seg2"], 150, mutex=("M",), funds=("一般财政",)),
        pv("D", ["seg3"], 150, mutex=("M",), funds=("一般财政",)),
        pv("E", ["seg4"], 500, base=2030, funds=("一般财政",)),
        pv("F", ["seg9"], 50, funds=("一般财政",)),
    ):
        svc.register_project(p)
    return svc


class PlanEngineTests(unittest.TestCase):
    def setUp(self):
        self.svc = fixture_service()
        self.projects = list(self.svc.latest_projects().values())
        self.budgets = {2027: 300, 2028: 300}
        self.pools = [FundingPool("一般财政", 300, y) for y in (2027, 2028)]

    def plan(self, **kw):
        return build_plan(self.projects, self.budgets, self.pools,
                          weights=ZERO_WEIGHTS, **kw)

    def test_basic_schedule_and_budget_headroom(self):
        plan = self.plan()
        sel = {d.project_id: d.start_year for d in plan.selected}
        self.assertEqual(sel, {"A": 2027, "B": 2028, "C": 2027, "F": 2027})
        self.assertEqual(int(plan.budget_usage[2027].remaining), 0)
        self.assertEqual(int(plan.budget_usage[2028].remaining), 100)

    def test_mutex_blocks_alternative(self):
        plan = self.plan()
        d = plan.decision("D")
        self.assertFalse(d.selected)
        self.assertIn("no_feasible_window", d.reason_codes)
        self.assertTrue(any("互斥" in r for r in d.reasons))

    def test_out_of_horizon_project_is_deferred_with_reason(self):
        d = self.plan().decision("E")
        self.assertEqual(d.status, "deferred")
        self.assertTrue(any("2030" in r for r in d.reasons))

    def test_shared_asset_cannot_be_constructed_concurrently(self):
        projects = [pv("P1", ["X"], 50, funds=("一般财政",)),
                    pv("P2", ["X"], 50, funds=("一般财政",))]
        plan = build_plan(projects, {2027: 200, 2028: 200}, self.pools,
                          weights=ZERO_WEIGHTS)
        self.assertEqual(plan.decision("P1").start_year, 2027)
        self.assertEqual(plan.decision("P2").start_year, 2028)

    def test_shared_funding_pool_caps_across_years(self):
        projects = [
            pv("Q1", ["q1"], 60, funds=(FundingCommitment("专项债", 1.0),)),
            pv("Q2", ["q2"], 60, funds=(FundingCommitment("专项债", 1.0),)),
        ]
        pools = [FundingPool("专项债", 100)]
        plan = build_plan(projects, {2027: 500, 2028: 500}, pools,
                          weights=ZERO_WEIGHTS)
        self.assertTrue(plan.decision("Q1").selected)
        self.assertFalse(plan.decision("Q2").selected)
        self.assertTrue(any("专项债" in r for r in plan.decision("Q2").reasons))

    def test_dependency_lag_pushes_start_year(self):
        projects = [
            pv("X", ["x"], 10, funds=("一般财政",)),
            pv("Y", ["y"], 10, deps=(Dependency("X", lag_years=1),),
               funds=("一般财政",)),
        ]
        pools = [FundingPool("一般财政", 300, y) for y in (2027, 2028, 2029)]
        plan = build_plan(projects, {2027: 300, 2028: 300, 2029: 300}, pools,
                          weights=ZERO_WEIGHTS)
        self.assertEqual(plan.decision("Y").start_year, 2029)

    def test_excluding_upstream_cascades_downstream(self):
        plan = self.plan(excluded=["A"])
        self.assertFalse(plan.decision("B").selected)
        self.assertEqual(plan.decision("B").reason_codes, ["dependency_blocked"])
        self.assertEqual(plan.decision("B").blocker_chain, ["A", "B"])
        self.assertIn("A", plan.propagation)
        self.assertEqual(plan.propagation["A"]["downstream"], ["B"])

    def test_forced_in_conflict_raises_with_violations(self):
        with self.assertRaises(PlanError) as ctx:
            self.plan(forced_in={"E": None})
        self.assertTrue(ctx.exception.violations)

    def test_forced_in_respects_mutex(self):
        plan = self.plan(forced_in={"D": 2027})
        self.assertTrue(plan.decision("D").selected)
        self.assertFalse(plan.decision("C").selected)

    def test_forced_in_fails_when_prerequisite_excluded(self):
        with self.assertRaises(PlanError):
            self.plan(forced_in={"B": None}, excluded=["A"])

    def test_every_decision_is_explained(self):
        plan = self.plan()
        for d in plan.decisions:
            text = plan.explain(d.project_id)
            self.assertIn(d.project_id, text)
            self.assertIn("评分构成", text)

    def test_missing_dependency_is_infeasible(self):
        projects = [pv("A", ["a"], 10, deps=("GHOST",), funds=("一般财政",))]
        plan = build_plan(projects, self.budgets, self.pools, weights=ZERO_WEIGHTS)
        self.assertEqual(plan.decision("A").status, "infeasible")
        self.assertEqual(plan.decision("A").reason_codes, ["missing_dependency"])


class ScenarioGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.svc = fixture_service()

    def test_lock_assumptions_change_plan(self):
        s1 = self.svc.new_scenario("S1", "方案一")
        plan1 = self.svc.compute("S1")
        self.assertTrue(plan1.decision("C").selected)
        self.assertFalse(plan1.decision("D").selected)

        s2 = self.svc.new_scenario("S2", "方案二", clone_from="S1")
        s2.lock_in("D", 2027)
        s2.lock_exclusion("C")
        plan2 = self.svc.compute("S2")
        self.assertFalse(plan2.decision("C").selected)
        self.assertTrue(plan2.decision("D").selected)

    def test_priority_boost_reorders(self):
        s = self.svc.new_scenario("S", "提权",
                                  weights=PlanWeights(0, 0, {"D": 1}))
        # D 仅加 1 分仍与 C 同分；改为大幅加分验证互斥组中 D 胜出
        s.set_priority_boost("D", 10_000)
        plan = self.svc.compute("S")
        self.assertTrue(plan.decision("D").selected)
        self.assertFalse(plan.decision("C").selected)

    def test_expired_scenario_cannot_submit_or_publish(self):
        s = self.svc.new_scenario("SX", "过期", expires_on="2026-01-01")
        with self.assertRaises(GovernanceError):
            self.svc.compute("SX")
        with self.assertRaises(GovernanceError):
            self.svc.submit_scenario("SX")
        self.assertEqual(s.status, "expired")

    def test_submit_publish_baseline_and_compare(self):
        self.svc.new_scenario("S1", "方案一")
        s2 = self.svc.new_scenario("S2", "方案二")
        s2.lock_in("D", 2027)
        s2.lock_exclusion("C")
        self.svc.compute("S1")
        self.svc.compute("S2")

        diff = compare_scenarios(self.svc.scenarios["S1"],
                                 self.svc.scenarios["S2"])
        self.assertIn("C", diff.only_left)
        self.assertIn("D", diff.only_right)
        self.assertTrue(any("锁定" in c for c in diff.assumption_changes))
        self.assertIn("C: selected → deferred", diff.as_text())

        self.svc.submit_scenario("S1")
        baseline = self.svc.publish("S1", "BL-1", "2027-01-10")
        self.assertEqual(baseline.revision, 1)
        self.assertEqual(self.svc.scenarios["S1"].status, "approved")
        # 已发布情景不能再改假设
        with self.assertRaises(GovernanceError):
            self.svc.scenarios["S1"].lock_in("D", 2027)

    def test_locked_assumption_unsatisfiable_rejected(self):
        s = self.svc.new_scenario("SBAD", "强锁远期")
        s.lock_in("E", 2027)
        with self.assertRaises(GovernanceError):
            self.svc.compute("SBAD")


class BaselineAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.svc = fixture_service()
        self.svc.new_scenario("S1", "方案一")
        self.svc.submit_scenario("S1")
        self.svc.publish("S1", "BL-1", "2027-01-10")
        self.bl = self.svc.baselines["BL-1"]

    def test_remove_cascades_dependents_and_versions(self):
        before = dict(self.bl.schedule)
        order_items = [AdjustmentItem("remove", "A", reason="暂缓")]
        result = self.svc.create_adjustment(
            "BL-1", "ADJ-1", order_items, apply=True)
        self.assertEqual(result["preview"]["cascade_removed"], ["B"])
        new_bl = self.svc.baselines["BL-1"]
        self.assertEqual(new_bl.revision, 2)
        self.assertNotIn("A", new_bl.schedule)
        self.assertNotIn("B", new_bl.schedule)
        self.assertIn("C", new_bl.schedule)
        self.assertEqual(new_bl.adjustments[0]["order_id"], "ADJ-1")
        # 原基线对象保持不变（不可变版本）
        self.assertEqual(self.bl.revision, 1)
        self.assertIn("A", before)

    def test_mutex_add_is_rejected(self):
        with self.assertRaises(GovernanceError):
            self.svc.create_adjustment(
                "BL-1", "ADJ-X",
                [AdjustmentItem("add", "D", start_year=2027)])

    def test_retime_violating_dependency_rejected(self):
        # A 推迟到 2028，但其下游 B 已排 2028，依赖约束失败
        with self.assertRaises(GovernanceError):
            self.svc.create_adjustment(
                "BL-1", "ADJ-X",
                [AdjustmentItem("retime", "A", start_year=2028)])

    def test_valid_retime_passes_preview(self):
        result = self.svc.create_adjustment(
            "BL-1", "ADJ-2",
            [AdjustmentItem("retime", "F", start_year=2028)])
        self.assertEqual(result["preview"]["schedule"]["F"], 2028)
        # 仅预演未应用：基线仍是 v1
        self.assertEqual(self.svc.baselines["BL-1"].revision, 1)

    def test_stale_order_revision_rejected(self):
        self.svc.create_adjustment(
            "BL-1", "ADJ-1", [AdjustmentItem("remove", "A")], apply=True)
        stale = type("O", (), {})()
        from capital_portfolio.governance import AdjustmentOrder
        order = AdjustmentOrder("OLD", 1, [AdjustmentItem("retime", "F", 2028)])
        from capital_portfolio.governance import validate_adjustment
        with self.assertRaises(GovernanceError):
            validate_adjustment(order, self.svc.baselines["BL-1"],
                                self.svc.latest_projects(), self.svc.pools)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.svc = fixture_service()
        # A 需要里程数据用于里程预警
        self.svc.projects[0] = pv("A", ["seg1"], 100, benefit={"seg1": 50},
                                  funds=("一般财政",), mileage=1000.0)
        self.svc.new_scenario("S1", "方案一")
        self.svc.submit_scenario("S1")
        self.svc.publish("S1", "BL-1", "2027-01-10")

    def test_overrun_warn_and_major(self):
        alerts = self.svc.backfill("A", 2027, 115, 1000.0)
        kinds = [a["kind"] for a in alerts]
        self.assertIn("cost_overrun", kinds)
        self.assertEqual(alerts[0]["level"], "warn")

        alerts = self.svc.backfill("A", 2027, 130, 1000.0)
        self.assertEqual(alerts[0]["level"], "major")

    def test_mileage_lag_alert(self):
        alerts = self.svc.backfill("A", 2027, 100, 800.0)
        self.assertTrue(any(a["kind"] == "mileage_lag" for a in alerts))

    def test_backfill_outside_schedule_rejected(self):
        with self.assertRaises(GovernanceError):
            self.svc.backfill("A", 2030, 1, 1.0)
        with self.assertRaises(GovernanceError):
            self.svc.backfill("E", 2027, 1, 1.0)


class ServiceSurfaceTests(unittest.TestCase):
    def test_overview_and_overlap_reports_are_jsonable(self):
        svc = fixture_service()
        svc.new_scenario("S1", "方案一")
        overview = svc.overview("S1")
        self.assertEqual(overview["status"], "draft")
        self.assertIn("budget_headroom", overview)
        self.assertTrue(svc.overlap_report())
        svc.compute  # attr check

    def test_register_new_version_replaces_latest(self):
        svc = fixture_service()
        svc.register_project(pv("F", ["seg9"], 99, version=2))
        self.assertEqual(svc.latest_projects()["F"].version, 2)
        self.assertEqual(svc.latest_projects()["F"].estimated_cost, 99)


class PersistenceTests(unittest.TestCase):
    def test_scenario_and_baseline_round_trip(self):
        import tempfile
        from capital_portfolio.persistence import load_state, save_state

        svc = fixture_service()
        svc.new_scenario("S1", "方案一")
        svc.scenarios["S1"].set_priority_boost("F", 123)
        svc.submit_scenario("S1")
        svc.publish("S1", "BL-1", "2027-01-10")
        svc.backfill("A", 2027, 115, 1000.0)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(svc, path)

            restored = fixture_service()
            self.assertTrue(load_state(restored, path))
            self.assertEqual(restored.scenarios["S1"].status, "approved")
            self.assertEqual(
                restored.scenarios["S1"].assumptions.weights.manual_boost,
                {"F": 123.0})
            bl = restored.baselines["BL-1"]
            self.assertEqual(bl.schedule["A"], 2027)
            # 年度键必须保持 int，预算余量与实际值可用
            self.assertIn(2027, bl.budget_remaining)
            self.assertEqual(bl.actual_spend["A@2027"], 115)
            self.assertTrue(restored.alerts)


if __name__ == "__main__":
    unittest.main()
