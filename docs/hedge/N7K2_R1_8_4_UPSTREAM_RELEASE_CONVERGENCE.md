# N7K2 R1.8.4 upstream release convergence

This hotfix closes the three remaining failures observed after all Hedge suites passed.

## Changes

1. `hedge-db` is loaded lazily from the CLI command package so normal `freqtrade -h` startup does not import SQLAlchemy and the H3 migration stack.
2. Telegram profit argument parsing now accepts only the documented `list[str]` shape. Truthy mocks and proxy objects no longer create an accidental one-day filter.
3. Security pins are advanced to `torch==2.13.0` and `pymdown-extensions==11.0.1`; the installer also refreshes `setuptools==83.0.0` before the security test.
4. The Windows installer captures native stderr separately so normal interpreter cleanup logging is not surfaced as a PowerShell `NativeCommandError`.

The release remains read-only/Paper-only with native exchange writes locked.
