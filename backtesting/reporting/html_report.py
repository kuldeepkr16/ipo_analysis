from __future__ import annotations
from pathlib import Path
import json
import pandas as pd
import plotly.io as pio

from backtest.trade import Trade
from metrics.performance import overall_metrics, per_stock_metrics, per_strategy_metrics, indicator_contribution, check_edge
from reporting.charts import (
    equity_curve, monthly_heatmap, return_distribution,
    trade_duration_chart, strategy_comparison, best_worst_stocks, mfe_mae_scatter,
)
from utils.logger import get_logger

log = get_logger(__name__)


def _fig_html(fig) -> str:
    return pio.to_html(fig, full_html=False, include_plotlyjs=False)


def _metric_card(label: str, value, suffix: str = "", color: str = "") -> str:
    style = f"color:{color};" if color else ""
    return f"""
    <div class="metric-card">
      <div class="metric-label">{label}</div>
      <div class="metric-value" style="{style}">{value}{suffix}</div>
    </div>"""


def _edge_badge(ok: bool, label: str) -> str:
    cls = "badge-ok" if ok else "badge-fail"
    icon = "✓" if ok else "✗"
    return f'<span class="badge {cls}">{icon} {label}</span>'


def build_report(
    trades: list[Trade],
    cfg,
    opt_df: pd.DataFrame | None = None,
    out_path: str | Path = "output/report.html",
) -> None:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    m = overall_metrics(trades)
    ps = per_stock_metrics(trades)
    pst = per_strategy_metrics(trades)
    ind = indicator_contribution(trades)
    edge = check_edge(m, cfg.raw().get("filters", {}))

    fig_equity   = _fig_html(equity_curve(trades))
    fig_heatmap  = _fig_html(monthly_heatmap(trades))
    fig_dist     = _fig_html(return_distribution(trades))
    fig_duration = _fig_html(trade_duration_chart(trades))
    fig_strat    = _fig_html(strategy_comparison(trades))
    fig_stocks   = _fig_html(best_worst_stocks(ps))
    fig_scatter  = _fig_html(mfe_mae_scatter(trades))

    def df_table(df: pd.DataFrame, max_rows: int = 50) -> str:
        if df.empty:
            return "<p>No data</p>"
        return df.head(max_rows).to_html(index=False, classes="data-table", border=0)

    # Edge badges
    edge_badges = "".join([
        _edge_badge(edge["win_rate_ok"],      f"Win Rate > {cfg.filters.win_rate_target}%"),
        _edge_badge(edge["profit_factor_ok"], f"PF > {cfg.filters.profit_factor_target}"),
        _edge_badge(edge["avg_return_ok"],    f"Avg Return > {cfg.filters.avg_return_target}%"),
        _edge_badge(edge["max_dd_ok"],        f"Max DD < {cfg.filters.max_drawdown_limit}%"),
        _edge_badge(edge["holding_ok"],       f"Avg Hold < {cfg.filters.max_avg_holding_days}d"),
    ])

    edge_conclusion = (
        '<p class="verdict-pass">✓ A statistically valid edge was identified. '
        'These conditions consistently produced profitable short-term trades.</p>'
        if edge["all_targets_met"] else
        '<p class="verdict-fail">✗ No single strategy met all five targets simultaneously. '
        'Review the optimisation table for closest combinations.</p>'
    )

    opt_table = ""
    if opt_df is not None and not opt_df.empty:
        opt_table = f"<h2>Optimisation Results (Top {len(opt_df)})</h2>" + df_table(opt_df, 100)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Swing Trading Backtest Report</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #c9d1d9; font-size: 14px; line-height: 1.5; }}
  .hdr {{ background: #161b22; border-bottom: 1px solid #30363d;
          padding: 20px 32px; display: flex; align-items: baseline; gap: 16px; }}
  .hdr h1 {{ font-size: 20px; font-weight: 700; color: #f0f6fc; }}
  .hdr span {{ font-size: 12px; color: #8b949e; }}
  .main {{ max-width: 1400px; margin: 0 auto; padding: 28px 32px; }}
  h2 {{ font-size: 15px; font-weight: 600; color: #f0f6fc; margin: 32px 0 14px;
       border-left: 3px solid #1f6feb; padding-left: 10px; }}
  .metric-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; margin-bottom: 8px; }}
  .metric-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  padding: 14px 16px; }}
  .metric-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: .07em; color: #8b949e; margin-bottom: 4px; }}
  .metric-value {{ font-size: 20px; font-weight: 700; color: #f0f6fc; font-variant-numeric: tabular-nums; }}
  .edge-bar {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }}
  .badge {{ padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }}
  .badge-ok   {{ background: rgba(35,134,54,.25); color: #3fb950; border: 1px solid #238636; }}
  .badge-fail {{ background: rgba(248,81,73,.15); color: #f85149; border: 1px solid #da3633; }}
  .verdict-pass {{ color: #3fb950; font-weight: 600; margin: 12px 0; }}
  .verdict-fail {{ color: #f85149; font-weight: 600; margin: 12px 0; }}
  .chart-wrap {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; margin-bottom: 20px; overflow: hidden; }}
  .chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .data-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  .data-table th {{ background: #21262d; color: #8b949e; padding: 8px 10px; text-align: left;
                    font-size: 10px; text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid #30363d; }}
  .data-table td {{ padding: 7px 10px; border-bottom: 1px solid #21262d; color: #c9d1d9; }}
  .data-table tr:hover td {{ background: #21262d; }}
  .table-wrap {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                 overflow-x: auto; margin-bottom: 20px; }}
</style>
</head>
<body>
<div class="hdr">
  <h1>Swing Trading Backtest Report</h1>
  <span>{m.get('years_tested', '—')} years · {m.get('total_trades', 0):,} trades · Nifty 500 + Active + Volatile</span>
</div>
<div class="main">

<h2>Overall Performance</h2>
<div class="metric-grid">
  {_metric_card("Total Trades",   f"{m.get('total_trades',0):,}")}
  {_metric_card("Win Rate",       f"{m.get('win_rate',0):.1f}",   "%",
                "#3fb950" if m.get('win_rate',0)>=55 else "#f85149")}
  {_metric_card("Avg Gain",       f"+{m.get('avg_gain_pct',0):.2f}", "%", "#3fb950")}
  {_metric_card("Avg Loss",       f"{m.get('avg_loss_pct',0):.2f}", "%", "#f85149")}
  {_metric_card("Profit Factor",  f"{m.get('profit_factor',0):.2f}",
                color="#3fb950" if m.get('profit_factor',0)>=1.8 else "#f85149")}
  {_metric_card("Expectancy",     f"{m.get('expectancy',0):.2f}", "%")}
  {_metric_card("CAGR",           f"{m.get('cagr_pct',0):.1f}", "%")}
  {_metric_card("Max Drawdown",   f"{m.get('max_drawdown_pct',0):.1f}", "%", "#f85149")}
  {_metric_card("Avg Hold",       f"{m.get('avg_holding_days',0):.1f}", " days")}
  {_metric_card("Avg MFE",        f"{m.get('avg_mfe_pct',0):.1f}", "%", "#3fb950")}
  {_metric_card("Avg MAE",        f"{m.get('avg_mae_pct',0):.1f}", "%", "#f85149")}
</div>

<h2>Edge Assessment</h2>
<div class="edge-bar">{edge_badges}</div>
{edge_conclusion}

<h2>Equity Curve & Drawdown</h2>
<div class="chart-wrap">{fig_equity}</div>

<div class="chart-row">
  <div class="chart-wrap">{fig_heatmap}</div>
  <div class="chart-wrap">{fig_dist}</div>
</div>

<div class="chart-row">
  <div class="chart-wrap">{fig_strat}</div>
  <div class="chart-wrap">{fig_duration}</div>
</div>

<div class="chart-row">
  <div class="chart-wrap">{fig_stocks}</div>
  <div class="chart-wrap">{fig_scatter}</div>
</div>

<h2>Exit Strategy Breakdown</h2>
<div class="table-wrap">{df_table(pst)}</div>

<h2>Indicator Contribution Analysis</h2>
<div class="table-wrap">{df_table(ind)}</div>

<h2>Per-Stock Performance (Top 50)</h2>
<div class="table-wrap">{df_table(ps, 50)}</div>

{opt_table}

</div>
</body>
</html>"""

    Path(out_path).write_text(html, encoding="utf-8")
    log.info(f"HTML report saved → {out_path}")
