from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import numpy as np

from app.indicators.ta import bollinger_bands, macd, to_heikin_ashi
from app.models import Candle, Direction, Signal


def analyze_2m(pair: str, candles: List[Candle], payout: Optional[float] = None) -> Optional[Signal]:
    """
    Stratégie M2 :
      - Heikin Ashi
      - Bollinger Bands (6, 1.3)
      - MACD (6, 19, 6)
    """
    if len(candles) < 30:
        return None

    ha = to_heikin_ashi(candles)
    closes = [c.close for c in ha]

    bb_upper, _, bb_lower = bollinger_bands(closes, period=6, deviation=1.3)
    macd_line, signal_line, hist = macd(closes, fast=6, slow=19, signal=6)

    if np.isnan(bb_upper[-1]) or np.isnan(macd_line[-2]) or np.isnan(signal_line[-2]):
        return None

    last = ha[-1]
    is_bullish_candle = last.close > last.open
    is_bearish_candle = last.close < last.open
    body_ratio = abs(last.close - last.open) / max(1e-9, (last.high - last.low))
    strong_candle = body_ratio > 0.6

    macd_cross_up = macd_line[-2] <= signal_line[-2] and macd_line[-1] > signal_line[-1]
    macd_cross_down = macd_line[-2] >= signal_line[-2] and macd_line[-1] < signal_line[-1]

    # "Croisement au-dessus/en-dessous de l'histogramme" ~ le croisement doit
    # se produire du bon côté du zéro de l'histogramme précédent.
    cross_above_hist = hist[-2] <= 0 <= macd_line[-1] - signal_line[-1] or macd_cross_up
    cross_below_hist = hist[-2] >= 0 >= macd_line[-1] - signal_line[-1] or macd_cross_down

    near_upper_band = last.high >= bb_upper[-1] * 0.999
    near_lower_band = last.low <= bb_lower[-1] * 1.001

    if macd_cross_up and near_upper_band and is_bullish_candle and strong_candle:
        return Signal(
            pair=pair,
            direction=Direction.CALL,
            timeframe_minutes=2,
            entry_time=datetime.now(timezone.utc),
            payout=payout,
            confidence=min(95.0, 65 + 20 * body_ratio),
            entry_price=closes[-1],
            rsi=50.0,  # non utilisé dans cette stratégie, valeur neutre
            bb_upper=bb_upper[-1],
            bb_lower=bb_lower[-1],
            macd=macd_line[-1],
            macd_signal=signal_line[-1],
        )

    if macd_cross_down and near_lower_band and is_bearish_candle and strong_candle:
        return Signal(
            pair=pair,
            direction=Direction.PUT,
            timeframe_minutes=2,
            entry_time=datetime.now(timezone.utc),
            payout=payout,
            confidence=min(95.0, 65 + 20 * body_ratio),
            entry_price=closes[-1],
            rsi=50.0,
            bb_upper=bb_upper[-1],
            bb_lower=bb_lower[-1],
            macd=macd_line[-1],
            macd_signal=signal_line[-1],
        )

    return None
