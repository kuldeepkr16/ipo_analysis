from __future__ import annotations
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

from backtest.trade import Trade
from metrics.performance import monthly_returns


# Colour palette
C_GREEN  = "#26a69a"
C_RED    = "#ef5350"
C_BLUE   = "#42a5f5"
C_YELLOW = "#ffa726"
C_GREY   = "#90a4ae"


def equity_curve(trades: list[Trade]) -> go.Figure:
    if not trades:
        return go.Figure()

    sorted_t = sorted(trades, key=lambda t: t.entry_date)
    rets = np.array([t.return_pct / 100 for t in sorted_t])
    equity = np.cumprod(1 + rets)
    dates = [t.entry_date for t in sorted_t]

    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak * 100

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.7, 0.3], vertical_spacing=0.03)

    fig.add_trace(go.Scatter(
        x=dates, y=equity,
        mode="lines", name="Equity",
        line=dict(color=C_BLUE, width=2),
        fill="tozeroy", fillcolor="rgba(66,165,245,0.1)",
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=dates, y=dd,
        mode="lines", name="Drawdown %",
        line=dict(color=C_RED, width=1.5),
        fill="tozeroy", fillcolor="rgba(239,83,80,0.15)",
    ), row=2, col=1)

    fig.update_layout(
        title="Equity Curve & Drawdown",
        template="plotly_dark",
        height=500,
        legend=dict(orientation="h", y=1.02),
        margin=dict(l=40, r=20, t=60, b=40),
    )
    fig.update_yaxes(title_text="Equity Multiple", row=1, col=1)
    fig.update_yaxes(title_text="Drawdown %", row=2, col=1)
    return fig


def monthly_heatmap(trades: list[Trade]) -> go.Figure:
    if not trades:
        return go.Figure()

    df = pd.DataFrame([{"entry_date": t.entry_date, "return_pct": t.return_pct} for t in trades])
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["year"]  = df["entry_date"].dt.year
    df["month"] = df["entry_date"].dt.month

    pivot = df.pivot_table(index="year", columns="month", values="return_pct",
                           aggfunc="sum", fill_value=0)
    month_names = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    pivot.columns = [month_names[m - 1] for m in pivot.columns]

    fig = go.Figure(go.Heatmap(
        z=pivot.values,
        x=pivot.columns.tolist(),
        y=[str(y) for y in pivot.index.tolist()],
        colorscale=[[0, C_RED], [0.5, "#263238"], [1, C_GREEN]],
        zmid=0,
        text=[[f"{v:.1f}%" for v in row] for row in pivot.values],
        texttemplate="%{text}",
        showscale=True,
    ))
    fig.update_layout(
        title="Monthly Returns Heatmap (%)",
        template="plotly_dark",
        height=350,
        margin=dict(l=40, r=20, t=60, b=40),
    )
    return fig


def return_distribution(trades: list[Trade]) -> go.Figure:
    if not trades:
        return go.Figure()

    rets = [t.return_pct for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]

    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=wins, name="Wins",
        marker_color=C_GREEN, opacity=0.75, nbinsx=40,
    ))
    fig.add_trace(go.Histogram(
        x=losses, name="Losses",
        marker_color=C_RED, opacity=0.75, nbinsx=40,
    ))
    fig.update_layout(
        barmode="overlay",
        title="Return Distribution (Win / Loss Histogram)",
        xaxis_title="Return %",
        yaxis_title="Frequency",
        template="plotly_dark",
        height=380,
        margin=dict(l=40, r=20, t=60, b=40),
    )
    return fig


def trade_duration_chart(trades: list[Trade]) -> go.Figure:
    if not trades:
        return go.Figure()

    days = [t.days_held for t in trades]
    fig = go.Figure(go.Histogram(
        x=days,
        marker_color=C_YELLOW,
        nbinsx=20,
    ))
    fig.update_layout(
        title="Trade Duration Distribution",
        xaxis_title="Days Held",
        yaxis_title="Frequency",
        template="plotly_dark",
        height=340,
        margin=dict(l=40, r=20, t=60, b=40),
    )
    return fig


def strategy_comparison(trades: list[Trade]) -> go.Figure:
    if not trades:
        return go.Figure()

    by_strat: dict[str, list[float]] = {}
    for t in trades:
        by_strat.setdefault(t.exit_strategy, []).append(t.return_pct)

    strategies = list(by_strat.keys())
    win_rates   = [sum(1 for r in v if r > 0) / len(v) * 100 for v in by_strat.values()]
    avg_returns = [np.mean(v) for v in by_strat.values()]
    counts      = [len(v) for v in by_strat.values()]

    fig = make_subplots(rows=1, cols=2,
                        subplot_titles=("Win Rate by Strategy", "Avg Return by Strategy"))

    fig.add_trace(go.Bar(x=strategies, y=win_rates,
                         marker_color=C_BLUE, name="Win Rate %"), row=1, col=1)
    fig.add_trace(go.Bar(x=strategies, y=avg_returns,
                         marker_color=[C_GREEN if v > 0 else C_RED for v in avg_returns],
                         name="Avg Return %"), row=1, col=2)

    fig.update_layout(
        title="Exit Strategy Comparison",
        template="plotly_dark",
        height=380,
        showlegend=False,
        margin=dict(l=40, r=20, t=80, b=60),
    )
    return fig


def best_worst_stocks(
    per_stock_df: pd.DataFrame,
    top_n: int = 15,
) -> go.Figure:
    if per_stock_df.empty:
        return go.Figure()

    df = per_stock_df[per_stock_df["total_trades"] >= 3].copy()
    best   = df.nlargest(top_n, "win_rate")
    worst  = df.nsmallest(top_n, "win_rate")

    fig = make_subplots(rows=1, cols=2,
                        subplot_titles=(f"Top {top_n} Stocks (Win Rate)", f"Bottom {top_n} Stocks"))

    fig.add_trace(go.Bar(
        x=best["win_rate"], y=best["symbol"],
        orientation="h", marker_color=C_GREEN, name="Best",
    ), row=1, col=1)

    fig.add_trace(go.Bar(
        x=worst["win_rate"], y=worst["symbol"],
        orientation="h", marker_color=C_RED, name="Worst",
    ), row=1, col=2)

    fig.update_layout(
        title="Best & Worst Stocks by Win Rate",
        template="plotly_dark",
        height=450,
        showlegend=False,
        margin=dict(l=100, r=20, t=80, b=40),
    )
    return fig


def mfe_mae_scatter(trades: list[Trade]) -> go.Figure:
    if not trades:
        return go.Figure()

    wins = [t for t in trades if t.is_win]
    losses = [t for t in trades if t.is_loss]

    fig = go.Figure()
    for group, color, label in [(wins, C_GREEN, "Win"), (losses, C_RED, "Loss")]:
        fig.add_trace(go.Scatter(
            x=[t.mae for t in group],
            y=[t.mfe for t in group],
            mode="markers",
            marker=dict(color=color, size=5, opacity=0.6),
            name=label,
            text=[t.symbol for t in group],
        ))

    fig.update_layout(
        title="Max Adverse vs Max Favorable Excursion",
        xaxis_title="MAE % (Max Loss Reached)",
        yaxis_title="MFE % (Max Gain Reached)",
        template="plotly_dark",
        height=420,
        margin=dict(l=60, r=20, t=60, b=60),
    )
    return fig
