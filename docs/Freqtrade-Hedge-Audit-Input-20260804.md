# Freqtrade-Hedge 四份审计合并核验总报告



## 结论

两份上传报告中的大部分架构性结论可以由源码支持，但并非全部准确。经逐条核验：第一份10项中7项确认、2项部分确认、1项否定；第二份15项中8项确认、5项条件/部分确认、2项否定或明显过度。



- 原始源码ZIP文件数：1339

- 原始ZIP中的 `.pyc`：0

- 原始ZIP中的 `__pycache__`：0



## 合并后的P0

### M-P0-01 生产权威运行路径分裂

Freqtrade集成的production-locked主循环仍构造IntegratedFakeRuntime，标准Hedge配置又明确禁止live；真正R5主网写入通过独立r5.cli运行。结果是控制面、策略主循环和真实写适配器不在同一权威组合根。

- 源码证据：`freqtrade/hedge/integration/production_assembly.py:10-13,44,73-117；freqtrade/hedge/config.py:418-428；freqtrade/hedge/r5/cli.py:90-160`

### M-P0-02 R5真实写入绕过正式持久化风险执行链

R5OrderExecutor使用简化_R5RiskApproval、InMemoryExecutionStore和InMemoryIdempotencyStore，没有接入HedgeRiskRuntime、SQL执行权威、SingleWriter/PositionLock、RiskApprovalCommit和Outbox。

- 源码证据：`freqtrade/hedge/r5/executor.py:51-121；docs/hedge_t/direction3_risk_concurrency_readiness.md；direction5_execution_control.md`

### M-P0-03 R5单周期多意图累计风险错误

Bridge逐个处理意图时都从同一旧account快照投影；两个各60 USDT的意图会各自被评估为60，而非第二笔按累计120评估，可能突破Gross/Net/OpenOrders上限。

- 源码证据：`freqtrade/hedge/r5/strategy_bridge.py:44-110；freqtrade/hedge/r5/auto.py:71-98`

### M-P0-04 R5日亏损门禁缺少权威数据源

max_daily_loss只依赖cycle可选返回的realized_pnl；默认Bridge不返回该字段，也不根据权益、成交账本或Binance Income计算，默认自动路径中的realized_loss可长期保持0。

- 源码证据：`freqtrade/hedge/r5/auto.py:83-124；strategy_bridge.py:44-86；budget.py:104-121`

### M-P0-05 允许标的上的外部订单/仓位所有权识别不完整

R5主要按symbol区分managed/unmanaged，BTCUSDT/ETHUSDT上的手工单、其他机器人订单及外部仓位未按clientOrderId前缀、run或持久账本归属识别。

- 源码证据：`freqtrade/hedge/r5/mainnet_api.py:246-275；对比exchange/binance_user_stream.py:1365-1368`

### M-P0-06 R5 Permit撤销对已签发对象无效

revoke_all()只清空Authority内部登记；已持有的R5StagePermit.consume()不回查Authority，动态复现显示撤销后仍能继续扣减写预算。

- 源码证据：`freqtrade/hedge/r5/arming.py:13-84`

### M-P0-07 SingleWriter fencing token未贯穿真实副作用

HedgeExecutionEngine仅在开头assert_leader，SingleWriterPort返回None；fencing token没有进入风险审批、状态条件更新和最后一次交易所写请求，存在失去租约后的TOCTOU窗口。

- 源码证据：`freqtrade/hedge/contracts/ports.py:38-40；integration/composition.py:121-131；execution/orchestrator.py:115-143`

### M-P0-08 Fresh Schema与迁移Schema不一致

ExecutionOrderStateRow和ExecutionIdempotencyRow已定义，但不在HEDGE_MODEL_CLASSES；create_hedge_tables不会创建它们，H3-027迁移另行创建。测试fixture直接调用create_hedge_tables，导致fresh/migrated/test三条Schema路径分叉。

