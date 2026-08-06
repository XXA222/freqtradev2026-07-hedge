# N7K2 R1.8 execution/persistence boundary

R1.8 removes ORM model imports from `freqtrade.hedge.execution`.  The execution
domain now contains contracts, state machines, services and in-memory fakes only.
SQL implementations live in `freqtrade.persistence.hedge_execution_adapters` and
are wired exclusively by the integration composition root.

The Paper control-plane regression now uses explicit `operation_mode=paper` and a
durable JSON checkpoint.  A transient/readonly runtime remains intentionally
fail-closed and must not report PAPER readiness.
