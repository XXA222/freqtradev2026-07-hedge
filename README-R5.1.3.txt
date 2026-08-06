R5.1.3 fixes PowerShell 5.1 native-command exit-code capture in the mainnet read-only runner.
Python stdout/stderr is streamed to the console and UTF-8 log without contaminating the function return value.
Workspace preparation, user-stream observation, R5 read-only stage, and final acceptance now compare a scalar integer exit code.
Mainnet exchange order writes remain locked and the installer remains offline.
