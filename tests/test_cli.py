import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import make_service  # noqa: F401  (确保 src 已加入 sys.path)
from capital_portfolio.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--db", self.db, *argv])
        self.assertEqual(code, 0)
        return buf.getvalue()

    def test_seed_solve_explain_publish_flow(self):
        self.run_cli("seed")
        out = self.run_cli("scenario", "create", "--name", "CLI方案",
                           "--horizon", "2027", "2029", "--by", "评审员")
        sid = json.loads(out)["scenario_id"]

        out = self.run_cli("scenario", "solve", sid)
        self.assertTrue(json.loads(out)["items"])

        out = self.run_cli("scenario", "explain-selection", sid)
        self.assertEqual(json.loads(out)["summary"]["included"]
                         + json.loads(out)["summary"]["excluded"], 8)

        out = self.run_cli("scenario", "explain-deps", sid, "P-001")
        self.assertIn("P-005", json.loads(out)["all_dependents"])

        out = self.run_cli("scenario", "explain-funding", sid)
        self.assertEqual(len(json.loads(out)["years"]), 3)

        self.run_cli("scenario", "submit", sid, "--by", "评审员")
        self.run_cli("scenario", "approve", sid, "--by", "处长")
        out = self.run_cli("scenario", "publish", sid, "--by", "处长")
        bid = json.loads(out)["baseline_id"]

        self.run_cli("actual", "add", bid, "P-001", "2027",
                     "--spend", "10000000", "--mileage", "100")
        out = self.run_cli("baseline", "alerts", bid)
        self.assertTrue(json.loads(out))

        out = self.run_cli("co", "create", bid, "RESCHEDULE",
                           "--payload", '{"project_id": "P-003", "new_start_year": 2029}',
                           "--reason", "同步道路大修", "--by", "评审员")
        co_id = json.loads(out)["change_order_id"]
        self.run_cli("co", "submit", co_id, "--by", "评审员")
        self.run_cli("co", "approve", co_id, "--by", "处长")
        out = self.run_cli("co", "apply", co_id, "--by", "处长")
        self.assertEqual(json.loads(out)["revision"], 2)

    def test_demo_command_runs_end_to_end(self):
        out = self.run_cli("demo")
        for marker in ("入选", "落选", "资金余量", "依赖传播", "预警", "调整单"):
            self.assertIn(marker, out)

    def test_domain_error_exit_code(self):
        self.run_cli("seed")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--db", self.db, "scenario", "submit", "SCN-9999",
                         "--by", "x"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
