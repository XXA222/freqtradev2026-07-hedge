# Freqtrade Hedge 当前项目状态与待办

生成时间：2026-07-26（Asia/Shanghai）  
源码版本：`30183ee chore(docker): consolidate into fixed workspace container`  
P2-H2 核心版本：`d316248 feat(hedge): integrate P2-H2 safety core`

## 运行约束

- 唯一固定容器：`freqtrade-hedge-workspace`；
- 镜像：`freqtrade-hedge:p2-h1-test`，已包含项目、开发与测试依赖；
- 项目目录完整挂载到容器内 `/workspace`；
- 开发、测试和依赖检查均通过 `docker exec freqtrade-hedge-workspace ...` 执行；
- 不再使用 `docker compose run` 创建临时测试容器；
- P3 交易写入路径保持锁定：没有下单、撤单、杠杆、保证金模式或持仓模式写操作。

## 已完成能力

- P2-H1 Docker 基础：固定镜像、依赖、受限运行权限与可复现测试环境；
- P2-H2 安全核心：有限 Decimal 校验、交易对标准化、账户/方向维度持仓主键；
- LONG/SHORT 双腿持仓簿、对账、目标仓位差分、幂等键、Reduce-Only 本地计算；
- Binance 全账户只读检查：仓位、订单、成交、时钟偏差、仓位模式、未管理敞口与风险 HALT；
- 用户流 listen-key 生命周期与强制 REST 再校准；
- Wallet 的双腿索引和旧单腿接口兼容：双腿歧义时拒绝旧的 pair-only 查询；
- 配置校验和执行锁：P2 仅允许 `read_only=true`、`live_trading_enabled=false`。

## 最近验证结果

| 检查 | 结果 |
| --- | --- |
| `python -m pip check`（固定容器） | 通过，无损坏依赖 |
| `pytest tests/hedge/p2_h2 -q` | 35 通过 |
| `pytest tests/test_wallets.py::test_check_exit_amount_futures tests/hedge -q` | 126 通过，3 子测试通过 |
| `ruff check freqtrade/hedge freqtrade/wallets.py tests/hedge tests/test_wallets.py` | 通过 |
| 功能全量 `pytest -q -o addopts=`（排除下述环境性能探针） | 4517 通过、20 跳过、395 deselect、3 子测试通过 |

### 已正式隔离的环境性能探针

`tests/commands/test_startup_time.py::test_startup_time` 不涉及 Hedge 功能，但上游要求 CLI 在 0.5 秒内启动。Windows Docker 绑定挂载环境连续测得 0.759s、0.775s 和 0.640s，因此在 Docker 功能全量门禁中明确 deselect；未删除、未修改该上游测试。应在 Linux 原生 CI 文件系统上重新评估该性能指标。

## 未完成事项

以下项目是设计边界内明确尚未实现的后续阶段，不代表当前 P2-H2 测试失败：

1. P2 后续安全阶段（P2-H3 及以后）的服务化集成与验收；
2. 数据库 `Intent`、`Fill`、`Snapshot`、`Outbox` 表及迁移；
3. 生产 WebSocket 传输、断线恢复与持久化状态衔接；
4. P3 受控执行服务：下单、撤单、Reduce-Only 订单生命周期与审批/审计；
5. 杠杆、保证金模式、持仓模式等交易所写操作（当前严格禁止）；
6. 双腿 Hedge 专用回测、仿真成交与风险归因；
7. UI、消息控制端点及运行监控面板；
8. Linux 原生 CI 复核上游 CLI 启动性能阈值；
9. 在具备只读 API 权限后，进行真实 Binance 只读联调；不得启用交易权限或写路径。

## 恢复点

- P2-H2 标签：`p2-h2-integrated-safety-core-20260726`；
- P2-H2 bundle：`D:\Program Files\freqtrade-hedge-backups\freqtrade-hedge-p2-h2-integrated-safety-core-20260726.bundle`；
- 当前单容器编排提交：`30183ee`。
