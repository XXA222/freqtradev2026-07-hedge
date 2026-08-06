# N7K2 R1.6 P0 contract convergence

R1.6 resolves the three P0 regressions exposed by the Windows full validation run.

- Durable paper recovery now explicitly verifies that still-open grid orders survive restart with the same lifecycle and approved quantity.
- API route registration tests use OpenAPI and router matching, compatible with FastAPI 0.137 lazy included-router trees.
- `bootstrap_hedge_persistence` is restored as a backward-compatible, explicit low-level H3 migration and recovery entry point.

No real exchange write path is enabled.
