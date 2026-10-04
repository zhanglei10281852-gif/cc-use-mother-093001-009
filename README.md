# 管网改造投资组合治理服务

面向市级投资评审处的跨年度改造投资组合治理：接收带资产范围、成本曲线、风险证据、
前置依赖与资金来源的项目版本，自动识别范围重叠与重复申报收益，形成跨年度候选方案；
评审人可锁定假设、调整优先级、比较情景并提交审批；审批基线只通过正式调整单变更，
实际支出与里程完成量滚动回填并触发偏差预警。所有入选/落选决定、依赖传播、资金余量
与情景差异均可复核解释。

## 领域模型（`src/capital_portfolio/`）

| 文件 | 职责 |
|---|---|
| `contracts.py` | `ProjectVersion`（资产范围/成本曲线/风险证据/依赖/资金来源/互斥组/按管段收益）、`BudgetEnvelope`、`FundingPool`、`RiskEvidence`、`Dependency`、`FundingCommitment` |
| `analysis.py` | 按管段识别范围重叠与重复收益、收益按优先级只归属一次、依赖图/环检测/反向传播闭包、互斥组归集 |
| `engine.py` | 跨年度规划：优先级评分（申报收益+风险严重度+配套带动+人工调整）、最早可行窗口搜索、预算/跨年度资金池/共同管段同年施工/互斥施工/前置滞后/horizon 一致性约束、逐项决定解释与级联失效传播链 |
| `governance.py` | 情景生命周期（草稿→提交→批准/驳回/过期）、锁定假设冲突拒绝、情景差异 `ScenarioDiff`、发布基线、正式调整单（新增/移除/重排 + 级联移除预演 + 版本号闸门）、滚动实际值与偏差预警 |
| `service.py` | 服务编排层与 JSON 可序列化输出，供 CLI 及未来 HTTP 适配复用 |
| `persistence.py` | 情景假设/状态、基线（含调整记录与实际值）、预警的轻量 JSON 持久化 |
| `sample_data.py` | 内置演示目录（共段重复收益、依赖链、道路互斥组、远期项目、两种评审情景） |

## 一致性约束（引擎与调整单闸门双重执行）

1. **年度预算**：每年度项目成本曲线占用之和不得超过 envelope。
2. **资金来源**：按 `FundingCommitment` 比例同时校验年度专池与跨年度总池。
3. **共同管段**：有资产交集的项目在任一年度不得处于同年施工。
4. **互斥施工**：同 `mutex_groups` 的项目在同一方案/基线中不可共存。
5. **前置依赖与滞后**：开工年不早于前置项目完工年 + 1 + `lag_years`；上游落选/移除
   时，下游配套按最短依赖链**级联失效**，并输出传播链解释。
6. **horizon**：最远不超过预算覆盖末年；远期项目落选并给出原因。
7. **收益不重复计算**：每条管段的申报收益在方案中只认可一次（按优先级归首个申报者），
   重复部分单独披露为 `duplicated_benefit_eliminated`。
8. **过期情景**：超过 `expires_on` 的情景不能重算、提交或发布（自动置为 expired，
   可复制为新情景）。
9. **基线不可直接改**：所有变更走调整单；调整单必须匹配基线当前版本，预演全部约束后
   才能提交应用，产生新版本号并留存调整痕迹（含级联移除清单）。

## 运行

```bash
# 编译与测试（40 项）
python -m compileall -q src cli.py run_cli.py tests
python -m unittest discover -s tests -v

# 旧契约冒烟（保持向后兼容）
python run_cli.py

# 无参数 = 引导式完整治理闭环演示（重叠诊断→方案→解释→对比→发布→回填→调整单→版本）
python cli.py

# 子命令（跨命令状态通过 --state 持久化，默认 .portfolio_state.json）
python cli.py overlaps
python cli.py deps
python cli.py compute S-BASE
python cli.py explain S-BASE P-103      # 逐项入选/落选理由（每个尝试年份的约束明细）
python cli.py propagation S-BASE        # 落选项目连带失效的下游配套与传播链
python cli.py headroom S-BASE           # 年度预算与资金来源余量
python cli.py compare S-BASE S-URBAN    # 假设/入选集/排程/状态/占用/收益的可复核差异
python cli.py priority S-BASE P-106 5000000
python cli.py weight S-BASE risk_value_per_point 3000000
python cli.py lock S-BASE P-103 2027    # 锁定入选（可锁年）
python cli.py exclude S-BASE P-102
python cli.py unlock S-BASE P-103
python cli.py submit S-BASE
python cli.py publish S-BASE BL-2027
python cli.py adjust BL-2027 --remove P-101 --add P-106:2028 --apply
python cli.py backfill P-100 2027 34000000 2500 --baseline BL-2027
python cli.py baseline BL-2027
python cli.py --json overview S-BASE    # JSON 接口形态
python cli.py --expired-demo submit S-URBAN   # 验证过期拒绝
```

## 偏差预警阈值（滚动回填）

- 累计支出超概算 ≥10% 为 warn，≥20% 为 major；低于计划 ≥10% 为 underspend warn。
- 里程完成占比比按成本应有进度落后 ≥15 个百分点为 mileage_lag。
- 回填只能落在项目计划施工年，且项目须在当前基线中。

## 设计说明

- **向后兼容**：`BudgetEnvelope(year, amount)`、`ProjectVersion(pid, ver, assets, cost)`
  原有位置参数与校验保持不变，新增字段全部带默认值。
- **版本取舍**：同一项目可登记多个版本，分析与规划自动取最新版。
- **确定性**：评分相同时按成本、标识兜底排序，同一输入必得同一方案，便于复核。
- **可解释性**：落选决定按候选开工年逐一记录预算/资金/共段/互斥/依赖失败明细，
  而非只给结论；情景对比逐项列出假设变化、入选集变化、重排与年度占用差异。
- **不可变版本**：调整单应用返回基线新版本（原对象保留），实际值随项目移除自动清理。
