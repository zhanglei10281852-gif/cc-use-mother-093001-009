# 管网改造投资组合治理

面向市级投资评审处的地下管网改造投资组合治理服务：接收带资产范围、成本曲线、风险证据、
前置依赖和资金来源的项目版本，识别范围重叠与收益冲突，形成跨年度候选方案，支持评审人
锁定假设、比较情景、调整优先级并提交审批；审批后的基线只能通过正式调整单变更，实际
支出与里程完成量可滚动回填并触发偏差预警，接口与命令行可解释每个入选/落选决定、依赖
传播、资金余量与情景间可复核差异。

## 运行

```bash
pip install -r requirements.txt   # 仅 API 与 API 测试需要; 核心逻辑与 CLI 为纯标准库

python -m unittest discover -s tests -v          # 运行测试
python -m compileall -q src tests run_cli.py     # 编译检查
python run_cli.py                                # 遗留契约冒烟

PYTHONPATH=src python -m capital_portfolio.cli --db portfolio.db seed   # 写入演示数据
PYTHONPATH=src python -m capital_portfolio.cli --db portfolio.db demo   # 端到端演示
PYTHONPATH=src python -m capital_portfolio.cli --db portfolio.db serve  # 启动 HTTP 服务(8000 端口)
```

## 业务闭环

1. **项目申报**：`project submit` 提交项目版本（资产范围=管段长度、成本曲线=分年成本、
   风险证据=严重度×可能性及佐证、前置依赖、可用资金来源、分管段收益申报、互斥施工声明）。
   提交时校验版本递增、资金来源存在、依赖无环。
2. **冲突识别**：自动识别同一管段被多项目覆盖（范围重叠）与同一(管段,指标)收益被重复
   申报（收益冲突），收益按开工最早项目归属去重。
3. **候选方案**：`scenario create` + `scenario solve` 在规划窗口内按优先级得分
   （风险分 + 收益成本比，可乘评审人倍率）贪心排程，硬约束包括年度分来源预算占用、
   前置项目完工先于后继开工、互斥施工（同窗口/同组合）、范围重叠策略（去重/禁止）。
   每个项目都记录入选或落选原因（资金缺口、前置未入选、互斥冲突、窗口不可行等）。
4. **评审**：`scenario lock` 锁定假设（INCLUDE/EXCLUDE/PIN_START/CAP_OVERRIDE/PRIORITY），
   假设变更后必须重新求解；`compare` 输出两个情景的项目增减、排程变化、年度预算差、
   收益差与决定原因差；`scenario submit/approve/reject` 走审批流。
5. **发布**：`scenario publish` 仅允许已审批且未过期的情景；项目出现更新版本、资金
   额度更新或超过有效期都会使情景过期，过期情景不能直接发布，须 `scenario refresh`
   重新校核产生新修订。
6. **基线治理**：发布后基线只读，变更只能通过 `co create/submit/approve/apply`
   正式调整单（改期/换版本/增项/删项/资金再分配），应用前重新校验全部一致性约束，
   通过后产生新的基线修订。
7. **执行跟踪**：`actual add` 滚动回填实际支出与里程（同期重报自动取代旧值），
   成本偏差超 10% 或里程滞后超 15% 触发预警（超两倍阈值升级为 CRITICAL），
   `baseline deviation` 输出各项目计划与实际对照。

## 解释接口

| 接口 | CLI | 说明 |
| --- | --- | --- |
| GET /scenarios/{id}/explain/selection | `scenario explain-selection` | 每个项目入选/落选决定及原因 |
| GET /scenarios/{id}/explain/dependencies/{pid} | `scenario explain-deps` | 依赖链与延后/移除的传播影响 |
| GET /scenarios/{id}/explain/funding | `scenario explain-funding` | 分年度分来源的占用与资金余量 |
| GET /compare?a=&b= | `compare A B` | 情景间可复核差异 |
| GET /audit | `audit` | 全量审计日志 |

## 代码结构

```
src/capital_portfolio/
  contracts.py   基础契约(遗留)        domain.py    领域模型与枚举
  store.py       SQLite 持久化+审计    analysis.py  重叠/收益冲突识别、依赖图与传播
  planner.py     跨年度组合求解器      service.py   情景生命周期、基线、调整单、回填预警
  explain.py     可复核解释            seed.py      演示数据
  api.py         FastAPI 接口          cli.py       命令行
```
