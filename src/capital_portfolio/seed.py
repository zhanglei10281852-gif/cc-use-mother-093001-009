"""演示数据：覆盖重叠、收益冲突、依赖、互斥与预算约束的典型样例。"""
from __future__ import annotations

from .service import PortfolioService

M = 1_000_000

FUNDING_SOURCES = [
    {"source_id": "F-CENTRAL", "name": "中央专项资金",
     "annual_caps": {y: 6 * M for y in range(2027, 2030)}},
    {"source_id": "F-CITY", "name": "市财政资金",
     "annual_caps": {y: 4 * M for y in range(2027, 2030)}},
    {"source_id": "F-DEBT", "name": "专项债券",
     "annual_caps": {y: 2 * M for y in range(2027, 2030)}},
]

PROJECTS = [
    {"project_id": "P-001", "version": 1, "name": "老城区供水主干管改造",
     "segments": {"SEG-001": 1200, "SEG-002": 800},
     "cost_curve": [8 * M, 8 * M], "earliest_start": 2027,
     "risk_evidence": [{"risk_type": "爆管", "severity": 5, "likelihood": 4,
                        "evidence_ref": "JC-2026-101", "assessed_at": "2026-06-01"}],
     "prerequisites": [], "funding_sources": ["F-CENTRAL", "F-CITY"],
     "benefits": [{"segment_id": "SEG-001", "metric": "leak_reduction_m3", "value": 50000},
                  {"segment_id": "SEG-001", "metric": "people_served", "value": 20000},
                  {"segment_id": "SEG-002", "metric": "leak_reduction_m3", "value": 30000}],
     "exclusive_with": [], "submitted_by": "水务集团"},
    {"project_id": "P-002", "version": 1, "name": "城东污水干管修复",
     "segments": {"SEG-003": 1500}, "cost_curve": [3 * M], "earliest_start": 2027,
     "risk_evidence": [{"risk_type": "腐蚀", "severity": 4, "likelihood": 3,
                        "evidence_ref": "JC-2026-102", "assessed_at": "2026-06-02"}],
     "prerequisites": [], "funding_sources": ["F-DEBT", "F-CITY"],
     "benefits": [{"segment_id": "SEG-003", "metric": "risk_events_avoided", "value": 2}],
     "exclusive_with": [["P-004", "same_year"]], "submitted_by": "排水公司"},
    {"project_id": "P-003", "version": 1, "name": "燃气交叉段改迁",
     "segments": {"SEG-002": 800}, "cost_curve": [2 * M], "earliest_start": 2027,
     "risk_evidence": [{"risk_type": "泄漏", "severity": 4, "likelihood": 4,
                        "evidence_ref": "JC-2026-103", "assessed_at": "2026-06-03"}],
     "prerequisites": [], "funding_sources": ["F-DEBT", "F-CITY"],
     "benefits": [{"segment_id": "SEG-002", "metric": "leak_reduction_m3", "value": 30000}],
     "exclusive_with": [], "submitted_by": "燃气集团"},
    {"project_id": "P-004", "version": 1, "name": "道路开挖同步雨水管改造",
     "segments": {"SEG-004": 600}, "cost_curve": [2 * M, 2 * M], "earliest_start": 2027,
     "risk_evidence": [{"risk_type": "塌陷", "severity": 3, "likelihood": 3,
                        "evidence_ref": "JC-2026-104", "assessed_at": "2026-06-04"}],
     "prerequisites": [], "funding_sources": ["F-CITY"],
     "benefits": [{"segment_id": "SEG-004", "metric": "people_served", "value": 8000}],
     "exclusive_with": [["P-002", "same_year"]], "submitted_by": "市政公司"},
    {"project_id": "P-005", "version": 1, "name": "二次供水设施改造",
     "segments": {"SEG-005": 900}, "cost_curve": [5 * M], "earliest_start": 2028,
     "risk_evidence": [{"risk_type": "水质", "severity": 3, "likelihood": 2,
                        "evidence_ref": "JC-2026-105", "assessed_at": "2026-06-05"}],
     "prerequisites": ["P-001"], "funding_sources": ["F-CITY", "F-CENTRAL"],
     "benefits": [{"segment_id": "SEG-005", "metric": "people_served", "value": 5000}],
     "exclusive_with": [], "submitted_by": "水务集团"},
    {"project_id": "P-006", "version": 1, "name": "管网监测平台建设",
     "segments": {"SEG-006": 100}, "cost_curve": [4 * M, 4 * M], "earliest_start": 2027,
     "risk_evidence": [{"risk_type": "监测缺失", "severity": 2, "likelihood": 3,
                        "evidence_ref": "JC-2026-106", "assessed_at": "2026-06-06"}],
     "prerequisites": [], "funding_sources": ["F-CENTRAL", "F-DEBT"],
     "benefits": [{"segment_id": "SEG-006", "metric": "risk_events_avoided", "value": 1}],
     "exclusive_with": [], "submitted_by": "信息中心"},
    {"project_id": "P-007", "version": 1, "name": "SEG-007 内衬修复方案",
     "segments": {"SEG-007": 700}, "cost_curve": [2_600_000], "earliest_start": 2027,
     "risk_evidence": [{"risk_type": "腐蚀", "severity": 4, "likelihood": 2,
                        "evidence_ref": "JC-2026-107", "assessed_at": "2026-06-07"}],
     "prerequisites": [], "funding_sources": ["F-CENTRAL", "F-DEBT"],
     "benefits": [{"segment_id": "SEG-007", "metric": "leak_reduction_m3", "value": 40000}],
     "exclusive_with": [["P-008", "portfolio"]], "submitted_by": "水务集团"},
    {"project_id": "P-008", "version": 1, "name": "SEG-007 整段更换方案",
     "segments": {"SEG-007": 700}, "cost_curve": [2_500_000], "earliest_start": 2027,
     "risk_evidence": [{"risk_type": "腐蚀", "severity": 4, "likelihood": 2,
                        "evidence_ref": "JC-2026-108", "assessed_at": "2026-06-08"}],
     "prerequisites": [], "funding_sources": ["F-CENTRAL", "F-DEBT"],
     "benefits": [{"segment_id": "SEG-007", "metric": "leak_reduction_m3", "value": 38000}],
     "exclusive_with": [["P-007", "portfolio"]], "submitted_by": "水务集团"},
]


def seed(service: PortfolioService) -> None:
    """写入演示资金来源与项目版本。"""
    for src in FUNDING_SOURCES:
        service.upsert_funding_source(src)
    for p in PROJECTS:
        service.submit_project_version(p)
