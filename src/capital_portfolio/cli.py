"""命令行接口：与 HTTP API 等价的投资组合治理操作入口。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capital_portfolio.domain import DomainError  # noqa: E402
from capital_portfolio.seed import seed  # noqa: E402
from capital_portfolio.service import PortfolioService  # noqa: E402
from capital_portfolio.store import Store  # noqa: E402


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _service(args) -> PortfolioService:
    return PortfolioService(Store(args.db))


def _load_json(path_or_inline: str) -> dict:
    p = Path(path_or_inline)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(path_or_inline)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfolio",
                                     description="管网改造投资组合治理命令行")
    parser.add_argument("--db", default="portfolio.db", help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="写入演示数据")
    sub.add_parser("demo", help="端到端演示")
    sub.add_parser("serve", help="启动 HTTP 服务")

    p = sub.add_parser("project", help="项目版本管理")
    ps = p.add_subparsers(dest="action", required=True)
    x = ps.add_parser("submit"); x.add_argument("--file", required=True)
    ps.add_parser("list")
    x = ps.add_parser("versions"); x.add_argument("project_id")

    p = sub.add_parser("funding", help="资金来源管理")
    ps = p.add_subparsers(dest="action", required=True)
    x = ps.add_parser("upsert"); x.add_argument("--file", required=True)
    ps.add_parser("list")

    p = sub.add_parser("scenario", help="情景(候选方案)管理")
    ps = p.add_subparsers(dest="action", required=True)
    x = ps.add_parser("create")
    x.add_argument("--name", required=True)
    x.add_argument("--horizon", nargs=2, type=int, required=True, metavar=("起年", "止年"))
    x.add_argument("--by", required=True); x.add_argument("--valid-days", type=int, default=90)
    x.add_argument("--config", default=None, help="求解参数 JSON(内联或文件)")
    ps.add_parser("list")
    x = ps.add_parser("show"); x.add_argument("id")
    x = ps.add_parser("solve"); x.add_argument("id")
    x = ps.add_parser("lock"); x.add_argument("id")
    x.add_argument("--kind", required=True,
                   choices=["INCLUDE", "EXCLUDE", "PIN_START", "CAP_OVERRIDE", "PRIORITY"])
    x.add_argument("--project", default=None)
    x.add_argument("--value", default="{}", help="假设参数 JSON")
    x.add_argument("--by", required=True); x.add_argument("--note", default="")
    x = ps.add_parser("unlock"); x.add_argument("id"); x.add_argument("assumption_id")
    for action in ("submit", "approve", "reject", "refresh", "publish"):
        x = ps.add_parser(action); x.add_argument("id")
        x.add_argument("--by", required=True); x.add_argument("--note", default="")
    x = ps.add_parser("explain-selection"); x.add_argument("id")
    x = ps.add_parser("explain-deps"); x.add_argument("id"); x.add_argument("project_id")
    x = ps.add_parser("explain-funding"); x.add_argument("id")

    x = sub.add_parser("compare", help="比较两个情景的可复核差异")
    x.add_argument("a"); x.add_argument("b")

    p = sub.add_parser("baseline", help="基线管理")
    ps = p.add_subparsers(dest="action", required=True)
    ps.add_parser("current")
    x = ps.add_parser("show"); x.add_argument("id")
    x = ps.add_parser("deviation"); x.add_argument("id")
    x = ps.add_parser("alerts"); x.add_argument("id")
    x = ps.add_parser("actuals"); x.add_argument("id"); x.add_argument("--project", default=None)

    p = sub.add_parser("actual", help="实际支出与里程回填")
    ps = p.add_subparsers(dest="action", required=True)
    x = ps.add_parser("add")
    x.add_argument("baseline_id"); x.add_argument("project_id"); x.add_argument("period")
    x.add_argument("--spend", type=int, required=True)
    x.add_argument("--mileage", type=float, required=True)
    x.add_argument("--source", default="manual"); x.add_argument("--note", default="")

    p = sub.add_parser("co", help="基线调整单")
    ps = p.add_subparsers(dest="action", required=True)
    x = ps.add_parser("create")
    x.add_argument("baseline_id")
    x.add_argument("type",
                   choices=["RESCHEDULE", "REPLACE_VERSION", "ADD_ITEM",
                            "REMOVE_ITEM", "REALLOCATE"])
    x.add_argument("--payload", required=True, help="调整内容 JSON")
    x.add_argument("--reason", required=True); x.add_argument("--by", required=True)
    x = ps.add_parser("list"); x.add_argument("baseline_id")
    for action in ("submit", "approve", "reject", "apply"):
        x = ps.add_parser(action); x.add_argument("id")
        x.add_argument("--by", required=True); x.add_argument("--note", default="")

    x = sub.add_parser("audit", help="审计日志")
    x.add_argument("--entity", default=None)
    return parser


def run_demo(svc: PortfolioService) -> None:
    """端到端演示：申报 -> 求解 -> 假设 -> 比较 -> 审批发布 -> 回填预警 -> 调整单。"""
    seed(svc)
    print("== 1. 演示数据已写入(3 个资金来源, 8 个项目) ==")

    scn = svc.create_scenario("2027-2029 候选方案 A", (2027, 2029), "评审员甲")
    sid = scn.scenario_id
    svc.solve_scenario(sid)
    sel = svc.explain_selection(sid)
    print(f"\n== 2. 情景 {sid} 求解: 入选 {sel['summary']['included']}, "
          f"落选 {sel['summary']['excluded']} ==")
    for row in sel["included"]:
        print(f"  [入选] {row['project_id']}: {row['reason']}")
    for row in sel["excluded"]:
        print(f"  [落选] {row['project_id']}: {row['reason']}")
    print("  范围重叠与收益冲突:")
    for o in sel["overlaps"]:
        print(f"    - {o['message']}")

    print(f"\n== 3. 资金余量 ==")
    for y in svc.explain_funding(sid)["years"]:
        line = ", ".join(f"{s['source_id']} 余 {s['margin']:,}" for s in y["sources"])
        print(f"  {y['year']}: {line}")

    dep = svc.explain_dependencies(sid, "P-001")
    print(f"\n== 4. 依赖传播(P-001 延后一年) ==")
    for a in dep["if_delayed_one_year"]["affected_projects"]:
        print(f"  {a['project_id']}: {a['original_start']} -> {a['required_start']}")

    locked = svc.add_assumption(sid, "PRIORITY", "评审员甲", project_id="P-006",
                                value={"multiplier": 5.0}, note="监测平台纳入城市更新重点")
    assumption_id = locked.assumptions[-1].assumption_id
    svc.solve_scenario(sid)
    sel2 = svc.explain_selection(sid)
    print(f"\n== 5. 锁定 P-006 优先级倍率 5 后重新求解 ==")
    for row in sel2["included"]:
        if row["project_id"] == "P-006":
            print(f"  [入选] P-006: {row['reason']}")
    dropped = [r for r in sel2["excluded"] if r["project_id"] in ("P-001", "P-005")]
    for row in dropped:
        print(f"  [连带影响] {row['project_id']}: {row['reason']}")

    svc.remove_assumption(sid, assumption_id)
    svc.solve_scenario(sid)
    print(f"\n== 6. 解除该假设并重新求解, 恢复原方案 ==")

    scn_b = svc.create_scenario("2027-2029 候选方案 B(禁止重叠)", (2027, 2029),
                                "评审员乙", config={"overlap_policy": "forbid"})
    svc.solve_scenario(scn_b.scenario_id)
    diff = svc.compare_scenarios(sid, scn_b.scenario_id)
    print(f"\n== 7. 情景差异 A({sid}) vs B({scn_b.scenario_id}) ==")
    print(f"  B 新增: {diff['items_added']}, B 移除: {diff['items_removed']}")
    print(f"  收益差异: {diff['benefit_delta']['from']:,.0f} -> "
          f"{diff['benefit_delta']['to']:,.0f}")

    svc.submit_scenario(sid, "评审员甲")
    svc.approve_scenario(sid, "处长")
    baseline = svc.publish_scenario(sid, "处长")
    bid = baseline.baseline_id
    print(f"\n== 8. 情景 {sid} 已发布为基线 {bid} ==")

    svc.backfill_actual(bid, "P-001", "2027", 10_500_000, 900, source="计量系统")
    print("\n== 9. 回填 P-001 2027 年实际(支出 1050 万, 里程 900 米)后预警 ==")
    for a in svc.list_alerts(bid):
        print(f"  [{a['severity']}] {a['message']}")

    co = svc.create_change_order(bid, "RESCHEDULE",
                                 {"project_id": "P-003", "new_start_year": 2029},
                                 "燃气管迁改与道路大修同步", "评审员甲")
    svc.submit_change_order(co.change_order_id, "评审员甲")
    svc.approve_change_order(co.change_order_id, "处长")
    svc.apply_change_order(co.change_order_id, "处长")
    print(f"\n== 10. 调整单 {co.change_order_id} 已应用, 基线修订 "
          f"{svc.get_baseline(bid)['revision']} ==")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn
        from capital_portfolio.api import create_app
        uvicorn.run(create_app(args.db), host="127.0.0.1", port=8000)
        return 0

    svc = _service(args)
    try:
        if args.command == "seed":
            seed(svc)
            _print({"seeded": True, "db": args.db})
        elif args.command == "demo":
            run_demo(svc)
        elif args.command == "project":
            if args.action == "submit":
                _print(svc.submit_project_version(_load_json(args.file)).to_dict())
            elif args.action == "list":
                _print(svc.list_projects())
            else:
                _print(svc.get_project_versions(args.project_id))
        elif args.command == "funding":
            if args.action == "upsert":
                _print(svc.upsert_funding_source(_load_json(args.file)).to_dict())
            else:
                _print(svc.list_funding_sources())
        elif args.command == "scenario":
            _dispatch_scenario(svc, args)
        elif args.command == "compare":
            _print(svc.compare_scenarios(args.a, args.b))
        elif args.command == "baseline":
            _dispatch_baseline(svc, args)
        elif args.command == "actual":
            _print(svc.backfill_actual(args.baseline_id, args.project_id, args.period,
                                       args.spend, args.mileage, args.source,
                                       args.note).to_dict())
        elif args.command == "co":
            _dispatch_co(svc, args)
        elif args.command == "audit":
            _print(svc.audit_trail(args.entity))
    except DomainError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0


def _dispatch_scenario(svc: PortfolioService, args) -> None:
    a = args.action
    if a == "create":
        config = _load_json(args.config) if args.config else None
        _print(svc.create_scenario(args.name, tuple(args.horizon), args.by,
                                   config=config, valid_days=args.valid_days).to_dict())
    elif a == "list":
        _print(svc.list_scenarios())
    elif a == "show":
        _print(svc.get_scenario(args.id))
    elif a == "solve":
        _print(svc.solve_scenario(args.id).to_dict())
    elif a == "lock":
        _print(svc.add_assumption(args.id, args.kind, args.by, project_id=args.project,
                                  value=json.loads(args.value), note=args.note).to_dict())
    elif a == "unlock":
        _print(svc.remove_assumption(args.id, args.assumption_id).to_dict())
    elif a == "submit":
        _print(svc.submit_scenario(args.id, args.by).to_dict())
    elif a == "approve":
        _print(svc.approve_scenario(args.id, args.by, args.note).to_dict())
    elif a == "reject":
        _print(svc.reject_scenario(args.id, args.by, args.note).to_dict())
    elif a == "refresh":
        _print(svc.refresh_scenario(args.id, args.by).to_dict())
    elif a == "publish":
        _print(svc.publish_scenario(args.id, args.by).to_dict())
    elif a == "explain-selection":
        _print(svc.explain_selection(args.id))
    elif a == "explain-deps":
        _print(svc.explain_dependencies(args.id, args.project_id))
    elif a == "explain-funding":
        _print(svc.explain_funding(args.id))


def _dispatch_baseline(svc: PortfolioService, args) -> None:
    a = args.action
    if a == "current":
        _print(svc.current_baseline() or {})
    elif a == "show":
        _print(svc.get_baseline(args.id))
    elif a == "deviation":
        _print(svc.deviation_report(args.id))
    elif a == "alerts":
        _print(svc.list_alerts(args.id))
    elif a == "actuals":
        _print(svc.list_actuals(args.id, args.project))


def _dispatch_co(svc: PortfolioService, args) -> None:
    a = args.action
    if a == "create":
        _print(svc.create_change_order(args.baseline_id, args.type,
                                       json.loads(args.payload), args.reason,
                                       args.by).to_dict())
    elif a == "list":
        _print(svc.list_change_orders(args.baseline_id))
    elif a == "submit":
        _print(svc.submit_change_order(args.id, args.by).to_dict())
    elif a == "approve":
        _print(svc.approve_change_order(args.id, args.by, args.note).to_dict())
    elif a == "reject":
        _print(svc.reject_change_order(args.id, args.by, args.note).to_dict())
    elif a == "apply":
        _print(svc.apply_change_order(args.id, args.by).to_dict())


if __name__ == "__main__":
    raise SystemExit(main())
