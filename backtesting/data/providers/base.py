from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import date
import pandas as pd


class DataProvider(ABC):
    """Abstract OHLCV data provider. Swap implementations to change data source."""

    @abstractmethod
    def download(
        self,
        symbol: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """
        Return a DataFrame with columns: Open, High, Low, Close, Volume.
        Index must be DatetimeIndex (UTC or tz-naive, daily frequency).
        Returns empty DataFrame on failure.
        """
        ...

    @abstractmethod
    def name(self) -> str:
        """Human-readable provider name for logging."""
        ...
