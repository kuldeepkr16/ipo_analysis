from __future__ import annotations
from datetime import date
import pandas as pd

from .base import DataProvider


class KiteProvider(DataProvider):
    """
    Zerodha Kite Connect data provider stub.
    Implement this when switching from yfinance to live Kite data.

    Usage:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key="your_key")
        kite.set_access_token("your_token")
        provider = KiteProvider(kite)
    """

    def __init__(self, kite_client=None) -> None:
        if kite_client is None:
            raise NotImplementedError(
                "KiteProvider requires an authenticated KiteConnect client. "
                "Install kiteconnect and pass a KiteConnect instance."
            )
        self._kite = kite_client

    def name(self) -> str:
        return "kite"

    def download(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        # Kite API: instruments lookup → token → historical_data()
        # instrument_token = self._kite.ltp(f"NSE:{symbol}")[f"NSE:{symbol}"]["instrument_token"]
        # data = self._kite.historical_data(instrument_token, start, end, "day")
        # df = pd.DataFrame(data).set_index("date")[["open","high","low","close","volume"]]
        # df.columns = ["Open","High","Low","Close","Volume"]
        # return df
        raise NotImplementedError("KiteProvider.download() not yet implemented.")
