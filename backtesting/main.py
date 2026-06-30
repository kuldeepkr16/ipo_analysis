"""
Swing Trading Backtesting Framework
Entry point — run: python main.py [--optimize] [--refresh-data]
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

# Ensure project root on path so sibling packages resolve correctly
sys.path.insert(0, str(Path(__file__).parent))

from utils.config_loader import Config
from utils.logger import get_logger
from data.universe import build_universe, get_top_active_symbols, get_top_volatile_symbols
from data.downloader import download_universe
from indicators.calculator import compute_indicators_bulk
from strategy.exits import build_exit_strategies
from backtest.runner import run_backtest
from metrics.performance import (
    overall_metrics, per_stock_metrics, per_strategy_metrics,
    indicator_contribution, check_edge,
)
from reporting.exporter import export_trades_csv, export_summary_csv, export_optimization_csv
from reporting.html_report import build_report
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()
log = get_logger("main")


def print_summary(m: dict, edge: dict) -> None:
    tbl = Table(title="Backtest Summary", box=box.ROUNDED, style="dim")
    tbl.add_column("Metric", style="cyan")
    tbl.add_column("Value", justify="right")

    def row(label, val, ok=None):
        colour = "" if ok is None else ("green" if ok else "red")
        tbl.add_row(label, f"[{colour}]{val}[/]" if colour else str(val))

    row("Total Trades",    f"{m.get('total_trades', 0):,}")
    row("Win Rate",        f"{m.get('win_rate', 0):.1f}%",    edge.get("win_rate_ok"))
    row("Avg Gain",        f"+{m.get('avg_gain_pct', 0):.2f}%", edge.get("avg_return_ok"))
    row("Avg Loss",        f"{m.get('avg_loss_pct', 0):.2f}%")
    row("Profit Factor",   f"{m.get('profit_factor', 0):.3f}", edge.get("profit_factor_ok"))
    row("Expectancy",      f"{m.get('expectancy', 0):.3f}%")
    row("CAGR",            f"{m.get('cagr_pct', 0):.1f}%")
    row("Max Drawdown",    f"{m.get('max_drawdown_pct', 0):.1f}%", edge.get("max_dd_ok"))
    row("Avg Hold",        f"{m.get('avg_holding_days', 0):.1f}d", edge.get("holding_ok"))
    row("Years Tested",    f"{m.get('years_tested', 0):.1f}")

    console.print(tbl)

    if edge["all_targets_met"]:
        console.print("\n[bold green]✓ EDGE FOUND — Strategy meets all targets.[/bold green]")
    else:
        console.print("\n[bold red]✗ NO CONSISTENT EDGE — Strategy does not meet all targets.[/bold red]")
        failed = [k for k, v in edge.items() if k != "all_targets_met" and not v]
        console.print(f"  Failed: {', '.join(failed)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing Trading Backtester")
    parser.add_argument("--config",       default="config/config.yaml", help="Path to config file")
    parser.add_argument("--optimize",     action="store_true", help="Run parameter optimisation")
    parser.add_argument("--refresh-data", action="store_true", help="Force re-download (ignore cache)")
    parser.add_argument("--symbols",      nargs="*", help="Override universe with specific symbols")
    args = parser.parse_args()

    cfg = Config(args.config)
    log.info(f"Config loaded from {args.config}")

    out_dir = Path(cfg.output.dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    #  1. Universe                                                         #
    # ------------------------------------------------------------------ #
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
        log.info(f"Using {len(symbols)} user-supplied symbols")
    else:
        symbols = build_universe(cfg)

    # ------------------------------------------------------------------ #
    #  2. Download OHLCV                                                   #
    # ------------------------------------------------------------------ #
    t0 = time.time()
    all_data = download_universe(symbols, cfg, force_refresh=args.refresh_data)

    # Extend universe with top-active and top-volatile computed from data
    if not args.symbols:
        active = get_top_active_symbols(all_data, n=cfg.universe.top_active)
        volatile = get_top_volatile_symbols(all_data, n=cfg.universe.top_volatile)
        extra = set(active + volatile) - set(all_data.keys())
        if extra:
            log.info(f"Adding {len(extra)} extra active/volatile symbols")
            extra_data = download_universe(list(extra), cfg, force_refresh=False)
            all_data.update(extra_data)

    log.info(f"Universe: {len(all_data)} symbols  ({time.time()-t0:.0f}s)")

    # ------------------------------------------------------------------ #
    #  3. Indicators                                                       #
    # ------------------------------------------------------------------ #
    all_data = compute_indicators_bulk(all_data, cfg)

    # ------------------------------------------------------------------ #
    #  4. Backtest                                                         #
    # ------------------------------------------------------------------ #
    exit_strategies = build_exit_strategies(cfg)
    trades = run_backtest(all_data, cfg, exit_strategies=exit_strategies)

    if not trades:
        console.print("[bold red]No trades generated. Check entry conditions or data.[/bold red]")
        return

    # ------------------------------------------------------------------ #
    #  5. Metrics                                                          #
    # ------------------------------------------------------------------ #
    m    = overall_metrics(trades)
    ps   = per_stock_metrics(trades)
    pst  = per_strategy_metrics(trades)
    edge = check_edge(m, cfg.raw().get("filters", {}))

    print_summary(m, edge)

    # ------------------------------------------------------------------ #
    #  6. Optimisation (optional)                                          #
    # ------------------------------------------------------------------ #
    opt_df = None
    if args.optimize or cfg.optimization.enabled:
        from metrics.optimizer import run_optimization
        opt_df = run_optimization(all_data, cfg)
        if not opt_df.empty:
            export_optimization_csv(opt_df, out_dir / cfg.output.optimization_csv)
            console.print(f"\n[cyan]Top optimised combination:[/cyan]")
            console.print(opt_df.head(1).to_string())

    # ------------------------------------------------------------------ #
    #  7. Export                                                           #
    # ------------------------------------------------------------------ #
    export_trades_csv(trades, out_dir / cfg.output.trades_csv)
    export_summary_csv(m, ps, pst, out_dir / cfg.output.summary_csv)
    build_report(trades, cfg, opt_df=opt_df, out_path=out_dir / cfg.output.html_report)

    console.print(f"\n[bold green]Done.[/bold green] Outputs in [cyan]{out_dir}/[/cyan]")


if __name__ == "__main__":
    main()
