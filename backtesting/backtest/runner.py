from __future__ import annotations
from concurrent.futures import ProcessPoolExecutor, as_completed
import pandas as pd
from tqdm import tqdm

from strategy.exits import build_exit_strategies, ExitStrategy
from .simulator import simulate_symbol
from .trade import Trade
from utils.logger import get_logger

log = get_logger(__name__)


def run_backtest(
    all_data: dict[str, pd.DataFrame],
    cfg,
    params: dict | None = None,
    condition_names: list[str] | None = None,
    exit_strategies: list[ExitStrategy] | None = None,
) -> list[Trade]:
    """
    Run backtest across the full universe. Returns flat list of all Trade objects.
    params overrides config entry params when provided (used during optimisation).
    """
    if params is None:
        params = _params_from_cfg(cfg)

    if exit_strategies is None:
        exit_strategies = build_exit_strategies(cfg)

    # Inject same_bar_assumption from config
    params.setdefault("same_bar_assumption", cfg.get("same_bar_assumption", "sl_first"))

    all_trades: list[Trade] = []

    for sym, df in tqdm(all_data.items(), desc="Backtesting", unit="sym"):
        trades = simulate_symbol(sym, df, exit_strategies, params, condition_names)
        all_trades.extend(trades)

    log.info(f"Total trades generated: {len(all_trades)}")
    return all_trades


def _params_from_cfg(cfg) -> dict:
    e = cfg.entry
    ea = cfg.exits.strategy_a
    eb = cfg.exits.strategy_b
    ec = cfg.exits.strategy_c
    ed = cfg.exits.strategy_d
    return {
        "rsi_min":              e.rsi_min,
        "rsi_max":              e.rsi_max,
        "volume_multiplier":    e.volume_multiplier,
        "breakout_period":      e.breakout_period,
        "adx_min":              e.adx_min,
        # Strategy A
        "target_pct":           ea.target_pct,
        "stop_loss_pct":        ea.stop_loss_pct,
        "max_days":             ea.max_days,
        # Strategy B
        "atr_multiplier":       eb.atr_multiplier,
        "initial_stop_atr_mult": eb.initial_stop_atr_mult,
        # Strategy C/D
        "max_days_cd":          ec.max_days,
    }
