# R3.8.1 Windows Single-Instance Lock Hotfix

R3.8.1 retains the R3.8 production runtime supervisor and corrects Windows lock contention. Contenders acquire byte zero before reading or writing lock identity data, and Windows sharing violations are normalized to InstanceLockError. The installer validates the runtime but does not start the bot, load credentials, contact Binance, or send orders.
