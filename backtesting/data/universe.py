from __future__ import annotations
import io
import requests
import pandas as pd
from pathlib import Path

from utils.logger import get_logger

log = get_logger(__name__)

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Fallback: top Nifty 500 symbols hardcoded (subset) used only when NSE download fails.
# Generated from NSE index constituents as of 2025. Full 500 fetched dynamically.
_NIFTY500_FALLBACK = [
    "RELIANCE", "TCS", "HDFCBANK", "BHARTIARTL", "ICICIBANK", "INFOSYS",
    "SBIN", "HINDUNILVR", "ITC", "LT", "KOTAKBANK", "AXISBANK", "BAJFINANCE",
    "MARUTI", "HCLTECH", "SUNPHARMA", "TITAN", "ONGC", "POWERGRID", "NTPC",
    "ULTRACEMCO", "WIPRO", "BAJAJFINSV", "NESTLEIND", "JSWSTEEL", "TATAMOTORS",
    "ADANIENT", "ADANIPORTS", "TECHM", "COALINDIA", "INDUSINDBK", "DIVISLAB",
    "DRREDDY", "BPCL", "CIPLA", "HINDALCO", "GRASIM", "EICHERMOT", "TATASTEEL",
    "APOLLOHOSP", "BAJAJ_AUTO", "BRITANNIA", "SBILIFE", "HDFCLIFE", "TATACONSUM",
    "HEROMOTOCO", "UPL", "M&M", "SHREECEM", "ASIANPAINT",
]


def get_nifty500_symbols() -> list[str]:
    """Download Nifty 500 constituents from NSE. Falls back to hardcoded list."""
    url = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
    try:
        resp = requests.get(url, headers=_NSE_HEADERS, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        symbols = df["Symbol"].dropna().str.strip().tolist()
        log.info(f"Fetched {len(symbols)} Nifty 500 symbols from NSE")
        return symbols
    except Exception as e:
        log.warning(f"NSE download failed ({e}), using fallback list ({len(_NIFTY500_FALLBACK)} symbols)")
        return _NIFTY500_FALLBACK


def get_nifty100_symbols() -> list[str]:
    """Nifty 100 = Nifty 50 + Next 50. Used as base for 'top active' selection."""
    url = "https://archives.nseindia.com/content/indices/ind_nifty100list.csv"
    try:
        resp = requests.get(url, headers=_NSE_HEADERS, timeout=15)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        return df["Symbol"].dropna().str.strip().tolist()
    except Exception:
        return get_nifty500_symbols()[:100]


def get_top_active_symbols(
    all_data: dict[str, pd.DataFrame],
    n: int = 100,
    min_avg_volume: int = 50_000,
) -> list[str]:
    """
    Rank by average daily traded value (Close × Volume) and return top-n.
    Called after data is downloaded so no extra network request is needed.
    """
    records = []
    for sym, df in all_data.items():
        if df.empty or len(df) < 50:
            continue
        avg_val = (df["Close"] * df["Volume"]).mean()
        avg_vol = df["Volume"].mean()
        if avg_vol >= min_avg_volume:
            records.append((sym, avg_val))

    records.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in records[:n]]


def get_top_volatile_symbols(
    all_data: dict[str, pd.DataFrame],
    n: int = 50,
) -> list[str]:
    """
    Rank by annualised volatility of daily log returns and return top-n.
    High volatility = more swing trading opportunities (and risk).
    """
    import numpy as np

    records = []
    for sym, df in all_data.items():
        if df.empty or len(df) < 50:
            continue
        log_ret = np.log(df["Close"] / df["Close"].shift(1)).dropna()
        ann_vol = log_ret.std() * (252 ** 0.5) * 100  # annualised %
        records.append((sym, ann_vol))

    records.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in records[:n]]


def build_universe(cfg) -> list[str]:
    """Return deduplicated list of symbols to backtest based on config flags."""
    symbols: set[str] = set()

    if cfg.universe.nifty500:
        symbols.update(get_nifty500_symbols())

    log.info(f"Universe size before active/volatile filter: {len(symbols)}")
    return sorted(symbols)
