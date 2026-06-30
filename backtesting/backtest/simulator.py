from __future__ import annotations
from datetime import timedelta
import pandas as pd

from strategy.conditions import generate_signals
from strategy.exits import ExitStrategy, ExitResult
from .trade import Trade
from utils.logger import get_logger

log = get_logger(__name__)


def simulate_symbol(
    symbol: str,
    df: pd.DataFrame,
    exit_strategies: list[ExitStrategy],
    params: dict,
    condition_names: list[str] | None = None,
) -> list[Trade]:
    """
    Generate signals for one symbol and simulate all exit strategies.
    Returns one Trade per (signal × exit_strategy).
    """
    if df.empty or len(df) < 60:
        return []

    signals = generate_signals(df, params, condition_names)
    signal_indices = [i for i, v in enumerate(signals) if v]

    if not signal_indices:
        return []

    trades: list[Trade] = []

    for sig_idx in signal_indices:
        # Entry is next day's open
        entry_idx = sig_idx + 1
        if entry_idx >= len(df):
            continue

        entry_row = df.iloc[entry_idx]
        entry_price = entry_row["Open"]

        if entry_price <= 0 or pd.isna(entry_price):
            continue

        sig_row = df.iloc[sig_idx]
        signal_date = df.index[sig_idx].date()
        entry_date = df.index[entry_idx].date()

        for strat in exit_strategies:
            try:
                result: ExitResult = strat.evaluate(entry_price, entry_idx, df, params)
            except Exception as e:
                log.debug(f"Exit eval error {symbol}/{strat.name()}: {e}")
                continue

            if result.days_held == 0:
                continue

            ret_pct = (result.exit_price - entry_price) / entry_price * 100
            abs_pnl = result.exit_price - entry_price  # per share (1 unit)

            # Exit date
            exit_idx = entry_idx + result.days_held
            if exit_idx < len(df):
                exit_date = df.index[exit_idx].date()
            else:
                exit_date = df.index[-1].date()

            trade = Trade(
                symbol=symbol,
                signal_date=signal_date,
                entry_date=entry_date,
                entry_price=entry_price,
                exit_date=exit_date,
                exit_price=result.exit_price,
                exit_strategy=strat.name(),
                exit_reason=result.exit_reason,
                days_held=result.days_held,
                return_pct=ret_pct,
                abs_pnl=abs_pnl,
                mfe=result.mfe,
                mae=result.mae,
                highest_price=result.highest_price,
                lowest_price=result.lowest_price,
                target_hit=result.exit_reason == "TARGET",
                stop_hit=result.exit_reason in ("STOP_LOSS", "TRAILING_STOP"),
                rsi=_safe(sig_row, "RSI"),
                adx=_safe(sig_row, "ADX"),
                vol_ratio=_safe(sig_row, "Vol_Ratio"),
                macd_hist=_safe(sig_row, "MACD_Hist"),
                ema20=_safe(sig_row, "EMA20"),
                ema50=_safe(sig_row, "EMA50"),
                ema200=_safe(sig_row, "EMA200"),
                atr=_safe(sig_row, "ATR"),
                params=params.copy(),
            )
            trades.append(trade)

    return trades


def _safe(row: pd.Series, col: str):
    v = row.get(col)
    if v is None or pd.isna(v):
        return None
    return round(float(v), 4)
