from __future__ import annotations
import math
import numpy as np
import pandas as pd
from backtest.trade import Trade


def _returns(trades: list[Trade]) -> np.ndarray:
    return np.array([t.return_pct for t in trades])


# --------------------------------------------------------------------------- #
#  Overall metrics                                                             #
# --------------------------------------------------------------------------- #

def overall_metrics(trades: list[Trade]) -> dict:
    if not trades:
        return {}

    rets = _returns(trades)
    wins = [t for t in trades if t.is_win]
    losses = [t for t in trades if t.is_loss]

    win_rate = len(wins) / len(trades)
    loss_rate = len(losses) / len(trades)

    avg_gain = np.mean([t.return_pct for t in wins]) if wins else 0.0
    avg_loss = abs(np.mean([t.return_pct for t in losses])) if losses else 0.0

    gross_profit = sum(t.return_pct for t in wins) if wins else 0.0
    gross_loss = abs(sum(t.return_pct for t in losses)) if losses else 1e-9
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Expectancy = (win_rate × avg_gain) - (loss_rate × avg_loss)
    expectancy = (win_rate * avg_gain) - (loss_rate * avg_loss)

    # CAGR — use cumulative return assuming equal-weight 1-unit trades
    cum_ret = (rets / 100 + 1).prod()
    years = _years_span(trades)
    cagr = (cum_ret ** (1 / years) - 1) * 100 if years > 0 else 0.0

    # Max drawdown on equity curve
    equity = np.cumprod(1 + rets / 100)
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak * 100
    max_dd = abs(drawdown.min()) if len(drawdown) > 0 else 0.0

    avg_holding = np.mean([t.days_held for t in trades])
    avg_mfe = np.mean([t.mfe for t in trades])
    avg_mae = np.mean([t.mae for t in trades])

    exit_dist = pd.Series([t.exit_reason for t in trades]).value_counts().to_dict()

    return {
        "total_trades":    len(trades),
        "win_rate":        round(win_rate * 100, 2),
        "loss_rate":       round(loss_rate * 100, 2),
        "avg_gain_pct":    round(avg_gain, 3),
        "avg_loss_pct":    round(-avg_loss, 3),
        "profit_factor":   round(profit_factor, 3),
        "expectancy":      round(expectancy, 3),
        "cagr_pct":        round(cagr, 2),
        "max_drawdown_pct":round(max_dd, 2),
        "avg_holding_days":round(avg_holding, 1),
        "avg_mfe_pct":     round(avg_mfe, 2),
        "avg_mae_pct":     round(avg_mae, 2),
        "exit_distribution": exit_dist,
        "years_tested":    round(years, 1),
    }


# --------------------------------------------------------------------------- #
#  Per-stock metrics                                                           #
# --------------------------------------------------------------------------- #

