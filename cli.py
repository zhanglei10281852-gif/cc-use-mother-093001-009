"""改造投资组合治理服务命令行。

无参数运行内置演示全流程；子命令支持重叠诊断、情景计算与调整、
可复核解释、审批发布、正式调整单与滚动实际值回填。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from capital_portfolio.governance import GovernanceError  # noqa: E402
from capital_portfolio.persistence import load_state, save_state  # noqa: E402
from capital_portfolio.sample_data import build_service  # noqa: E402
from capital_portfolio.service import to_jsonable  # noqa: E402

DEFAULT_STATE = ".portfolio_state.json"
MUTATING_COMMANDS = frozenset({
    "priority", "weight", "lock", "exclude", "unlock", "submit", "publish",
    "adjust", "backfill",
})


def _emit(obj, as_json: bool, text: str = "") -> None:
    if as_json:
        print(json.dumps(to_jsonable(obj), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(text if text else json.dumps(
            to_jsonable(obj), ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portfolio", description="管网改造投资组合治理服务")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--expired-demo", action="store_true",
                        help="内置数据中使协同情景已过期（用于演示过期拒绝）")
    parser.add_argument("--state", default=DEFAULT_STATE,
                        help=f"治理状态文件（默认 {DEFAULT_STATE}）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("overlaps", help="识别范围重叠与重复申报收益")
    sub.add_parser("deps", help="依赖环、互斥组与依赖传播图")

    p = sub.add_parser("compute", help="计算情景的跨年度候选方案")
    p.add_argument("scenario")

    p = sub.add_parser("overview", help="情景总览（入选/落选/余量）")
    p.add_argument("scenario")

    p = sub.add_parser("explain", help="解释某个项目入选或落选的决定")
    p.add_argument("scenario")
    p.add_argument("project")

    p = sub.add_parser("propagation", help="解释延后项目导致的配套失效传播")
    p.add_argument("scenario")

    p = sub.add_parser("headroom", help="年度预算与资金来源余量")
    p.add_argument("scenario")

    p = sub.add_parser("compare", help="比较两个情景的可复核差异")
    p.add_argument("left")
    p.add_argument("right")

    p = sub.add_parser("priority", help="调整项目人工优先级加分（0 清除）")
    p.add_argument("scenario")
    p.add_argument("project")
    p.add_argument("boost", type=float)

    p = sub.add_parser("weight", help="调整风险/配套带动权重")
    p.add_argument("scenario")
    p.add_argument("field", choices=["risk_value_per_point",
                                     "enablement_bonus_per_dependent"])
    p.add_argument("value", type=float)

    p = sub.add_parser("lock", help="锁定入选（可指定开工年）")
    p.add_argument("scenario")
    p.add_argument("project")
    p.add_argument("year", nargs="?", type=int, default=None)

    p = sub.add_parser("exclude", help="锁定排除")
    p.add_argument("scenario")
    p.add_argument("project")

    p = sub.add_parser("unlock", help="解除锁定假设")
    p.add_argument("scenario")
    p.add_argument("project")

    p = sub.add_parser("submit", help="提交情景审批")
    p.add_argument("scenario")

    p = sub.add_parser("publish", help="把已提交情景发布为审批基线")
    p.add_argument("scenario")
    p.add_argument("baseline")
    p.add_argument("--date", dest="approved_on", default=None)

    p = sub.add_parser("baseline", help="查看基线（含版本与调整记录）")
    p.add_argument("baseline")

    p = sub.add_parser("adjust", help="编制/提交/应用正式调整单")
    p.add_argument("baseline")
    p.add_argument("--add", action="append", default=[], metavar="PID:YEAR")
    p.add_argument("--remove", action="append", default=[], metavar="PID")
    p.add_argument("--retime", action="append", default=[], metavar="PID:YEAR")
    p.add_argument("--rationale", default="")
    p.add_argument("--apply", action="store_true", help="提交并审批应用，产生新版本")

    p = sub.add_parser("backfill", help="滚动回填实际支出与里程并触发预警")
    p.add_argument("project")
    p.add_argument("year", type=int)
    p.add_argument("spend", type=int)
    p.add_argument("mileage", type=float)
    p.add_argument("--baseline", dest="baseline_id", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    svc = build_service(expired=getattr(args, "expired_demo", False))

    is_demo = args.command is None
    if not is_demo:
        try:
            load_state(svc, args.state)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            print(f"无法读取状态文件 {args.state}：{exc}", file=sys.stderr)
            return 2

    try:
        if args.command is None:
            return run_demo(svc, args.json, args.expired_demo)

        cmd = args.command
        if cmd == "overlaps":
            report = svc.overlap_report()
            text = ["范围重叠与收益冲突："]
            for o in report:
                text.append(
                    f"- 管段 {o['asset_id']} 被 {o['project_ids']} 重复申报；"
                    f"各项目申报 {o['claimed_by_project']}，"
                    f"合计 {o['claimed_total']}，重复计算 {o['duplicated_benefit']}")
            total_dup = sum(o["duplicated_benefit"] for o in report)
            text.append(f"重复申报收益合计：{total_dup}")
            _emit(report, args.json, "\n".join(text))

        elif cmd == "deps":
            report = svc.dependency_report()
            text = ["依赖治理诊断：",
                    f"- 依赖环：{report['cycles'] or '无'}",
                    f"- 互斥施工组：{report['mutex_groups'] or '无'}",
                    "- 上游落选 → 下游配套传播图："]
            for pid, children in sorted(report["propagation_map"].items()):
                text.append(f"    {pid} → {children}")
            _emit(report, args.json, "\n".join(text))

        elif cmd == "compute":
            plan = svc.compute(args.scenario)
            _emit(svc.overview(args.scenario), args.json,
                  _plan_text(svc, args.scenario, plan))

        elif cmd == "overview":
            if svc.scenarios[args.scenario].plan is None:
                svc.compute(args.scenario)
            _emit(svc.overview(args.scenario), args.json,
                  _plan_text(svc, args.scenario, svc.scenarios[args.scenario].plan))

        elif cmd == "explain":
            print(svc.explain_decision(args.scenario, args.project))

        elif cmd == "propagation":
            print(svc.explain_propagation(args.scenario))

        elif cmd == "headroom":
            print(svc.explain_headroom(args.scenario))

        elif cmd == "compare":
            for sid in (args.left, args.right):
                if svc.scenarios[sid].plan is None:
                    svc.compute(sid)
            from capital_portfolio.governance import compare_scenarios
            print(compare_scenarios(
                svc.scenarios[args.left], svc.scenarios[args.right]).as_text())

        elif cmd == "priority":
            svc.scenarios[args.scenario].set_priority_boost(
                args.project, args.boost)
            print(f"已设置 {args.project} 人工优先级加分为 {args.boost}（情景未重算，请运行 compute）")

        elif cmd == "weight":
            svc.scenarios[args.scenario].set_weight(args.field, args.value)
            print(f"已调整权重 {args.field}={args.value}（情景未重算，请运行 compute）")

        elif cmd == "lock":
            svc.scenarios[args.scenario].lock_in(args.project, args.year)
            print(f"已锁定 {args.project} 入选"
                  + (f"，开工年 {args.year}" if args.year else "（年度不锁）"))

        elif cmd == "exclude":
            svc.scenarios[args.scenario].lock_exclusion(args.project)
            print(f"已锁定排除 {args.project}")

        elif cmd == "unlock":
            svc.scenarios[args.scenario].unlock(args.project)
            print(f"已解除 {args.project} 的锁定假设")

        elif cmd == "submit":
            if svc.scenarios[args.scenario].plan is None:
                svc.compute(args.scenario)
            svc.submit_scenario(args.scenario)
            print(f"情景 {args.scenario} 已提交审批")

        elif cmd == "publish":
            if svc.scenarios[args.scenario].plan is None:
                svc.compute(args.scenario)
            if svc.scenarios[args.scenario].status == "draft":
                svc.submit_scenario(args.scenario)
            baseline = svc.publish(args.scenario, args.baseline, args.approved_on)
            print(f"基线 {baseline.baseline_id} 第 {baseline.revision} 版已发布，"
                  f"在册项目 {sorted(baseline.schedule)}")

        elif cmd == "baseline":
            _emit(svc.baseline_view(args.baseline), args.json,
                  _baseline_text(svc.baseline_view(args.baseline)))

        elif cmd == "adjust":
            items = []
            for spec in args.add:
                pid, year = spec.split(":")
                items.append({"action": "add", "project_id": pid,
                              "start_year": int(year)})
            for pid in args.remove:
                items.append({"action": "remove", "project_id": pid})
            for spec in args.retime:
                pid, year = spec.split(":")
                items.append({"action": "retime", "project_id": pid,
                              "start_year": int(year)})
            result = svc.create_adjustment(
                args.baseline, f"ADJ-{len(svc.baselines[args.baseline].adjustments)+1:03d}",
                items, rationale=args.rationale, apply=args.apply)
            if args.apply:
                print(f"调整单已审批应用，基线更新至第 {result['new_revision']} 版；"
                      f"级联移除：{result['preview']['cascade_removed'] or '无'}")
            else:
                print("调整单一致性预演通过（未提交）：")
                print(json.dumps(to_jsonable(result["preview"]),
                                 ensure_ascii=False, indent=2))

        elif cmd == "backfill":
            alerts = svc.backfill(args.project, args.year, args.spend,
                                  args.mileage, args.baseline_id)
            if alerts:
                for a in alerts:
                    print(f"[{a['level']}/{a['kind']}] {a['message']}")
            else:
                print("回填完成，未触发偏差预警")

        if cmd in MUTATING_COMMANDS:
            save_state(svc, args.state)
        return 0
    except (GovernanceError, ) as exc:
        print(f"治理规则拒绝：{exc}", file=sys.stderr)
        for v in getattr(exc, "violations", []):
            print(f"  - {v}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"对象不存在：{exc}", file=sys.stderr)
        return 2


def _plan_text(svc, scenario_id, plan) -> str:
    lines = [f"情景 {scenario_id}（{svc.scenarios[scenario_id].assumptions.name}）"
             f"候选方案 horizon {plan.horizon[0]}–{plan.horizon[1]}"]
    lines.append("入选：")
    for d in plan.selected:
        lines.append(
            f"  {d.project_id}  {d.start_year}-{d.end_year}  "
            f"评分 {d.score:.0f}  认可收益 {d.recognized_benefit}  "
            f"年度成本 {d.cost_by_year}")
    lines.append("落选/延后：")
    for d in plan.deferred:
        lines.append(f"  {d.project_id}  [{d.status}] {d.reason_codes}")
        for r in d.reasons[:2]:
            lines.append(f"      {r}")
        if d.blocker_chain:
            lines.append(f"      传播链：{' → '.join(d.blocker_chain)}")
    lines.append(f"消除重复申报收益：{plan.duplicated_benefit_eliminated}")
    lines.append(svc.explain_headroom(scenario_id))
    return "\n".join(lines)


def _baseline_text(view: dict) -> str:
    lines = [
        f"基线 {view['baseline_id']} 第 {view['revision']} 版"
        f"（{view['approved_on']} 审批自情景 {view['scenario_id']}）",
        f"排程：{view['schedule']}",
        f"项目版本：{view['project_versions']}",
        f"年度预算余量：{view['budget_remaining']}",
        f"资金来源余量：{view['funding_remaining']}",
        f"调整记录：{len(view['adjustments'])} 份",
    ]
    for adj in view["adjustments"]:
        lines.append(
            f"  - {adj['order_id']}：v{adj['from_revision']}→v{adj['to_revision']} "
            f"{adj['applied_on']}；明细 {adj['items']}；级联 {adj['cascade_removed']}")
    return "\n".join(lines)


def run_demo(svc, as_json: bool, expired: bool) -> int:
    """无参数引导式演示：完整走一遍治理闭环。"""
    out: dict = {"steps": []}

    def log(title, payload=None, text=None):
        out["steps"].append({"title": title, "payload": payload or text})
        print(f"\n{'='*72}\n{title}\n{'='*72}")
        if text:
            print(text)

    log("1) 范围重叠与重复申报收益",
        text="\n".join(
            f"- 管段 {o['asset_id']} 被 {o['project_ids']} 申报，"
            f"重复计算收益 {o['duplicated_benefit']}"
            for o in svc.overlap_report())
        + f"\n重复申报合计：{sum(o['duplicated_benefit'] for o in svc.overlap_report())}")
    log("2) 依赖与互斥诊断", text=_deps_text(svc))

    base = svc.compute("S-BASE")
    urban = svc.scenarios["S-URBAN"]
    if urban.assumptions.is_expired():
        # 过期情景不允许治理动作；但只读分析视图仍可按其锁定假设计算用于对比。
        from capital_portfolio.engine import build_plan
        urban.plan = build_plan(
            svc.latest_projects().values(), svc.budgets, svc.pools,
            weights=urban.assumptions.weights,
            forced_in=urban.assumptions.forced_in,
            excluded=urban.assumptions.excluded)
    else:
        svc.compute("S-URBAN")
    log("3) 基线权衡情景候选方案", text=_plan_text(svc, "S-BASE", base))
    log("4) 落选决定可复核解释（P-103）",
        text=svc.explain_decision("S-BASE", "P-103"))
    log("5) 资金与预算余量", text=svc.explain_headroom("S-BASE"))

    from capital_portfolio.governance import compare_scenarios
    log("6) 情景差异（S-BASE → S-URBAN）",
        text=compare_scenarios(svc.scenarios["S-BASE"],
                               svc.scenarios["S-URBAN"]).as_text())

    svc.submit_scenario("S-BASE")
    baseline = svc.publish("S-BASE", "BL-2027")
    log("7) 审批发布基线",
        text=f"BL-2027 第 {baseline.revision} 版：{sorted(baseline.schedule)}")

    alerts = (svc.backfill("P-100", 2027, 34_000_000, 2500.0)
              if "P-100" in baseline.schedule else [])
    log("8) 滚动回填 P-100 2027 实际值（超概算 + 里程滞后）",
        text="\n".join(f"[{a['level']}/{a['kind']}] {a['message']}" for a in alerts)
             or "无预警")

    bad_items = [{"action": "add", "project_id": "P-107", "start_year": 2027}]
    try:
        svc.create_adjustment("BL-2027", "ADJ-BAD", bad_items,
                              rationale="应当被拒绝：超期且无预算")
        log("9) 非法调整单（意外通过）", text="不应发生")
    except GovernanceError as exc:
        log("9) 正式调整单闸门拒绝非法变更",
            text=f"ADJ-BAD 被拒：{exc}\n" +
                 "\n".join(f"  - {v}" for v in exc.violations))

    valid = [
        {"action": "remove", "project_id": "P-101",
         "reason": "雨污分流规划条件变化，年度暂缓"},
        {"action": "add", "project_id": "P-106", "start_year": 2028,
         "reason": "释放专项债与 2028 年度窗口，物联感知提前补位"},
    ]
    result = svc.create_adjustment(
        "BL-2027", "ADJ-001", valid,
        rationale="P-101 暂缓后其下游 P-104 无前置而级联失效；P-106 提至 2028 补位",
        apply=True)
    log("10) 正式调整单 ADJ-001 通过并产生基线 v2",
        text=f"新版本：v{result['new_revision']}；级联移除："
             f"{result['preview']['cascade_removed']}\n"
             f"调整后排程：{result['preview']['schedule']}")
    log("11) 基线视图", text=_baseline_text(svc.baseline_view("BL-2027")))

    if expired:
        try:
            svc.submit_scenario("S-URBAN")
        except GovernanceError as exc:
            log("12) 过期情景不能提交/发布", text=f"被拒：{exc}")
    else:
        log("12) 过期情景治理",
            text="当前演示数据未过期；使用 --expired-demo 可验证过期情景一律拒绝提交与发布。")

    if as_json:
        print(json.dumps(to_jsonable(out), ensure_ascii=False, indent=2))
    return 0


def _deps_text(svc) -> str:
    report = svc.dependency_report()
    lines = [f"依赖环：{report['cycles'] or '无'}",
             f"互斥施工组：{report['mutex_groups']}"]
    for pid, children in sorted(report["propagation_map"].items()):
        lines.append(f"  {pid} 延后将连带：{children}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
