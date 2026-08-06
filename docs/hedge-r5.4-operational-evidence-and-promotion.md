# R5.4 Operational Evidence and Promotion

R5.4 is the bridge between a tested execution engine and real operational promotion.
The authority database records immutable, hash-bound evidence and stage decisions.

## Stage order

1. `MAINNET_READONLY`
2. `ORDER_TEST`
3. `SUBMIT_CANCEL`
4. `SINGLE_LEG`
5. `DUAL_LEG`
6. `RESTRICTED_AUTO`
7. `EXPANDED_LIMITS`

Evaluation is cumulative. Evidence for a later stage never compensates for a missing earlier-stage artifact.

## Evidence workflow

```text
external run / offline drill
    -> strict result model
    -> EvidenceEnvelope
    -> account/environment/hash/freshness verification
    -> R54OperationalStore
    -> cumulative promotion decision
    -> evidence bundle
```

## Main commands

```powershell
python -m freqtrade.hedge.r54 init-store --database artifacts/hedge_r54/r54-authority.sqlite
python -m freqtrade.hedge.r54 record-evidence --database ... --evidence ... --account-id ...
python -m freqtrade.hedge.r54 evaluate-all --database ... --account-id ... --output ...
python tools/hedge_r54/offline_acceptance.py artifacts/hedge_r54/R5.4-OFFLINE-ACCEPTANCE.json
```

## External evidence

The package contains validators and orchestration, not fabricated operational facts. A 72-hour evidence file
can only pass when its underlying observation timeline is actually at least 72 hours, continuous, clean and
contains at least one funding observation.
