from __future__ import annotations
import pandas as pd
import pandas_ta as ta
import numpy as np

from utils.logger import get_logger

log = get_logger(__name__)

# Minimum rows needed before any indicator is meaningful
MIN_ROWS = 250


def add_indicators(df: pd.DataFrame, cfg) -> pd.DataFrame:
    """
    Add all technical indicators to an OHLCV DataFrame.
    Returns a new DataFrame with indicator columns appended.
    Rows with NaN indicators are NOT dropped here — strategy layer decides.
    """
    if len(df) < MIN_ROWS:
        log.debug(f"Too few rows ({len(df)}) to compute indicators reliably")

    df = df.copy()

    ind = cfg.indicators

    # --- EMAs ---
    for period in ind.ema_periods:
        df[f"EMA{period}"] = ta.ema(df["Close"], length=period)

    # --- RSI ---
    df["RSI"] = ta.rsi(df["Close"], length=ind.rsi_period)

    # --- MACD ---
    macd_cfg = ind.macd
    macd = ta.macd(
        df["Close"],
        fast=macd_cfg.fast,
        slow=macd_cfg.slow,
        signal=macd_cfg.signal,
    )
    if macd is not None and not macd.empty:
        df["MACD"] = macd.iloc[:, 0]          # MACD line
        df["MACD_Signal"] = macd.iloc[:, 2]   # Signal line
        df["MACD_Hist"] = macd.iloc[:, 1]     # Histogram
        # Bullish crossover: MACD crosses above signal
        df["MACD_Cross"] = (
            (df["MACD"] > df["MACD_Signal"]) &
            (df["MACD"].shift(1) <= df["MACD_Signal"].shift(1))
        )

    # --- ATR ---
    df["ATR"] = ta.atr(df["High"], df["Low"], df["Close"], length=ind.atr_period)

    # --- ADX ---
    adx_df = ta.adx(df["High"], df["Low"], df["Close"], length=ind.adx_period)
    if adx_df is not None and not adx_df.empty:
        df["ADX"] = adx_df.iloc[:, 0]
        df["DMP"] = adx_df.iloc[:, 1]
        df["DMN"] = adx_df.iloc[:, 2]

    # --- Bollinger Bands ---
    bb_cfg = ind.bollinger
    bb = ta.bbands(df["Close"], length=bb_cfg.period, std=bb_cfg.std_dev)
    if bb is not None and not bb.empty:
        df["BB_Lower"] = bb.iloc[:, 0]
        df["BB_Mid"] = bb.iloc[:, 1]
        df["BB_Upper"] = bb.iloc[:, 2]
        df["BB_Width"] = bb.iloc[:, 3]
        df["BB_Pct"] = bb.iloc[:, 4]   # %B: where Close sits within band

    # --- Volume SMA ---
    vol_period = ind.volume_sma_period
    df["Vol_SMA"] = df["Volume"].rolling(vol_period).mean()
    df["Vol_Ratio"] = df["Volume"] / df["Vol_SMA"]

    # --- Rolling VWAP (daily data approximation) ---
    # True intraday VWAP not possible with daily bars.
    # Rolling VWAP = sum(TP * V, window) / sum(V, window)
    vwap_period = ind.vwap_period
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    df["VWAP"] = (tp * df["Volume"]).rolling(vwap_period).sum() / df["Volume"].rolling(vwap_period).sum()

    # --- Rolling N-day high (for breakout detection) ---
    # Added dynamically by strategy layer since breakout_period is a tunable parameter.
    # Pre-compute common ones to avoid redundant work:
    for period in [10, 20, 30]:
        df[f"High{period}d"] = df["High"].shift(1).rolling(period).max()

    return df


def compute_indicators_bulk(
    all_data: dict[str, pd.DataFrame],
    cfg,
) -> dict[str, pd.DataFrame]:
    """Apply add_indicators to every stock in the universe."""
    result = {}
    for sym, df in all_data.items():
        try:
            result[sym] = add_indicators(df, cfg)
        except Exception as e:
            log.warning(f"Indicator calculation failed for {sym}: {e}")
    log.info(f"Indicators computed for {len(result)} symbols")
    return result
