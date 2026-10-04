import unittest

from helpers import make_project
from capital_portfolio.domain import (
    ASSUME_CAP_OVERRIDE, ASSUME_EXCLUDE, ASSUME_INCLUDE, ASSUME_PIN_START,
    Assumption, InfeasibleError, ReasonCode,
)
from capital_portfolio.planner import SolveConfig, solve, validate_items

M = 1_000_000


def cfg(caps=None, horizon=(2027, 2029), policy="dedupe", multipliers=None):
    return SolveConfig(horizon=horizon,
                       caps=caps or {"F1": {y: 10 * M for y in range(2027, 2030)}},
                       metric_values={"leak_reduction_m3": 3.0},
                       overlap_policy=policy,
                       priority_multipliers=multipliers or {})


def lock(kind, pid=None, value=None, aid="AS-1"):
    return Assumption(assumption_id=aid, kind=kind, project_id=pid, value=value or {},
                      note="", locked_by="tester", locked_at="2027-01-01")


def decisions_of(result):
    return {d.project_id: d for d in result.decisions}


class BudgetConstraintTests(unittest.TestCase):
    def test_lower_score_project_excluded_when_budget_runs_out(self):
        projects = {
            "A": make_project("A", [8 * M], risk_score=20),
            "B": make_project("B", [8 * M], risk_score=5),
        }
        result = solve(projects, cfg(caps={"F1": {2027: 10 * M}}), [])
        d = decisions_of(result)
        self.assertEqual(d["A"].decision, "INCLUDED")
        self.assertEqual(d["B"].decision, "EXCLUDED")
        self.assertEqual(d["B"].reason_code, ReasonCode.BUDGET_INSUFFICIENT.value)
        self.assertIn("缺口", d["B"].reason)

    def test_annual_occupancy_never_exceeds_cap(self):
        projects = {f"P{i}": make_project(f"P{i}", [4 * M, 4 * M], risk_score=50 - i)
                    for i in range(4)}
        caps = {"F1": {y: 10 * M for y in range(2027, 2030)}}
        result = solve(projects, cfg(caps=caps), [])
        for year in (2027, 2028, 2029):
            used = sum(a.get("F1", 0) for i in result.items
                       for y, a in i.funding_plan.items() if y == year)
            self.assertLessEqual(used, caps["F1"][year])

    def test_cap_override_assumption_tightens_budget(self):
        projects = {"A": make_project("A", [8 * M], risk_score=20),
                    "B": make_project("B", [8 * M], risk_score=5)}
        result = solve(projects, cfg(horizon=(2027, 2027)),
                       [lock(ASSUME_CAP_OVERRIDE, value={
                           "source": "F1", "year": 2027, "cap": 9 * M})])
        d = decisions_of(result)
        self.assertEqual(d["A"].decision, "INCLUDED")
        self.assertEqual(d["B"].decision, "EXCLUDED")


class DependencyTests(unittest.TestCase):
    def test_dependent_starts_after_prerequisite_completion(self):
        projects = {
            "A": make_project("A", [5 * M, 5 * M], risk_score=30),
            "B": make_project("B", [5 * M], prereqs=("A",), risk_score=10),
        }
        result = solve(projects, cfg(), [])
        starts = {i.project_id: i.start_year for i in result.items}
        self.assertEqual(starts["A"], 2027)
        self.assertEqual(starts["B"], 2029)  # A 2027-2028 完工后

    def test_dependent_excluded_when_prerequisite_excluded(self):
        projects = {
            "A": make_project("A", [9 * M], risk_score=1),   # 得分低且挤占预算
            "B": make_project("B", [9 * M], risk_score=50),
            "C": make_project("C", [1 * M], prereqs=("A",), risk_score=40),
        }
        result = solve(projects, cfg(caps={"F1": {2027: 10 * M}}), [])
        d = decisions_of(result)
        self.assertEqual(d["A"].decision, "EXCLUDED")
        self.assertEqual(d["C"].decision, "EXCLUDED")
        self.assertEqual(d["C"].reason_code, ReasonCode.DEPENDENCY_UNMET.value)
        self.assertIn("A", d["C"].reason)

    def test_cycle_rejected(self):
        projects = {"A": make_project("A", [1], prereqs=("B",)),
                    "B": make_project("B", [1], prereqs=("A",))}
        with self.assertRaises(InfeasibleError):
            solve(projects, cfg(), [])


class ExclusionTests(unittest.TestCase):
    def test_same_year_exclusion_defers_lower_score_project(self):
        projects = {
            "A": make_project("A", [4 * M], risk_score=30, exclusive=(("B", "same_year"),)),
            "B": make_project("B", [4 * M], risk_score=20),
        }
        caps = {"F1": {2027: 10 * M, 2028: 10 * M}}
        result = solve(projects, cfg(caps=caps, horizon=(2027, 2028)), [])
        starts = {i.project_id: i.start_year for i in result.items}
        self.assertEqual(starts["A"], 2027)
        self.assertEqual(starts["B"], 2028)  # 互斥施工顺延

    def test_portfolio_exclusion_keeps_higher_score_only(self):
        projects = {
            "A": make_project("A", [4 * M], risk_score=30, exclusive=(("B", "portfolio"),)),
            "B": make_project("B", [4 * M], risk_score=20, exclusive=(("A", "portfolio"),)),
        }
        result = solve(projects, cfg(), [])
        d = decisions_of(result)
        self.assertEqual(d["A"].decision, "INCLUDED")
        self.assertEqual(d["B"].decision, "EXCLUDED")
        self.assertEqual(d["B"].reason_code, ReasonCode.EXCLUSION_CONFLICT.value)


