from pandas import DataFrame

from freqtrade.strategy import IStrategy


class R23NoTradeStrategy(IStrategy):
    """Connectivity smoke strategy which can never emit an order signal."""

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "1m"
    process_only_new_candles = True
    startup_candle_count = 5
    minimal_roi = {"0": 100.0}
    stoploss = -0.99

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe
