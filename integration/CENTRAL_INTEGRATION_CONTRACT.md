# H3 中央接入契约

`integrate_h3_full.py` 直接处理两个中央文件。

## trade_model.py

必须保证：

- Order 字段只声明一次；
- LocalTrade/Trade 字段只声明一次；
- 每个构造调用的 keyword 唯一；
- Trade 和 Order JSON 序列化、反序列化成对；
- legacy side 缺失时才按 `is_short` 推导；
- 活动 Trade 的 `open_slot_key` 为 `account_id|pair|position_side`；
-关闭 Trade 时清空活动槽。

## migrations.py

必须保证：

- H3 模块在 `check_migrate()` 内惰性导入；
- 原生迁移前准备原始数据库备份；
- 原生迁移完成后执行 H3 迁移；
- H3 迁移后执行账本投影恢复；
- 原生 Trade/Order 表重建复制 Hedge 字段；
- 重复执行适配器不改变文件。
