from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass
class ExitResult:
    exit_price: float
    exit_reason: str        # TARGET | STOP_LOSS | TRAILING_STOP | EMA_BREAK |
                            # MACD_BEAR | HELD_N_DAYS | MAX_DAYS | END_OF_DATA
    days_held: int
    mfe: float              # Max Favorable Excursion (highest % gain reached)
    mae: float              # Max Adverse Excursion (deepest % loss reached)
    highest_price: float
    lowest_price: float


class ExitStrategy(ABC):
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def evaluate(
        self,
        entry_price: float,
        entry_idx: int,
        df: pd.DataFrame,
        params: dict,
    ) -> ExitResult:
        """
        Simulate what happens after buying at entry_price on day entry_idx+1 open.
        df is the full indicator-enriched DataFrame. entry_idx is the signal day index.
        """
        ...

    def _track_excursion(
        self, entry_price: float, highs: list[float], lows: list[float]
    ) -> tuple[float, float, float, float]:
        if not highs:
            return 0.0, 0.0, entry_price, entry_price
        highest = max(highs)
        lowest = min(lows)
        mfe = (highest - entry_price) / entry_price * 100
        mae = (lowest - entry_price) / entry_price * 100
        return mfe, mae, highest, lowest


# --------------------------------------------------------------------------- #
#  Strategy A: Fixed target / stop loss / max holding                         #
# --------------------------------------------------------------------------- #

class StrategyA(ExitStrategy):
    def name(self) -> str:
        return "A_fixed_target_sl"

    def evaluate(self, entry_price, entry_idx, df, params):
        target_pct = params.get("target_pct", 10.0)
        sl_pct = params.get("stop_loss_pct", 4.0)
        max_days = int(params.get("max_days", 10))
        same_bar = params.get("same_bar_assumption", "sl_first")

        target = entry_price * (1 + target_pct / 100)
        stop = entry_price * (1 - sl_pct / 100)

        highs, lows = [], []
        slice_df = df.iloc[entry_idx + 1 : entry_idx + 1 + max_days]

        for day_num, (_, row) in enumerate(slice_df.iterrows(), start=1):
            h, l = row["High"], row["Low"]
            highs.append(h); lows.append(l)
            target_hit = h >= target
            sl_hit = l <= stop

            if target_hit and sl_hit:
                # Both hit on the same candle
                if same_bar == "sl_first":
                    exit_p, reason = stop, "STOP_LOSS"
                else:
                    exit_p, reason = target, "TARGET"
            elif target_hit:
                exit_p, reason = target, "TARGET"
            elif sl_hit:
                exit_p, reason = stop, "STOP_LOSS"
            else:
                continue

            mfe, mae, hi, lo = self._track_excursion(entry_price, highs, lows)
            return ExitResult(exit_p, reason, day_num, mfe, mae, hi, lo)

        # Max holding period reached — exit at last close
        if slice_df.empty:
            mfe, mae, hi, lo = 0.0, 0.0, entry_price, entry_price
            return ExitResult(entry_price, "END_OF_DATA", 0, mfe, mae, hi, lo)

        exit_p = slice_df.iloc[-1]["Close"]
        days = len(slice_df)
        mfe, mae, hi, lo = self._track_excursion(entry_price, highs, lows)
        return ExitResult(exit_p, "MAX_DAYS", days, mfe, mae, hi, lo)


# --------------------------------------------------------------------------- #
#  Strategy B: ATR-based trailing stop                                        #
# --------------------------------------------------------------------------- #

class StrategyB(ExitStrategy):
    def name(self) -> str:
        return "B_atr_trailing"

    def evaluate(self, entry_price, entry_idx, df, params):
        atr_mult = params.get("atr_multiplier", 2.0)
        init_mult = params.get("initial_stop_atr_mult", 1.5)
        max_days = int(params.get("max_days", 15))

        entry_atr = df.iloc[entry_idx]["ATR"]
        trailing_stop = entry_price - init_mult * entry_atr

        highs, lows, peak = [], [], entry_price
        slice_df = df.iloc[entry_idx + 1 : entry_idx + 1 + max_days]

        for day_num, (_, row) in enumerate(slice_df.iterrows(), start=1):
            h, l, atr = row["High"], row["Low"], row["ATR"]
            highs.append(h); lows.append(l)

            # Ratchet the trailing stop upward as price rises
            peak = max(peak, h)
            trailing_stop = max(trailing_stop, peak - atr_mult * atr)

            if l <= trailing_stop:
                exit_p = min(l, trailing_stop)  # gap-down can breach stop
                mfe, mae, hi, lo = self._track_excursion(entry_price, highs, lows)
                return ExitResult(exit_p, "TRAILING_STOP", day_num, mfe, mae, hi, lo)

        if slice_df.empty:
            return ExitResult(entry_price, "END_OF_DATA", 0, 0.0, 0.0, entry_price, entry_price)

        exit_p = slice_df.iloc[-1]["Close"]
        mfe, mae, hi, lo = self._track_excursion(entry_price, highs, lows)
        return ExitResult(exit_p, "MAX_DAYS", len(slice_df), mfe, mae, hi, lo)


