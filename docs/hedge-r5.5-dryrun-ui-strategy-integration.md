# R5.5 Dry-run UI与策略适配

R5.5为本地Paper/Dry-run提供可选、仅回环地址的Dashboard，并冻结策略输出契约。UI遥测不是交易事实源；SQL Hedge Ledger仍是唯一交易事实。策略动态参数只能降低静态风险上限。

## 策略列
优先使用`hedge_long_score`、`hedge_short_score`、`hedge_target_net_ratio`、置信度、风险缩放、方向缩放和`hedge_allow_new_risk`。旧`enter_long/enter_short`仍兼容。新增订单在下一根K线才可成交。

## 安全边界
- 主网写入保持锁定。
- Dashboard默认关闭，启用时必须绑定127.0.0.1。
- UI控制仅能暂停/恢复新增风险，减仓继续允许。
- JSONL损坏或磁盘失败不会中断撮合和SQL记账。
- 配置模板不包含真实交易所密钥。
