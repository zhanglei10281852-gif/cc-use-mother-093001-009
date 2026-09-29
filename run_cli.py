import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))
from capital_portfolio.contracts import BudgetEnvelope, ProjectVersion

budget = BudgetEnvelope(2027, 50000000)
project = ProjectVersion("P-8", 2, ("ASSET-1", "ASSET-2"), 12000000)
print(json.dumps({"project": project.project_id, "version": project.version, "budget_year": budget.year}, ensure_ascii=False))
