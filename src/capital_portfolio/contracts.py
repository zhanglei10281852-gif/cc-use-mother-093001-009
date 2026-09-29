"""投资项目和年度预算的基础契约。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class BudgetEnvelope:
    year: int
    amount: int

    def __post_init__(self) -> None:
        if self.year < 2026 or self.amount < 0:
            raise ValueError("预算年度或金额无效")


@dataclass(frozen=True)
class ProjectVersion:
    project_id: str
    version: int
    asset_ids: tuple[str, ...]
    estimated_cost: int

    def __post_init__(self) -> None:
        if self.version < 1 or not self.asset_ids or self.estimated_cost <= 0:
            raise ValueError("项目版本信息不完整")
