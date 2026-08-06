# P2-H3 并行分支集成前测试报告

测试时间：2026-07-26（Asia/Shanghai）  
监工基线：`230fa8a feat(hedge): freeze public contracts for parallel workstreams`  
执行环境：唯一固定容器 `freqtrade-hedge-workspace`

## 结论

五条工作分支均仍指向同一冻结基线 `230fa8a`，相对冻结提交没有方向源码改动。
因此本报告只能确认**公共契约和共享基线通过**，不能确认任何方向的业务实现通过，
也不允许将任何方向标记为“可合并”。

## 基线测试结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| Hedge 回归 | `python -m pytest tests/hedge -q --tb=short` | 132 通过，3 子测试通过 |
| 静态检查 | `ruff check freqtrade/hedge tests/hedge` | 通过 |
| 编译 | `python -m compileall -q freqtrade/hedge` | 通过 |

## 分支验收矩阵

| 方向 / 分支 | 相对冻结基线的提交 | 可验收源码 | 测试状态 | 结论 |
| --- | ---: | --- | --- | --- |
| 一：持久化、Repository、Outbox、迁移 / `p2-h3-persistence` | 0 | 无 | 仅共享基线通过 | 未提交，不可合并 |
| 二：只读 REST、WebSocket、校准 / `p2-h3-readonly-sync` | 0 | 无 | 仅共享基线通过 | 未提交，不可合并 |
| 三：Wallet、Risk、Lock、SingleWriter、Readiness / `p2-h3-coordination` | 0 | 无 | 仅共享基线通过 | 未提交，不可合并 |
| 四：策略规划、模拟器、双腿回测 / `p2-h3-recovery` | 0 | 无 | 仅共享基线通过 | 未提交，不可合并 |
| 五：执行状态机、API、监控 / `p2-h3-observation` | 0 | 无 | 仅共享基线通过 | 未提交，不可合并 |

## 改动与简单修复

- 本轮没有方向实现提交，因此没有可审查的业务改动；
- 未发现需要在分支内修复的简单语法、导入、格式或测试问题；
- 未修改任何中央集成文件；
- 公共契约目录保持冻结，版本为 `1.0.0`。

## 阻断项

1. 每条工作分支必须先提交其职责范围内的实现、单元测试和迁移/配置说明；
2. 不得直接修改中央文件：`freqtradebot.py`、`exchange.py`、`binance.py`、
   `strategy/interface.py`、RPC/API、配置校验/Schema、Compose、`hedge/__init__.py`；
3. 不得修改 `freqtrade/hedge/contracts/`；若必要，先提交接口变更提案并升级契约版本；
4. 方向二需要可替换的 WebSocket transport，真实只读凭据仅用于集成验收；
5. 方向四必须提供双腿回测或确定性模拟器测试；方向五必须仅连接 Fake/测试网执行端口；
6. 24/72 小时观察只能在方向一至三合并、只读服务实际持续运行后开始计时。

## 推荐合并准入顺序

严格采用：方向一 → 方向二 → 方向三 → 方向四 → 方向五。

每一步只在前一步已经通过专项、回归、契约兼容检查后，由集成负责人统一修改中央文件。
