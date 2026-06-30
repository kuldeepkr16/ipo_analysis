from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Trade:
    symbol: str
    signal_date: date
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    exit_strategy: str
    exit_reason: str
    days_held: int
    return_pct: float           # (exit - entry) / entry * 100
    abs_pnl: float              # in rupees, 1 lot assumed
    mfe: float                  # Max Favorable Excursion %
    mae: float                  # Max Adverse Excursion %
    highest_price: float
    lowest_price: float
    target_hit: bool
    stop_hit: bool
    # Signal-day indicators snapshot
    rsi: float | None = None
    gmp_pct: float | None = None
    adx: float | None = None
    vol_ratio: float | None = None
    macd_hist: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    atr: float | None = None
    # Condition params used
    params: dict = field(default_factory=dict)

    @property
    def is_win(self) -> bool:
        return self.return_pct > 0

    @property
    def is_loss(self) -> bool:
        return self.return_pct < 0

    def to_dict(self) -> dict:
        return {
            "symbol":        self.symbol,
            "signal_date":   self.signal_date,
            "entry_date":    self.entry_date,
            "entry_price":   round(self.entry_price, 2),
            "exit_date":     self.exit_date,
            "exit_price":    round(self.exit_price, 2),
            "exit_strategy": self.exit_strategy,
            "exit_reason":   self.exit_reason,
            "days_held":     self.days_held,
            "return_pct":    round(self.return_pct, 3),
            "abs_pnl":       round(self.abs_pnl, 2),
            "mfe":           round(self.mfe, 3),
            "mae":           round(self.mae, 3),
            "highest_price": round(self.highest_price, 2),
            "lowest_price":  round(self.lowest_price, 2),
            "target_hit":    self.target_hit,
            "stop_hit":      self.stop_hit,
            "rsi":           self.rsi,
            "adx":           self.adx,
            "vol_ratio":     round(self.vol_ratio, 2) if self.vol_ratio else None,
            "macd_hist":     self.macd_hist,
            "ema20":         self.ema20,
            "ema50":         self.ema50,
            "ema200":        self.ema200,
            "atr":           self.atr,
        }