# --------------------------------------------------------------------------- #
#  Strategy C: Exit on EMA20 breakdown                                        #
# --------------------------------------------------------------------------- #

class StrategyC(ExitStrategy):
    def name(self) -> str:
        return "C_ema20_breakdown"

    def evaluate(self, entry_price, entry_idx, df, params):
        max_days = int(params.get("max_days", 20))
        highs, lows = [], []
        slice_df = df.iloc[entry_idx + 1 : entry_idx + 1 + max_days]

        for day_num, (_, row) in enumerate(slice_df.iterrows(), start=1):
            highs.append(row["High"]); lows.append(row["Low"])
            if row["Close"] < row["EMA20"]:
                exit_p = row["Close"]
                mfe, mae, hi, lo = self._track_excursion(entry_price, highs, lows)
                return ExitResult(exit_p, "EMA_BREAK", day_num, mfe, mae, hi, lo)

        if slice_df.empty:
            return ExitResult(entry_price, "END_OF_DATA", 0, 0.0, 0.0, entry_price, entry_price)

        exit_p = slice_df.iloc[-1]["Close"]
        mfe, mae, hi, lo = self._track_excursion(entry_price, highs, lows)
        return ExitResult(exit_p, "MAX_DAYS", len(slice_df), mfe, mae, hi, lo)


# --------------------------------------------------------------------------- #
#  Strategy D: Exit on MACD bearish crossover                                 #
# --------------------------------------------------------------------------- #

class StrategyD(ExitStrategy):
    def name(self) -> str:
        return "D_macd_bear"

    def evaluate(self, entry_price, entry_idx, df, params):
        max_days = int(params.get("max_days", 20))
        highs, lows = [], []
        slice_df = df.iloc[entry_idx + 1 : entry_idx + 1 + max_days]
        prev_macd_above = True  # assume MACD > Signal at entry

        for day_num, (_, row) in enumerate(slice_df.iterrows(), start=1):
            highs.append(row["High"]); lows.append(row["Low"])
            macd_above = row["MACD"] > row["MACD_Signal"]
            if prev_macd_above and not macd_above:
                exit_p = row["Close"]
                mfe, mae, hi, lo = self._track_excursion(entry_price, highs, lows)
                return ExitResult(exit_p, "MACD_BEAR", day_num, mfe, mae, hi, lo)
            prev_macd_above = macd_above

        if slice_df.empty:
            return ExitResult(entry_price, "END_OF_DATA", 0, 0.0, 0.0, entry_price, entry_price)

        exit_p = slice_df.iloc[-1]["Close"]
        mfe, mae, hi, lo = self._track_excursion(entry_price, highs, lows)
        return ExitResult(exit_p, "MAX_DAYS", len(slice_df), mfe, mae, hi, lo)


# --------------------------------------------------------------------------- #
#  Strategy E: Fixed holding periods                                           #
# --------------------------------------------------------------------------- #

class StrategyE(ExitStrategy):
    def __init__(self, holding_period: int) -> None:
        self._days = holding_period

    def name(self) -> str:
        return f"E_hold{self._days}d"

    def evaluate(self, entry_price, entry_idx, df, params):
        n = self._days
        slice_df = df.iloc[entry_idx + 1 : entry_idx + 1 + n]

        if slice_df.empty:
            return ExitResult(entry_price, "END_OF_DATA", 0, 0.0, 0.0, entry_price, entry_price)

        highs = slice_df["High"].tolist()
        lows = slice_df["Low"].tolist()
        exit_p = slice_df.iloc[-1]["Close"]
        days = len(slice_df)
        mfe, mae, hi, lo = self._track_excursion(entry_price, highs, lows)
        return ExitResult(exit_p, f"HELD_{days}_DAYS", days, mfe, mae, hi, lo)


# --------------------------------------------------------------------------- #
#  Factory                                                                     #
# --------------------------------------------------------------------------- #

def build_exit_strategies(cfg) -> list[ExitStrategy]:
    exits: list[ExitStrategy] = [
        StrategyA(),
        StrategyB(),
        StrategyC(),
        StrategyD(),
    ]
    for h in cfg.exits.strategy_e.holding_periods:
        exits.append(StrategyE(h))
    return exits
