# P2-H1 Docker统一基础里程碑

## 目标

本里程碑只建立可复现、可审计的Docker运行与测试基础，不开启真实订单，不修改交易所仓位模式，也不宣称P2只读闭环完成。

## 镜像

- `freqtrade-hedge:p2-h1-runtime`
- `freqtrade-hedge:p2-h1-dev`
- `freqtrade-hedge:p2-h1-test`

三个镜像使用相同Python基础镜像、相同源码提交和相同核心依赖。开发与测试依赖通过`pyproject.toml`中的`.[dev]`一次性固化到镜像，不使用宿主机Python、临时依赖目录或容器启动时`pip install`。

## Compose服务

- `hedge-runtime`
- `hedge-dev`
- `hedge-test`
- `hedge-backtest`
- `hedge-readonly`

`hedge-readonly`在本里程碑中保持fail-closed，直到P2-H2完成账户全量持仓检查、Wallet双腿安全、数值验证及专用只读服务入口。

## 验收闸门

1. Compose配置解析；
2. runtime/dev/test镜像构建；
3. `pip check`；
4. 核心、测试、FreqAI和FreqAI-RL导入矩阵；
5. `compileall`；
6. pytest完整收集；
7. `tests/hedge`；
8. 全部pytest；
9. Ruff；
10. one-way基线回测（当本地配置、策略和数据存在时）；
11. `git diff --check`；
12. 仅在全部通过后提交、打Tag和创建Git Bundle。

## 安全约束

- `dry_run=true`
- `hedge.read_only=true`
- 不启动runtime或readonly服务
- 不提交订单
- 不撤销订单
- 不修改账户持仓模式
- 不修改保证金模式
- 不修改杠杆