class AssumptionTests(unittest.TestCase):
    def test_locked_include_overrides_low_score(self):
        projects = {"A": make_project("A", [8 * M], risk_score=1),
                    "B": make_project("B", [8 * M], risk_score=50)}
        result = solve(projects, cfg(caps={"F1": {2027: 10 * M}}),
                       [lock(ASSUME_INCLUDE, "A")])
        d = decisions_of(result)
        self.assertEqual(d["A"].decision, "INCLUDED")
        self.assertEqual(d["A"].reason_code, ReasonCode.LOCKED_INCLUDE.value)

    def test_locked_exclude_records_reason(self):
        projects = {"A": make_project("A", [1 * M], risk_score=50)}
        result = solve(projects, cfg(), [lock(ASSUME_EXCLUDE, "A")])
        d = decisions_of(result)
        self.assertEqual(d["A"].decision, "EXCLUDED")
        self.assertEqual(d["A"].reason_code, ReasonCode.LOCKED_EXCLUDE.value)

    def test_contradictory_locks_rejected(self):
        projects = {"A": make_project("A", [1 * M])}
        with self.assertRaises(InfeasibleError):
            solve(projects, cfg(), [lock(ASSUME_INCLUDE, "A", aid="AS-1"),
                                    lock(ASSUME_EXCLUDE, "A", aid="AS-2")])

    def test_locked_include_with_locked_out_prerequisite_rejected(self):
        projects = {"A": make_project("A", [1 * M]),
                    "B": make_project("B", [1 * M], prereqs=("A",))}
        with self.assertRaises(InfeasibleError):
            solve(projects, cfg(), [lock(ASSUME_INCLUDE, "B", aid="AS-1"),
                                    lock(ASSUME_EXCLUDE, "A", aid="AS-2")])

    def test_locked_include_infeasible_budget_rejected(self):
        projects = {"A": make_project("A", [50 * M])}
        with self.assertRaises(InfeasibleError):
            solve(projects, cfg(caps={"F1": {2027: 10 * M}}),
                  [lock(ASSUME_INCLUDE, "A")])

    def test_pin_start_forces_start_year(self):
        projects = {"A": make_project("A", [2 * M])}
        result = solve(projects, cfg(),
                       [lock(ASSUME_PIN_START, "A", {"start_year": 2029})])
        self.assertEqual(result.items[0].start_year, 2029)

    def test_priority_multiplier_changes_ranking(self):
        projects = {"A": make_project("A", [8 * M], risk_score=30),
                    "B": make_project("B", [8 * M], risk_score=20)}
        caps = {"F1": {2027: 10 * M}}
        result = solve(projects, cfg(caps=caps, multipliers={"B": 5.0}), [])
        d = decisions_of(result)
        self.assertEqual(d["B"].decision, "INCLUDED")
        self.assertEqual(d["A"].decision, "EXCLUDED")


class OverlapPolicyTests(unittest.TestCase):
    def _overlapping(self):
        return {
            "A": make_project("A", [2 * M], segments={"S1": 500}, risk_score=30),
            "B": make_project("B", [2 * M], segments={"S1": 500}, risk_score=20),
        }

    def test_forbid_policy_excludes_overlapping_project(self):
        result = solve(self._overlapping(), cfg(policy="forbid"), [])
        d = decisions_of(result)
        self.assertEqual(d["A"].decision, "INCLUDED")
        self.assertEqual(d["B"].decision, "EXCLUDED")
        self.assertEqual(d["B"].reason_code, ReasonCode.OVERLAP_CONFLICT.value)

    def test_dedupe_policy_allows_but_reports_overlap(self):
        result = solve(self._overlapping(), cfg(policy="dedupe"), [])
        self.assertEqual(len(result.items), 2)
        kinds = {o["kind"] for o in result.overlaps}
        self.assertIn("SCOPE_OVERLAP", kinds)

    def test_benefit_double_counting_deduplicated(self):
        from capital_portfolio.domain import BenefitClaim
        pa = make_project("A", [2 * M], segments={"S1": 500}, risk_score=30,
                          benefits=(BenefitClaim("S1", "leak_reduction_m3", 1000),))
        pb = make_project("B", [2 * M], segments={"S1": 500}, risk_score=20,
                          benefits=(BenefitClaim("S1", "leak_reduction_m3", 800),))
        result = solve({"A": pa, "B": pb}, cfg(), [])
        self.assertEqual(result.benefit_total, 1000)  # 归属开工更早的 A
        conflict = [o for o in result.overlaps if o["kind"] == "BENEFIT_CONFLICT"]
        self.assertEqual(conflict[0]["double_counted"], 800)


class ValidateItemsTests(unittest.TestCase):
    def test_detects_budget_overrun_and_dependency_violation(self):
        projects = {
            "A": make_project("A", [5 * M, 5 * M]),
            "B": make_project("B", [5 * M], prereqs=("A",)),
        }
        from capital_portfolio.domain import ScenarioItem
        bad = [
            ScenarioItem("A", 1, 2027, {2027: {"F1": 5 * M}, 2028: {"F1": 5 * M}}, 0),
            ScenarioItem("B", 1, 2028, {2028: {"F1": 5 * M}}, 0),  # 早于 A 完工
        ]
        violations = validate_items(bad, projects, {"F1": {2027: 6 * M, 2028: 6 * M}})
        text = "; ".join(violations)
        self.assertIn("前置项目", text)
        self.assertIn("超支", text)  # 2028 占用 1000 万 > 600 万


if __name__ == "__main__":
    unittest.main()
