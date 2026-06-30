from __future__ import annotations
from datetime import date, timedelta
from pathlib import Path
import pandas as pd
from tqdm import tqdm

from .cache import DataCache
from .providers.base import DataProvider
from .providers.yfinance_provider import YFinanceProvider
from utils.logger import get_logger

log = get_logger(__name__)


def get_provider(cfg) -> DataProvider:
    name = cfg.data.provider
    if name == "yfinance":
        return YFinanceProvider(
            delay=cfg.data.request_delay_sec,
            retries=cfg.data.max_retries,
        )
    if name == "kite":
        from .providers.kite_provider import KiteProvider
        return KiteProvider()
    raise ValueError(f"Unknown provider: {name}")


def download_universe(
    symbols: list[str],
    cfg,
    force_refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    Download OHLCV data for all symbols.
    Returns dict[symbol -> DataFrame]. Symbols with no data are excluded.
    """
    years = cfg.data.years
    end = date.today()
    start = end - timedelta(days=int(years * 365.25))

    cache = DataCache(
        Path(cfg.data.cache_dir),
        fmt=cfg.data.cache_format,
    )
    provider = get_provider(cfg)

    log.info(f"Downloading {len(symbols)} symbols via {provider.name()} | {start} → {end}")
    results: dict[str, pd.DataFrame] = {}

    for sym in tqdm(symbols, desc="Downloading", unit="sym"):
        if not force_refresh and cache.exists(sym, start):
            df = cache.load(sym)
        else:
            df = provider.download(sym, start, end)
            if not df.empty:
                cache.save(sym, df)

        if df.empty:
            continue

        # Apply minimum volume filter
        min_vol = cfg.universe.min_avg_volume
        if df["Volume"].mean() < min_vol:
            log.debug(f"Skipping {sym}: avg volume below {min_vol}")
            continue

        # Apply penny stock filter
        min_price = cfg.universe.exclude_penny_below
        if df["Close"].mean() < min_price:
            log.debug(f"Skipping {sym}: avg price below ₹{min_price}")
            continue

        # Trim to requested window (cache may hold older data)
        df = df[df.index >= pd.Timestamp(start)]
        results[sym] = df

    log.info(f"Successfully loaded data for {len(results)}/{len(symbols)} symbols")
    return results