- 源码证据：`freqtrade/persistence/hedge_models.py:752-840,1060-1100；hedge_migrations.py:64-67,2359-2385；tests/hedge/persistence/conftest.py:7-13`


## 合并后的P1

### M-P1-01 R5恢复是阻断重放而非完整自动恢复

重启会转RECOVERY_REQUIRED，但R5本地订单与幂等状态是内存态；query又依赖本地store。recent fills还依赖已有exchange_order_id映射，ACK丢失场景下证据源不独立。

- 源码证据：`r5/orchestrator.py:114-143；r5/executor.py:90-138；execution/binance_usdm_adapter.py:341-387,589-614`

### M-P1-02 Backtest/Paper/集成主循环/R5实盘不共享同一执行语义

Backtest和Paper使用PureHedgePlanner及通用执行引擎；R5策略直接返回execution.OrderIntent，真实写入独立运行。测试Fake路径不能证明R5真实路径。

- 源码证据：`optimize/hedge_backtesting.py；integration/production_main_loop.py；r5/strategy_bridge.py`

### M-P1-03 R5不能执行完整PlanningResult

规划器可输出submit、cancel、modify、delete和risk_cancel；R5StrategyPort只返回可提交OrderIntent，无法完成理想订单集、风险撤单和cancel-replace。

- 源码证据：`planning/context.py:664-679；planning/ideal_orders.py:255-267；r5/strategy_bridge.py:16-86`

### M-P1-04 R5自动循环无1分钟K线节拍和决策游标

AutoSession在range(max_cycles)中连续执行，没有sleep、闭合K线时间、decision_id或去重游标，可能在同一1m K线上快速多次决策并消耗API/写预算。

- 源码证据：`freqtrade/hedge/r5/auto.py:62-99`

### M-P1-05 自动会话时长与短TTL授权不一致

RESTRICTED_AUTO/EXPANDED_LIMITS允许60/240分钟，但Permit和ProductionExecutionGate默认约5分钟且无续期，长会话名义上不能持续；简单延长TTL又会扩大撤销窗口。

- 源码证据：`r5/models.py:187-197；r5/arming.py:40-80；r5/executor.py:78-99`

### M-P1-06 统一禁Market阻断紧急reduce-only减仓

Unstuck和控制面Close使用MARKET减仓，R5却统一禁止Market，没有区分风险增加与受限应急退出。

- 源码证据：`planning/unstuck.py:96-115；control/service.py:454-505；r5/evidence.py:126-132；r5/executor.py:55-68`

### M-P1-07 长会话时钟证据只在启动时校准

preflight和Executor构造时同步时钟，运行期间无周期刷新；系统时间调整或漂移后Readiness仍可能使用旧证据。

- 源码证据：`r5/mainnet_api.py:77-78,130-153；r5/executor.py:122`

### M-P1-08 用户流对同毫秒事件采用过强全序假设

相同transaction/event毫秒但状态签名不同会先判GAP，即使累计成交单调增加；可能把合法更新变成重连/全量校准风暴。

- 源码证据：`exchange/binance_user_stream.py:262-304,341-387；tests/hedge/exchange/test_user_stream.py:352`

### M-P1-09 订单状态、Fill、Audit与Outbox不在一个网络后事务

SqlExecutionStore逐阶段独立提交；core.submit返回后ExecutionLedger再另开事务写Intent/Fill/Audit/Outbox。网络成功而第二事务失败时会形成事实缺口。

- 源码证据：`execution/service.py:802-870；persistence/hedge_execution_adapters.py:225-451,721-725；execution/orchestrator.py:200-240`

### M-P1-10 R5 Journal/Budget与正式账本形成多事实源

R5运行、证据和每日预算各用独立SQLite；预算在网络提交前直接累计，明确拒绝/前置失败时没有预留-确认-释放状态。

- 源码证据：`r5/config.py:61-63,157-159；r5/budget.py:52-121；persistence/hedge_models.py`

### M-P1-11 控制平面与R5真实写入未统一

