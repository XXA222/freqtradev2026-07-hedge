# 方向三 Risk / Lock / SingleWriter / Readiness v1.4

## 运行链路

```text
Direction-2 REST/User Stream/Ledger AccountRiskFacts
                         |
                         v
                RiskPortfolioSnapshot
                         |
                         v
                  HedgeRiskRuntime
                         |
       +-----------------+------------------+
       |                 |                  |
ReadinessMonitor  SingleWriterLeaseRunner  RiskActionStateMachine
       |                 |                  |
       +-----------------+------------------+
                         |
                RiskApprovalCoordinator
                         |
        +----------------+----------------+
        |                                 |
Increase capacity reservation       Locked Reduce reservation
        |                                 |
        +----------------+----------------+
                         |
 ApprovalCommitRecord + fencing token + exact request/risk snapshot
                         |
                         v
       direction-one transactional repository / pending risk / Outbox
```

## 核心不变量

- 完整腿身份：`exchange + account_id + canonical_symbol + position_side`。
- 任何允许审批的数量和名义金额不超过原请求。
- Reduce/CLOSE 缺少确认仓位时，协调器和底层引擎均拒绝。
- 同腿所有未完成 Reduce 和本地 reservation 总和不得超过确认仓位。
- scoped UNKNOWN 阻止同腿新增风险与再次减仓；UNKNOWN 无法定位时账户级 HALT。
- 缺少 liquidation/maintenance/风险版本事实时停止新增风险。
- SingleWriter fencing token 失效后不再产生新的 durable approval。
- local reservation 只在 durable pending risk 事务提交并验证后释放。
- Account→Position→Order 固定顺序；async 路径不阻塞事件循环。

## Readiness 状态

- `READY`：所有检查通过。
- `DEGRADED`：阻止新增风险，但 Reason Policy 可允许受控减仓。
- `HALT`：账户、写入或事实完整性失败；只有显式允许的动作可继续。

Reason Code 同时携带 severity、scope 和 controlled-reduce policy。调用方不得自行实现第二套“是否可交易”逻辑。

## durable handoff

生产 `RiskApprovalCommitPort` 必须原子保存：

- decision/intent/idempotency/correlation；
- request JSON 和 AccountRiskSnapshot JSON；
- approved quantity/notional；
- rules version、target snapshot version、evaluated/committed time；
- SingleWriter fencing token；
- durable pending risk 与 Outbox。

提交被接受但读回暂时失败时，本地 reservation 保留；读路径恢复后使用相同 commit record 幂等验证。
