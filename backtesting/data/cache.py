from __future__ import annotations
from datetime import date
from pathlib import Path
import pandas as pd

from utils.logger import get_logger

log = get_logger(__name__)


class DataCache:
    """Parquet-based disk cache for OHLCV data."""

    def __init__(self, cache_dir: str | Path, fmt: str = "parquet") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fmt = fmt

    def _path(self, symbol: str) -> Path:
        safe = symbol.replace("/", "_").replace(".", "_")
        ext = "parquet" if self._fmt == "parquet" else "csv"
        return self._dir / f"{safe}.{ext}"

    def exists(self, symbol: str, required_start: date) -> bool:
        p = self._path(symbol)
        if not p.exists():
            return False
        try:
            df = self._load(p)
            if df.empty:
                return False
            return df.index.min().date() <= required_start
        except Exception:
            return False

    def load(self, symbol: str) -> pd.DataFrame:
        p = self._path(symbol)
        if not p.exists():
            return pd.DataFrame()
        try:
            return self._load(p)
        except Exception as e:
            log.warning(f"Cache read failed for {symbol}: {e}")
            return pd.DataFrame()

    def save(self, symbol: str, df: pd.DataFrame) -> None:
        if df.empty:
            return
        p = self._path(symbol)
        try:
            if self._fmt == "parquet":
                df.to_parquet(p)
            else:
                df.to_csv(p)
        except Exception as e:
            log.warning(f"Cache write failed for {symbol}: {e}")

    def _load(self, path: Path) -> pd.DataFrame:
        if self._fmt == "parquet":
            df = pd.read_parquet(path)
        else:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df
