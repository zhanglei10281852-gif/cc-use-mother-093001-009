import unittest

from fastapi.testclient import TestClient

from helpers import make_service
from capital_portfolio.api import create_app

M = 1_000_000


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        svc, _ = make_service()
        cls.client = TestClient(create_app(service=svc))

    def test_projects_and_funding_listed(self):
        projects = self.client.get("/projects").json()
        self.assertEqual(len(projects), 8)
        sources = self.client.get("/funding-sources").json()
        self.assertEqual(len(sources), 3)

    def test_scenario_flow_and_explain_endpoints(self):
        r = self.client.post("/scenarios", json={
            "name": "API方案", "horizon": [2027, 2029], "created_by": "测试员"})
        self.assertEqual(r.status_code, 201)
        sid = r.json()["scenario_id"]

        r = self.client.post(f"/scenarios/{sid}/solve")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["items"])

        sel = self.client.get(f"/scenarios/{sid}/explain/selection").json()
        self.assertEqual(sel["summary"]["included"] + sel["summary"]["excluded"], 8)

        dep = self.client.get(f"/scenarios/{sid}/explain/dependencies/P-001").json()
        self.assertIn("P-005", dep["all_dependents"])

        fund = self.client.get(f"/scenarios/{sid}/explain/funding").json()
        self.assertEqual(len(fund["years"]), 3)

        self.client.post(f"/scenarios/{sid}/submit", json={"actor": "测试员"})
        self.client.post(f"/scenarios/{sid}/approve", json={"actor": "处长"})
        r = self.client.post(f"/scenarios/{sid}/publish", json={"actor": "处长"})
        self.assertEqual(r.status_code, 201)
        baseline_id = r.json()["baseline_id"]

        r = self.client.post(f"/baselines/{baseline_id}/actuals", json={
            "project_id": "P-001", "period": "2027", "actual_spend": 10 * M,
            "mileage_completed_m": 100})
        self.assertEqual(r.status_code, 201)
        alerts = self.client.get(f"/baselines/{baseline_id}/alerts").json()
        self.assertTrue(alerts)
        deviation = self.client.get(f"/baselines/{baseline_id}/deviation").json()
        self.assertEqual(len(deviation["items"]), len(sel["included"]))

    def test_validation_error_maps_to_422(self):
        r = self.client.post("/projects/versions", json={
            "project_id": "P-001", "version": 1, "name": "重复",
            "segments": {"SEG-X": 1}, "cost_curve": [1], "earliest_start": 2027,
            "funding_sources": ["F-CITY"]})
        self.assertEqual(r.status_code, 409)

    def test_not_found_maps_to_404(self):
        r = self.client.get("/scenarios/SCN-9999")
        self.assertEqual(r.status_code, 404)

    def test_compare_endpoint(self):
        def make(name, policy):
            r = self.client.post("/scenarios", json={
                "name": name, "horizon": [2027, 2029], "created_by": "测试员",
                "config": {"overlap_policy": policy}})
            sid = r.json()["scenario_id"]
            self.client.post(f"/scenarios/{sid}/solve")
            return sid
        a, b = make("A", "dedupe"), make("B", "forbid")
        r = self.client.get("/compare", params={"a": a, "b": b})
        self.assertEqual(r.status_code, 200)
        self.assertIn("P-003", r.json()["items_removed"])


if __name__ == "__main__":
    unittest.main()
