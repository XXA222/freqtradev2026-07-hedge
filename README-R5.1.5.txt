Freqtrade-Hedge R5.1.5 Mainnet Read-only Deep Contract Audit

Version: 5.1.5
Delivery: 20260804-2040-A7C9
Safety: all exchange order endpoints remain locked
Installer network access: none
Online runner: authenticated GET-only REST plus listenKey lifecycle

R5.1.5 corrects the complete read-only/promotion contract:
- disabled Futures TRADE permission no longer fails a successful read-only observation;
- key policy and existing account exposure independently block ORDER_TEST promotion;
- MAINNET_READONLY evidence can be refreshed after account or key-policy changes;
- prior completed read-only runs retain immutable replay evidence;
- ORDER_TEST requires the latest fresh promotion-ready read-only evidence;
- R2.4, R2.5, live accountAlias, API key, and R5 evidence are cryptographically bound;
- R5.1 does not copy credentials into the project tree;
- signed GET retries generate a new timestamp and signature;
- PowerShell 5.1 captures native stdout/stderr separately and returns scalar exit codes;
- every diagnostic test section continues after another section fails.