集成控制服务的Kill/Cancel/Close仍受LIVE_WRITE_LOCKED；R5由独立CLI和进程内KillSwitch执行，人工处置不是同一订单账本上的权威入口。

- 源码证据：`control/service.py:330-505；r5/executor.py:114-121`

### M-P1-12 原生Trade/Order与独立Hedge账本权威边界不清

trade_model加入account/side/open_slot等字段，同时Hedge又维护Intent、Snapshot、Fill和CurrentProjection；需明确原生Trade是兼容投影还是事实源，避免双向写入和恢复冲突。

- 源码证据：`persistence/trade_model.py:500-536,870-877,1992-2014；persistence/hedge_models.py`

### M-P1-13 Gate支持合同与Binance-only Hedge实现不一致

配置接受exchange_adapter=gate，但HedgeRuntimeCoordinator无条件构建BinanceReadonlyRuntime，旧BinanceReadonlyAdapter也明确拒绝非binance。上游Freqtrade支持Gate不等于Hedge链已支持Gate。

- 源码证据：`hedge/config.py:26,258-266,402-404；integration/coordinator.py:1,96-101；hedge/binance_readonly.py:330-331`

### M-P1-14 Wallet双腿歧义被静默映射为0，且存在真实调用点

get_position在双腿时返回None，get_owned随后返回0；freqtradebot.handle_onexchange_order仍不带position_side调用，可能把歧义解释为余额消失并调整Trade。

- 源码证据：`freqtrade/wallets.py:222-238,684-699；freqtrade/freqtradebot.py:613-690`

### M-P1-15 Hedge WebSocket未应用角色和事件级最小权限

REST把用户名映射为HedgeRole；WS只判断ws_token或JWT是否合法，任何有效JWT均可订阅全部HedgeTelemetryEvent，没有VIEWER检查、账户范围或敏感字段过滤。

- 源码证据：`rpc/api_server/hedge_plugin.py:34-55,95-101；hedge_ws.py:221-310`

### M-P1-16 ProductionExecutionRuntime内部状态审计仍是内存态

build_production_execution_runtime固定构造InMemoryAuditLog。即使外层事务账本持久化最终事件，ExecutionService逐阶段审计在崩溃后仍会丢失。

- 源码证据：`execution/production_runtime.py:20-44,102-121；execution/service.py:811-922,1441-1533`

### M-P1-17 Binance权重限速仅进程内协作

AdaptiveWeightLimiter使用本地计数和asyncio.Lock；多个机器人、R5脚本、控制面或调试工具共享同一IP/API额度时无法协同。

- 源码证据：`exchange/rate_limit.py:108-173`

### M-P1-18 单managed_pair与BTC/ETH账户作用域未统一

标准Hedge MVP只允许一个白名单pair；production main loop和R5证据采用BTCUSDT/ETHUSDT固定集合。当前可运行，但策略管理范围、账户观察范围和晋级证据范围不是同一个合同。

- 源码证据：`hedge/config.py:405-417；integration/main_loop_config.py:10-56,122-128；r5/models.py:13；r5/mainnet_api.py:92-102`

### M-P1-19 生产主循环K线游标和部分状态仅在内存

ProductionHedgeController用_last_candle_close去重，重启后丢失；没有统一持久游标补齐全部遗漏闭合K线。

- 源码证据：`integration/production_controller.py:53-55,76-137`

### M-P1-20 生产路径专项测试与长时间证据仍不足

测试数量丰富，但仍缺真实生产路径的多意图、双进程抢写、崩溃点、Outbox事务、ACK丢失、同毫秒用户流、配置样例和24/72小时门禁组合验证。

- 源码证据：`tests/hedge与tools/hedge_r*；源码包不含可核验完整24/72小时工件`


## 合并后的P2

### M-P2-01 多套领域DTO和枚举长期并存

contracts、execution、planning和R5各自定义相近OrderIntent/Side/Readiness，需要收敛到冻结契约。

