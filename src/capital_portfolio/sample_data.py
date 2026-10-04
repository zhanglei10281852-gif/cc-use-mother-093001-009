"""内置演示数据：市级管网改造项目目录。

故意构造三类治理难点：
- P-101/P-102/P-103 在 S-04 共同管段重复申报收益；P-106 与所有项目范围重叠；
- P-100 → P-101 → P-104 → P-105 构成配套依赖链，上游延后即级联失效；
- P-102 与 P-103 同属道路开挖互斥组；P-107 远期项目超出预算 horizon。
"""
from __future__ import annotations

from .contracts import (
    BudgetEnvelope,
    Dependency,
    FundingCommitment,
    FundingPool,
    ProjectVersion,
    RiskEvidence,
)
from .service import PortfolioService


def build_service(*, expired: bool = False) -> PortfolioService:
    svc = PortfolioService()

    for envelope in (
        BudgetEnvelope(2027, 60_000_000),
        BudgetEnvelope(2028, 55_000_000),
        BudgetEnvelope(2029, 40_000_000),
    ):
        svc.set_budget(envelope)

    # 专项债为跨年度总池；一般财政按年度设池。
    for pool in (
        FundingPool("专项债", 90_000_000),
        FundingPool("一般财政", 30_000_000, 2027),
        FundingPool("一般财政", 20_000_000, 2028),
        FundingPool("一般财政", 20_000_000, 2029),
    ):
        svc.add_funding_pool(pool)

    projects = [
        ProjectVersion(
            "P-100", 1, ("S-01", "S-02"), 30_000_000,
            name="沿江干管结构性修复",
            base_year=2027, mileage=3000.0,
            risk_evidence=(
                RiskEvidence("R-01", "结构缺陷", 5, "CCTV 2026Q2 检测", "2026-05-10"),
                RiskEvidence("R-02", "渗漏", 3, "养护巡检", "2026-06-02"),
            ),
            funding_sources=(
                FundingCommitment("专项债", 0.6),
                FundingCommitment("一般财政", 0.4),
            ),
            benefit_by_asset={"S-01": 12_000_000, "S-02": 10_000_000},
        ),
        ProjectVersion(
            "P-101", 1, ("S-02", "S-03", "S-04"), 50_000_000,
            name="老城片区雨污分流",
            base_year=2027, mileage=4200.0,
            depends_on=(Dependency("P-100"),),
            risk_evidence=(RiskEvidence("R-03", "雨污混接", 4, "排查台账", "2026-04-20"),),
            funding_sources=(FundingCommitment("专项债", 1.0),),
            benefit_by_asset={"S-02": 8_000_000, "S-03": 9_000_000, "S-04": 11_000_000},
        ),
        ProjectVersion(
            "P-102", 1, ("S-04", "S-05"), 18_000_000,
            name="解放西路内涝点整治",
            base_year=2027, mileage=1400.0,
            risk_evidence=(
                RiskEvidence("R-04", "内涝淹水", 4, "2025 汛期三次积淹", "2025-08-15"),
                RiskEvidence("R-05", "民生投诉", 3, "12345 热线聚类", "2026-03-01"),
            ),
            funding_sources=(FundingCommitment("一般财政", 1.0),),
            mutex_groups=("ROAD-JF-WEST-2027",),
            benefit_by_asset={"S-04": 14_000_000, "S-05": 6_000_000},
        ),
        ProjectVersion(
            "P-103", 2, ("S-04", "S-06"), 22_000_000,
            name="解放西路道路地下一体化",
            base_year=2027, mileage=1700.0,
            risk_evidence=(RiskEvidence("R-06", "道路协同", 2, "城市更新计划", "2026-02-10"),),
            funding_sources=(
                FundingCommitment("专项债", 0.5),
                FundingCommitment("一般财政", 0.5),
            ),
            mutex_groups=("ROAD-JF-WEST-2027",),
            benefit_by_asset={"S-04": 7_000_000, "S-06": 5_000_000},
        ),
        ProjectVersion(
            "P-104", 1, ("S-07", "S-08"), 22_000_000,
            name="北区老旧支管更换",
            base_year=2027, mileage=2600.0,
            depends_on=(Dependency("P-101"),),
            risk_evidence=(RiskEvidence("R-07", "管龄超期", 3, "资产台账", "2026-01-15"),),
            funding_sources=(FundingCommitment("专项债", 1.0),),
            benefit_by_asset={"S-07": 8_000_000, "S-08": 7_000_000},
        ),
        ProjectVersion(
            "P-105", 1, ("S-09",), 15_000_000,
            name="北泵站提升配套",
            base_year=2027, mileage=900.0,
            depends_on=(Dependency("P-104", lag_years=1),),
            risk_evidence=(RiskEvidence("R-08", "抽排能力不足", 3, "运行记录", "2026-05-30"),),
            funding_sources=(FundingCommitment("一般财政", 1.0),),
            benefit_by_asset={"S-09": 4_000_000},
        ),
        ProjectVersion(
            "P-106", 1,
            ("S-01", "S-02", "S-03", "S-04", "S-05", "S-06", "S-07", "S-08", "S-09"),
            8_000_000,
            name="干线在线监测物联感知",
            base_year=2027, mileage=0.0,
            risk_evidence=(RiskEvidence("R-09", "运维盲区", 2, "数字化规划", "2026-03-18"),),
            funding_sources=(FundingCommitment("专项债", 1.0),),
            benefit_by_asset={},
        ),
        ProjectVersion(
            "P-107", 1, ("S-10",), 40_000_000,
            name="新区延伸干管（远期）",
            base_year=2030, mileage=3800.0,
            risk_evidence=(RiskEvidence("R-10", "规划覆盖", 2, "国土空间规划", "2026-06-01"),),
            funding_sources=(FundingCommitment("专项债", 1.0),),
            benefit_by_asset={"S-10": 20_000_000},
        ),
    ]
    for pv in projects:
        svc.register_project(pv)

    # 两个评审情景：基线权衡 / 城市更新协同（道路一体化优先）。
    svc.new_scenario(
        "S-BASE", "基线权衡情景",
        expires_on="2027-03-31",
        notes="默认权重：申报收益 + 风险严重度 + 配套带动。")
    svc.new_scenario(
        "S-URBAN", "城市更新协同情景",
        expires_on="2027-03-31",
        notes="锁定道路地下一体化 P-103 入选，排除内涝点 P-102。",
        clone_from="S-BASE")
    urban = svc.scenarios["S-URBAN"]
    urban.lock_in("P-103", 2027)
    urban.lock_exclusion("P-102")
    if expired:
        # 锁定动作完成后再置为过期，模拟评审窗口已关闭的历史情景。
        urban.assumptions.expires_on = "2026-09-30"

    return svc