def per_stock_metrics(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()

    rows = []
    by_sym: dict[str, list[Trade]] = {}
    for t in trades:
        by_sym.setdefault(t.symbol, []).append(t)

    for sym, sym_trades in by_sym.items():
        rets = _returns(sym_trades)
        wins = [t for t in sym_trades if t.is_win]
        rows.append({
            "symbol":        sym,
            "total_trades":  len(sym_trades),
            "win_rate":      round(len(wins) / len(sym_trades) * 100, 1),
            "avg_return":    round(rets.mean(), 3),
            "best_trade":    round(rets.max(), 2),
            "worst_trade":   round(rets.min(), 2),
            "avg_holding":   round(np.mean([t.days_held for t in sym_trades]), 1),
            "avg_mfe":       round(np.mean([t.mfe for t in sym_trades]), 2),
            "avg_mae":       round(np.mean([t.mae for t in sym_trades]), 2),
        })

    df = pd.DataFrame(rows).sort_values("win_rate", ascending=False)
    return df


# --------------------------------------------------------------------------- #
#  Per-exit-strategy metrics                                                   #
# --------------------------------------------------------------------------- #

def per_strategy_metrics(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()

    rows = []
    by_strat: dict[str, list[Trade]] = {}
    for t in trades:
        by_strat.setdefault(t.exit_strategy, []).append(t)

    for strat, strat_trades in by_strat.items():
        m = overall_metrics(strat_trades)
        m["exit_strategy"] = strat
        rows.append(m)

    return pd.DataFrame(rows).sort_values("profit_factor", ascending=False)


# --------------------------------------------------------------------------- #
#  Per-indicator contribution analysis                                         #
# --------------------------------------------------------------------------- #

def indicator_contribution(trades: list[Trade]) -> pd.DataFrame:
    """
    For each indicator, split trades into 'above/below' buckets and compare win rates.
    Shows which indicator values correlate with profitable trades.
    """
    if not trades:
        return pd.DataFrame()

    df = pd.DataFrame([t.to_dict() for t in trades])
    results = []

    checks = [
        ("RSI", "rsi", lambda v: v >= 60, "RSI >= 60", "RSI < 60"),
        ("ADX", "adx", lambda v: v >= 30, "ADX >= 30", "ADX < 30"),
        ("Vol_Ratio", "vol_ratio", lambda v: v >= 2.5, "VolRatio >= 2.5x", "VolRatio < 2.5x"),
        ("MACD_Hist", "macd_hist", lambda v: v > 0, "MACD Hist > 0", "MACD Hist <= 0"),
    ]

    for ind_name, col, cond, label_true, label_false in checks:
        if col not in df.columns:
            continue
        sub = df.dropna(subset=[col])
        if sub.empty:
            continue
        mask = sub[col].apply(cond)
        for bucket, label in [(mask, label_true), (~mask, label_false)]:
            bucket_df = sub[bucket]
            if len(bucket_df) < 5:
                continue
            wr = (bucket_df["return_pct"] > 0).mean() * 100
            ar = bucket_df["return_pct"].mean()
            results.append({
                "indicator": ind_name,
                "bucket":    label,
                "trades":    len(bucket_df),
                "win_rate":  round(wr, 1),
                "avg_return":round(ar, 3),
            })

    return pd.DataFrame(results)


# --------------------------------------------------------------------------- #
#  Monthly returns                                                             #
# --------------------------------------------------------------------------- #

def monthly_returns(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()

    df = pd.DataFrame([{"entry_date": t.entry_date, "return_pct": t.return_pct} for t in trades])
    df["month"] = pd.to_datetime(df["entry_date"]).dt.to_period("M")
    monthly = df.groupby("month")["return_pct"].agg(["mean", "count", "sum"])
    monthly.columns = ["avg_return", "trades", "total_return"]
    return monthly.reset_index()


# --------------------------------------------------------------------------- #
#  Edge check against targets                                                  #
# --------------------------------------------------------------------------- #

def check_edge(metrics: dict, filters: dict) -> dict:
    """Compare overall_metrics against the filter targets from config."""
    passed = {
        "win_rate_ok":      metrics.get("win_rate", 0) >= filters.get("win_rate_target", 55),
        "profit_factor_ok": metrics.get("profit_factor", 0) >= filters.get("profit_factor_target", 1.8),
        "avg_return_ok":    metrics.get("avg_gain_pct", 0) >= filters.get("avg_return_target", 4.0),
        "max_dd_ok":        metrics.get("max_drawdown_pct", 999) <= filters.get("max_drawdown_limit", 15.0),
        "holding_ok":       metrics.get("avg_holding_days", 999) <= filters.get("max_avg_holding_days", 10),
    }
    passed["all_targets_met"] = all(passed.values())
    return passed


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _years_span(trades: list[Trade]) -> float:
    if not trades:
        return 1.0
    dates = [t.entry_date for t in trades]
    span = (max(dates) - min(dates)).days
    return max(span / 365.25, 1.0 / 365)
