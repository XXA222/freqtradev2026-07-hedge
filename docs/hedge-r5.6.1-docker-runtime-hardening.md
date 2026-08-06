# R5.6.1 Docker Runtime Hardening

See the repository root report `R5.6.1-DOCKER-RUNTIME-HARDENING-REPORT.md` for the complete remediation matrix.

## Safe Docker sequence

1. Verify or load a local Python 3.12 base image.
2. Run `scripts/Build-Freqtrade-Hedge-R5.6.1-Docker.ps1`.
3. Create/start the Compose service using `docker/docker-compose-hedge-r561.yml`.
4. The entrypoint writes a secure config only when needed, verifies the official schema, and starts Freqtrade.
5. Run `scripts/Run-Freqtrade-Hedge-R5.6.1-Docker-Preflight.ps1` with optional network and deterministic-scenario switches.
6. Collect a redacted evidence ZIP with `scripts/Run-Freqtrade-Hedge-R5.6.1-Docker-Evidence.ps1`.

The host Dashboard port is restricted to `127.0.0.1:8080`. No exchange credential is required or accepted by the safe template.
