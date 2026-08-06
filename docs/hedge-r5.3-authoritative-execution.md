# Hedge R5.3 Authoritative Execution

R5.3 composes planning, account-wide risk, durable ownership, single-writer fencing,
exchange lifecycle, fill recovery, Core/Tactical accounting, income projection, audit
and outbox into one SQL-authoritative graph.

## Normal execution

1. Read and bind fresh Binance account evidence.
2. Verify Hedge Mode, Cross mode, clock, key policy and account fingerprint.
3. Require signed adoption for every pre-existing position leg.
4. Recover PREPARED, SUBMITTING, ACKNOWLEDGED, PARTIAL and UNKNOWN orders.
5. Convert the complete PlanningResult into reduce-first cancellations and submissions.
6. Approve the whole intent batch against cumulative pending risk.
7. Reserve the whole daily budget before the first write.
8. Bind and recheck the exact fencing token at every final write boundary.
9. Persist the network result with Intent, Fill, Audit and Outbox in one transaction.
10. Project fills and Binance Income into Core/Tactical inventory and daily loss exactly once.

## Write isolation

The installer and protected-mainnet audit do not call order endpoints.  Real execution
is available only from `freqtrade.hedge.r5.authoritative_cli` and requires explicit
write flags, marked credentials with restrictive permissions, a stage arm token,
operator confirmation and all live readiness gates.

## Emergency reduction

Emergency MARKET is not a general strategy order.  It requires independent operator
and risk credentials, exact account/symbol/side/quantity scope, reduce-only action,
a 30-120 second TTL and single use.  It still follows the same SQL risk, budget,
fencing, lifecycle and fill projection chain.
