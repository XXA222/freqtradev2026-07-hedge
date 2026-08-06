# Freqtrade-Hedge R2.5.3 - Managed symbolConfig Scope Hotfix

R2.5.3 remains a read-only additive tool layer over the existing Hedge runtime.

## R2.5.3 changes

- filters account-level symbolConfig rows by exact managed-symbol membership before strict normalization
- ignores unrelated instruments, including non-ASCII display/API symbols, when they are outside the managed scope
- preserves strict Cross-margin and positive-leverage validation for every managed symbol
- retains active delivery-contract discovery and Position V3 margin-mode enrichment
- retains local two-line credential-file loading
- keeps order submission, amendment, cancellation, leverage and mode writes locked
- ships ASCII-only CRLF PowerShell installer and runner

## Credential file

The default Windows path is decoded from Base64 inside the runner. Line 1 is the API key and line 2 is the API secret.

## Safety boundary

The only state-changing HTTP methods allowed are listenKey lifecycle operations. Exchange order writes remain locked.
