from __future__ import annotations
from typing import Callable
import pandas as pd
import numpy as np

# A Condition takes (row, df, params) and returns bool.
# 'row' is a named tuple from df.itertuples(); 'df' is the full DataFrame.
Condition = Callable[[object, pd.DataFrame, dict], bool]


# --------------------------------------------------------------------------- #
#  Individual condition functions                                              #
# --------------------------------------------------------------------------- #

def cond_close_above_ema20(row, df: pd.DataFrame, params: dict) -> bool:
    return row.Close > row.EMA20

def cond_ema_aligned(row, df: pd.DataFrame, params: dict) -> bool:
    """EMA20 > EMA50 > EMA200 (bullish alignment)."""
    return row.EMA20 > row.EMA50 > row.EMA200

def cond_rsi_in_range(row, df: pd.DataFrame, params: dict) -> bool:
    rsi_min = params.get("rsi_min", 55)
    rsi_max = params.get("rsi_max", 70)
    return rsi_min <= row.RSI <= rsi_max

def cond_macd_crossover(row, df: pd.DataFrame, params: dict) -> bool:
    return bool(row.MACD_Cross)

def cond_volume_surge(row, df: pd.DataFrame, params: dict) -> bool:
    mult = params.get("volume_multiplier", 2.0)
    return row.Vol_Ratio >= mult

def cond_breakout_high(row, df: pd.DataFrame, params: dict) -> bool:
    """Close breaks above N-day rolling high (excluding today)."""
    period = params.get("breakout_period", 20)
    col = f"High{period}d"
    if col not in df.columns:
        # Compute on the fly if not pre-computed
        high_n = df["High"].shift(1).rolling(period).max()
        ref = high_n.loc[row.Index] if row.Index in high_n.index else np.nan
    else:
        ref = getattr(row, col, np.nan)
    if pd.isna(ref):
        return False
    return row.Close > ref

def cond_adx_strong(row, df: pd.DataFrame, params: dict) -> bool:
    adx_min = params.get("adx_min", 25)
    return row.ADX >= adx_min

def cond_close_above_vwap(row, df: pd.DataFrame, params: dict) -> bool:
    if pd.isna(row.VWAP):
        return True  # Don't penalise when VWAP not ready
    return row.Close > row.VWAP

def cond_above_bb_mid(row, df: pd.DataFrame, params: dict) -> bool:
    return row.Close > row.BB_Mid


# --------------------------------------------------------------------------- #
#  Condition registry                                                          #
# --------------------------------------------------------------------------- #

CONDITION_REGISTRY: dict[str, Condition] = {
    "close_above_ema20":  cond_close_above_ema20,
    "ema_aligned":        cond_ema_aligned,
    "rsi_in_range":       cond_rsi_in_range,
    "macd_crossover":     cond_macd_crossover,
    "volume_surge":       cond_volume_surge,
    "breakout_high":      cond_breakout_high,
    "adx_strong":         cond_adx_strong,
    "close_above_vwap":   cond_close_above_vwap,
    "above_bb_mid":       cond_above_bb_mid,
}


# --------------------------------------------------------------------------- #
#  Signal generator                                                            #
# --------------------------------------------------------------------------- #

DEFAULT_CONDITIONS = [
    "close_above_ema20",
    "ema_aligned",
    "rsi_in_range",
    "macd_crossover",
    "volume_surge",
    "breakout_high",
    "adx_strong",
]


def generate_signals(
    df: pd.DataFrame,
    params: dict,
    condition_names: list[str] | None = None,
) -> pd.Series:
    """
    Return a boolean Series (indexed like df) where True = entry signal.
    Skips rows where any required indicator column is NaN.
    """
    if condition_names is None:
        condition_names = DEFAULT_CONDITIONS

    conditions = [CONDITION_REGISTRY[n] for n in condition_names]

    # Required columns — skip rows where these are NaN
    required = ["EMA20", "EMA50", "EMA200", "RSI", "MACD", "ADX", "ATR", "Vol_SMA"]
    valid_mask = df[required].notna().all(axis=1)

    signals = pd.Series(False, index=df.index)

    for i, row in enumerate(df.itertuples()):
        if not valid_mask.iloc[i]:
            continue
        if all(c(row, df, params) for c in conditions):
            signals.iloc[i] = True

    return signals
