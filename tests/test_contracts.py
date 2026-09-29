import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from capital_portfolio.contracts import BudgetEnvelope, ProjectVersion


class PortfolioContractTests(unittest.TestCase):
    def test_project_has_asset_scope(self):
        self.assertEqual(ProjectVersion("P", 1, ("A",), 10).asset_ids, ("A",))

    def test_invalid_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            BudgetEnvelope(2020, 1)


if __name__ == "__main__": unittest.main()
