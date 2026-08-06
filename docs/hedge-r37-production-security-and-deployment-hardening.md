# R3.7 Production Security and Deployment Hardening

R3.7 hardens the execution and deployment boundary without enabling Binance
mainnet writes. The managed trading scope remains BTCUSDT and ETHUSDT USD-M
perpetual contracts only.

## Ambiguous submit crash safety

Before a Testnet submit request, the durable journal stores the deterministic
client order ID, full intent, actor, and `SUBMITTING` state. A process restarted
from `SUBMITTING` may query and compensate the existing order, but it never
blindly repeats `POST /fapi/v1/order`. An unresolved query ends in
`RECOVERY_REQUIRED` and requires operator review.

## Independent arming authority

The operator-entered arm token is checked against an independently provisioned
SHA256 digest. The runtime no longer derives the expected digest from the same
input token. Run IDs are also bound to the actor that claimed them.

## HTTP credential boundary

Authenticated Binance HTTP requests reject redirects. REST base URLs must use
the exact approved host, HTTPS, the expected port, and an empty root path.

## Recoverable control-operation leases

Durable dangerous-operation claims include lease owner and expiration. A stale
claim can be recovered only for the same account, idempotency key, payload, and
actor. Completion and failure clear the lease. H3-031 and H3-032 add and verify
the required columns.

## Transactional installation

The R3.7 installer records replaced and newly created files. If compilation,
imports, tests, or acceptance fail, it restores replaced files and removes new
files before returning a failure. Mainnet credentials are not loaded and no
network requests are made by the installer.
