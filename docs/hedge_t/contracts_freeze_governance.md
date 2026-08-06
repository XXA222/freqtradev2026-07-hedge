# Hedge 公共契约冻结

冻结版本：`1.0.0`。五个工作流必须从冻结提交创建分支，并仅依赖
`freqtrade.hedge.contracts` 中公开对象和端口。

禁止直接把跨工作流依赖加入 `freqtradebot.py`、`wallets.py`、`trade_model.py`
或 `exchange.py`。任何契约变更必须先提交接口变更提案，说明兼容性、迁移和
所有消费者；批准后仅通过提高契约版本实施。
