import unittest

from helpers import make_project
from capital_portfolio.analysis import (
    dedupe_benefits, dependency_graph, detect_overlaps, find_cycles,
    propagate_delay, propagate_removal, transitive_dependents,
)
from capital_portfolio.domain import BenefitClaim, DomainError, ScenarioItem


def item(pid, start=2027):
    return ScenarioItem(project_id=pid, version=1, start_year=start,
                        funding_plan={}, score=0.0)


class OverlapTests(unittest.TestCase):
    def test_scope_overlap_detected(self):
        projects = {
            "A": make_project("A", [1], segments={"S1": 500, "S2": 300}),
            "B": make_project("B", [1], segments={"S2": 300, "S3": 100}),
        }
        findings = detect_overlaps(projects)
        overlap = [f for f in findings if f["kind"] == "SCOPE_OVERLAP"]
        self.assertEqual(len(overlap), 1)
        self.assertEqual(overlap[0]["segment_id"], "S2")
        self.assertEqual(overlap[0]["projects"], ["A", "B"])
        self.assertEqual(overlap[0]["overlap_length_m"], 300)

    def test_benefit_double_counting_detected(self):
        claim = lambda seg, v: (BenefitClaim(seg, "leak_reduction_m3", v),)
        projects = {
            "A": make_project("A", [1], segments={"S1": 500}, benefits=claim("S1", 1000)),
            "B": make_project("B", [1], segments={"S1": 500}, benefits=claim("S1", 800)),
        }
        findings = detect_overlaps(projects)
        conflict = [f for f in findings if f["kind"] == "BENEFIT_CONFLICT"]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(conflict[0]["claimed_total"], 1800)
        self.assertEqual(conflict[0]["deduplicated"], 1000)
        self.assertEqual(conflict[0]["double_counted"], 800)

    def test_benefit_dedupe_attributes_to_earliest_start(self):
        claim = lambda v: (BenefitClaim("S1", "leak_reduction_m3", v),)
        projects = {
            "A": make_project("A", [1], segments={"S1": 500}, benefits=claim(1000)),
            "B": make_project("B", [1], segments={"S1": 500}, benefits=claim(800)),
        }
        items = [item("A", start=2028), item("B", start=2027)]
        total, by_metric, attribution = dedupe_benefits(projects, items)
        self.assertEqual(total, 800)  # B 开工更早, 收益归属 B
        counted = {a["project_id"]: a["counted"] for a in attribution}
        self.assertEqual(counted, {"A": False, "B": True})


class DependencyTests(unittest.TestCase):
    def test_cycle_detected(self):
        projects = {"A": make_project("A", [1], prereqs=("B",)),
                    "B": make_project("B", [1], prereqs=("A",))}
        cycles = find_cycles(dependency_graph(projects))
        self.assertTrue(cycles)

    def test_missing_prerequisite_rejected(self):
        projects = {"A": make_project("A", [1], prereqs=("GHOST",))}
        with self.assertRaises(DomainError):
            dependency_graph(projects)

    def test_transitive_dependents(self):
        projects = {"A": make_project("A", [1]),
                    "B": make_project("B", [1], prereqs=("A",)),
                    "C": make_project("C", [1], prereqs=("B",))}
        graph = dependency_graph(projects)
        self.assertEqual(transitive_dependents(graph, "A"), ["B", "C"])

    def test_propagate_removal_cascades(self):
        projects = {"A": make_project("A", [1]),
                    "B": make_project("B", [1], prereqs=("A",)),
                    "C": make_project("C", [1], prereqs=("B",)),
                    "D": make_project("D", [1])}
        graph = dependency_graph(projects)
        items = [item("A"), item("B"), item("C"), item("D")]
        affected = propagate_removal(graph, items, "A")
        self.assertEqual([a["project_id"] for a in affected], ["B", "C"])

    def test_propagate_delay_shifts_dependents(self):
        projects = {"A": make_project("A", [1, 1]),          # 2 年工期
                    "B": make_project("B", [1], prereqs=("A",)),
                    "C": make_project("C", [1], prereqs=("B",))}
        graph = dependency_graph(projects)
        items = [item("A", 2027), item("B", 2029), item("C", 2030)]
        shifts = propagate_delay(graph, projects, items, "A", 1)
        by_pid = {s["project_id"]: s for s in shifts}
        self.assertEqual(by_pid["B"]["required_start"], 2030)
        self.assertEqual(by_pid["C"]["required_start"], 2031)


if __name__ == "__main__":
    unittest.main()
