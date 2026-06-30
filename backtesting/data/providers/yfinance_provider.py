from __future__ import annotations
import time
from datetime import date
import pandas as pd
import yfinance as yf

from .base import DataProvider
from utils.logger import get_logger

log = get_logger(__name__)


class YFinanceProvider(DataProvider):
    """Downloads daily OHLCV data from Yahoo Finance."""

    def __init__(self, delay: float = 0.3, retries: int = 3) -> None:
        self._delay = delay
        self._retries = retries

    def name(self) -> str:
        return "yfinance"

    def download(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        ticker = self._to_yf_symbol(symbol)
        for attempt in range(1, self._retries + 1):
            try:
                df = yf.download(
                    ticker,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    progress=False,
                    auto_adjust=True,
                    actions=False,
                )
                if df.empty:
                    log.warning(f"[yellow]No data for {ticker}[/]")
                    return pd.DataFrame()

                # yfinance >=0.2.18 returns MultiIndex columns when single ticker
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)

                df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                df.index = pd.to_datetime(df.index).tz_localize(None)
                df.dropna(inplace=True)
                time.sleep(self._delay)
                return df

            except Exception as exc:
                log.warning(f"Attempt {attempt}/{self._retries} failed for {ticker}: {exc}")
                if attempt < self._retries:
                    time.sleep(2 ** attempt)

        return pd.DataFrame()

    @staticmethod
    def _to_yf_symbol(symbol: str) -> str:
        """Append .NS suffix for NSE stocks if not already present."""
        symbol = symbol.upper().strip()
        if not symbol.endswith(".NS") and not symbol.endswith(".BO"):
            return f"{symbol}.NS"
        return symbol
