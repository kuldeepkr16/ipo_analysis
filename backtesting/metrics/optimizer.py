from __future__ import annotations
import itertools
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial
import pandas as pd
from tqdm import tqdm

from backtest.simulator import simulate_symbol
from strategy.exits import StrategyA
from metrics.performance import overall_metrics
from utils.logger import get_logger

log = get_logger(__name__)


def _build_param_grid(cfg) -> list[dict]:
    opt = cfg.optimization
    grid = list(itertools.product(
        opt.rsi_ranges,
        opt.volume_multipliers,
        opt.breakout_periods,
        opt.ema_sets,
        opt.targets_pct,
        opt.stop_losses_pct,
    ))
    params_list = []
    for rsi_range, vol_mult, bp, ema_set, tgt, sl in grid:
        params_list.append({
            "rsi_min":           rsi_range[0],
            "rsi_max":           rsi_range[1],
            "volume_multiplier": vol_mult,
            "breakout_period":   bp,
            "ema_periods":       ema_set,
            "target_pct":        tgt,
            "stop_loss_pct":     sl,
            "max_days":          10,
            "adx_min":           25,
            "same_bar_assumption": cfg.get("same_bar_assumption", "sl_first"),
        })
    return params_list


def _run_single_combo(args) -> dict | None:
    """Worker function — runs one param combo across all symbols."""
    all_data, params, min_trades = args
    strat = StrategyA()
    trades = []
    for sym, df in all_data.items():
        trades.extend(simulate_symbol(sym, df, [strat], params))

    if len(trades) < min_trades:
        return None

    m = overall_metrics(trades)
    return {**params, **m}


def run_optimization(
    all_data: dict[str, pd.DataFrame],
    cfg,
) -> pd.DataFrame:
    """
    Grid search over all parameter combinations.
    Returns a ranked DataFrame of results.
    """
    if not cfg.optimization.enabled:
        log.info("Optimisation disabled in config.")
        return pd.DataFrame()

    param_grid = _build_param_grid(cfg)
    log.info(f"Optimisation: {len(param_grid)} parameter combinations to test")

    min_trades = cfg.filters.min_trades_for_stats
    max_workers = cfg.optimization.max_workers

    results = []
    args = [(all_data, p, min_trades) for p in param_grid]

    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_run_single_combo, a): a for a in args}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Optimising"):
            res = fut.result()
            if res is not None:
                results.append(res)

    if not results:
        log.warning("No parameter combinations produced enough trades.")
        return pd.DataFrame()

    df = pd.DataFrame(results)

    # Rank by composite score: profit_factor × win_rate - max_drawdown
    df["score"] = (
        df["profit_factor"] * df["win_rate"] / 100
        - df["max_drawdown_pct"] / 100
    )
    df = df.sort_values("score", ascending=False)

    top_n = cfg.optimization.top_n_results
    return df.head(top_n).reset_index(drop=True)