### M-P2-02 配置样例缺少权威类型清单和统一验收

config_examples同时包含完整Freqtrade配置、阶段overlay和部署清单；R4/R5使用独立命名空间，R35还含本机个人路径。应建立manifest和deprecated标记，再按类型测试。

### M-P2-03 Legacy reduce/close兼容入口可自填confirmed_quantity

HedgeRiskEngine.evaluate()在未指定position_side时把requested_quantity当confirmed_quantity；当前正式Coordinator使用evaluate_request，未发现生产误用，但应禁止真实写路径调用兼容入口。

### M-P2-04 主网凭据文件缺少ACL/Owner校验

加载器校验格式和脱敏，但未检查Windows ACL、owner或Unix 0600。

### M-P2-05 通用价格归一化不区分BUY/SELL/Post-only

统一ROUND_DOWN可能使SELL GTX价格更激进；应按方向量化并检查是否跨book。

### M-P2-06 高风险模块和函数过度集中

大文件和长函数降低事务、状态机及恢复代码的可审计性，应按Port/状态转换/UoW/Telemetry拆分。

### M-P2-07 迁移链过长且fresh/migrated矩阵复杂

H3-001至H3-032与上游迁移协同，需建立基线快照迁移和受支持升级窗口。

### M-P2-08 UTC naive/aware边界脆弱

大量DateTime(timezone=False)与外部aware证据混用，项目已出现TimezoneEvidenceParsingHotfix。

### M-P2-09 R4/R5和阶段工具长期留在生产源码树

版本阶段、业务模块和发行证据混合，增加权威入口识别成本。

### M-P2-10 上游核心Patch面与rebase成本偏高

FreqtradeBot、Wallets、Trade模型、迁移、RPC/API等存在多点Hedge触点，应压缩为最小hook并建立上游隔离测试。

### M-P2-11 版本/Git/热修复可追溯性不足

项目目录2026.07、基线2026.8-dev且git_metadata=false，R5.1.x靠连续安装热修复推进。

### M-P2-12 最小审计测试入口不够独立

pytest-xdist已声明为开发依赖，因此不是源码缺陷；但应提供一键安装+smoke marker，使审计包能在标准命令下复现。

### M-P2-13 文档成熟度标签需要分层

应分别标记模块完成、集成完成、生产证据完成和无人值守完成，避免“full completion/production”被误解。


## 不采纳或降级的结论

### X-01 原始源码ZIP包含大量.pyc/__pycache__ — 否定

直接读取ZIP中央目录：1339个文件，.pyc=0，__pycache__=0。

### X-02 缺少pytest-xdist证明源码交付不完整 — 降级

pyproject.toml的develop依赖和requirements-dev.txt都声明pytest-xdist；当前审计环境未安装开发依赖。问题是复现便利性，不是源码缺项。

### X-03 ConfirmationService内存令牌导致确认不可审计 — 否定主要结论

重启失效是安全行为；issue_confirmation、operation completed/failed和actor/role均进入持久审计/ControlOperationStore。可增加TOKEN_CONSUMED事件，但非阻断缺陷。

### X-04 单managed_pair与BTC/ETH必然运行冲突 — 降级

两套范围确实未统一，但R5单次请求只选一个symbol，main loop也可用allowed集合包含managed_pair；应作为合同收敛问题，而非直接崩溃证据。

### X-05 示例配置中的空key/secret本身就是不兼容 — 否定

空值是常见占位符；真正问题是示例类型未标识，以及部分overlay被命名为普通example config。


## 输入文件SHA256

- source_zip: `f8b02a64e13834fbde746b2e3fe0986971c1627ba10c90e6837f91c991400fb8`

- report_a: `b8de88c9508fc8e17ad37676fc262c533a92bf1a8c60d7fb774978616cf20d54`

- report_b: `358c40e9a4df5fb763252b20f63ec6cd2732d7bf0bad3ec5519634a87585c6e7`