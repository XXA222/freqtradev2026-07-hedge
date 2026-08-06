"""Install the R2.4 CCXT write lock before guarded process imports."""

from r24_toolkit.write_lock import install

INSTALL_RESULT = install()
