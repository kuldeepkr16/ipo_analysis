from __future__ import annotations
from pathlib import Path
import pandas as pd

from backtest.trade import Trade
from utils.logger import get_logger

log = get_logger(__name__)


def export_trades_csv(trades: list[Trade], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([t.to_dict() for t in trades])
    df.to_csv(path, index=False)
    log.info(f"Trades CSV saved → {path}  ({len(df)} rows)")


def export_summary_csv(metrics: dict, per_stock: pd.DataFrame, per_strategy: pd.DataFrame, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    rows = [{"metric": k, "value": v} for k, v in metrics.items() if not isinstance(v, dict)]
    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(path, index=False)
    log.info(f"Summary CSV saved → {path}")

    stem = Path(path).stem
    parent = Path(path).parent

    ps_path = parent / f"{stem}_per_stock.csv"
    per_stock.to_csv(ps_path, index=False)
    log.info(f"Per-stock CSV saved → {ps_path}")

    pst_path = parent / f"{stem}_per_strategy.csv"
    per_strategy.to_csv(pst_path, index=False)
    log.info(f"Per-strategy CSV saved → {pst_path}")


def export_optimization_csv(opt_df: pd.DataFrame, path: str | Path) -> None:
    if opt_df.empty:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    opt_df.to_csv(path, index=False)
    log.info(f"Optimisation results saved → {path}  ({len(opt_df)} rows)")
