# H3 v1.5 中央集成说明

本版不再要求人工编辑中央文件。执行：

```powershell
python -B install_h3_full.py --root <freqtrade-root> --run-tests
```

安装器会先备份，再调用 `integrate_h3_full.py`。中央文件解析或编译失败时，全部写入自动
回滚。安装结果和备份位置记录在：

```text
user_data/audit/h3-full-*/install-manifest.json
```

检查重点：

```powershell
python -m py_compile `
  freqtrade/persistence/trade_model.py `
  freqtrade/persistence/migrations.py

ruff check `
  freqtrade/persistence/trade_model.py `
  freqtrade/persistence/migrations.py `
  freqtrade/persistence/hedge_*.py `
  tests/hedge/persistence `
  tests/hedge/migrations

pytest -q tests/hedge/persistence tests/hedge/migrations
pytest -q
```

需要撤销时使用安装清单：

```powershell
python -B install_h3_full.py --restore-manifest <install-manifest.json>
```
