# Freqtrade-Hedge v1.5.0 Stage-A — 20-pass remediation record

## Scope and honesty boundary

This record maps the 24-page source audit to twenty independent engineering passes. It is an
implementation/evidence log, not a claim of elapsed wall-clock time. The release remains
**read-only/Paper only**. Production Binance writes, testnet evidence, PostgreSQL CI and 24/72-hour
soak evidence are gates that cannot be fabricated by a source patch.

## Passes

1. **Baseline and release boundary** — froze the v1.4.2 reviewed tree, excluded runtime artifacts,
   retained upstream ordinary-mode behavior as a first-class release gate.
2. **Native write barrier** — kept all legacy exchange write routes blocked whenever Hedge mode is
   enabled; cleanup, RPC and scheduling cannot enter native order mutation.
3. **Operation modes** — removed ambiguous `combined`; introduced explicit `shadow` semantics.
4. **Source-separated runtime** — split EXCHANGE, PAPER, LIVE and SHADOW projections, each with an
   independent sequence, source version, event time, staleness and halt scope.
5. **Effective safety policy** — readonly/shadow safety is derived from EXCHANGE facts; Paper can
   never clear an EXCHANGE or SYSTEM halt.
6. **Readiness namespaces** — separated common, exchange, paper, live and shadow checks. Paper no
   longer publishes fake user-stream or reconciliation health.
7. **H3 migration isolation** — removed H3 imports/calls from the upstream native migration hook.
   Added explicit plan/bootstrap/verify CLI entry points under a Hedge feature gate.
8. **Migration continuation** — added immutable steps H3-023..H3-026 for durable risk approval,
   source-scoped Paper checkpoints and action groups; existing published steps remain untouched.
9. **Common Freqtrade lifecycle** — Hedge mode now runs market refresh, DataProvider refresh,
   strategy loop/analyze, RPC queue, scheduler and commit phases without entering native entries,
   exits or order maintenance.
10. **Strategy signal adapter** — introduced a no-future-data adapter over the latest analyzed row,
    preferring Hedge target columns and preserving compatibility with enter-long/enter-short.
11. **Exchange market rules** — tick/step/minimums come from loaded exchange markets with a
    versioned configuration fallback.
12. **Strict configuration** — rejected unknown Hedge/Planner/Paper keys, normalized deprecated
    managed-pair aliases and made instant fill explicit test-only behavior.
13. **Paper/Backtest matcher parity** — Paper uses the conservative simulation matcher with price
    touch, volume budget, partial-fill ratio, fill-per-bar limit, fee and funding semantics.
14. **Durable Paper checkpoint** — active lifecycle, filled quantity, fake exchange snapshot,
    idempotency completion, account legs, buckets, planner mapping and last market are restored
    without resubmission.
15. **Durable execution facts** — execution intent, fill, audit and Outbox facts are mirrored to SQL
    before the in-process projection advances.
16. **Transactional risk handoff** — approved reservation evidence is committed to a unique SQL
    row keyed by decision and idempotency identity; conflicting evidence fails closed.
17. **Recoverable action groups** — added a SQL ActionGroup repository and source-scoped checkpoint
    table for restart-safe multi-leg coordination.
18. **Control-plane contracts** — REST responses expose source/version/sequence/event time/stale;
    WebSocket supports replay, explicit GAP events and bounded sequence history; roles and scoped
    confirmations are enforced server-side.
19. **Compatibility and debt containment** — preserved native Trade/Order/Wallet JSON behavior,
    unified exception reason codes, deprecated the old Binance readonly façade and kept new code
    behind adapters.
20. **Release and verification gates** — added Stage-A regression tests, local one-shot installer,
    full-Hedge/upstream test gates, manifest/hash generation, secret/artifact exclusions and an
    optional clean Git checkpoint workflow.

## Audit issue disposition

| Audit item | v1.5.0 disposition |
|---|---|
| P0-01 combined overwrite | Fixed by source-separated projections and `shadow` mode |
| P0-02 H3 global migration | Fixed; native migration hook contains no H3 call |
| P0-03 early return lifecycle bypass | Fixed through shared lifecycle + Hedge controller hook |
| P0-04 InMemory/fail-open production binding | Fail-open removed; SQL recovery, checkpoint, risk commit, action group and execution fact mirror added. In-process caches remain non-authoritative |
| P0-05 instant/full-fill Paper | Fixed for default Paper; conservative matcher is authoritative |
| P0-06 Binance live write adapter | **Not opened**; gated stub remains by design pending testnet implementation/evidence |
| P0-07 fake readonly health | Fixed through namespaced source checks |
| P1 strategy/data link | Implemented analyzed-data signal adapter |
| P1 market-rule drift | Exchange rules preferred and versioned |
| P1 backtest integration | Shared matcher/event engine is used; full standard Freqtrade CLI/UI integration remains Stage B |
| P1 duplicate contracts | Cross-direction adapters are centralized; complete deletion of legacy private DTOs is deferred to avoid unsafe big-bang replacement |
| P1 readonly dual stack | Legacy façade emits deprecation warnings; removal remains next upstream-compatible major release |
| P1 broad exceptions/large files | Boundary exceptions classified/narrowed in new paths; full file decomposition remains incremental |
| P1 API/WS | Source metadata, replay and GAP semantics implemented |
| P1 RBAC | Role mapping and payload-bound confirmations implemented; write routes remain disabled |
| P1 evidence | Automated source gates added; external PostgreSQL/soak/testnet evidence remains required |
| P1 Git governance | Reproducible checkpoint script and package manifest added |
| P1 core intrusion | H3 removed from native migration; remaining FreqtradeBot/Trade/Wallet compatibility hooks are minimal but still require upstream rebase tests |
| P1 config drift | Strict validation added |
| P2 UI/QQ/WeChat/multi-account | Not represented as complete; remains product Stage E after safety gates |

## Release stage

The package is a **Stage-A engineering candidate**, not a live-trading release. It may be promoted
only after local full test pass, PostgreSQL migration/recovery pass and real readonly soak evidence.
The Binance write feature gate must remain locked.
