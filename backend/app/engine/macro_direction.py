"""Direzione macro strutturale di BTC.

Le penalità direzionali apprese dall'edge calibration (min score LONG, blocklist,
weight override) vengono accumulate quasi interamente durante un regime macro
ribassista: punire i LONG in quel contesto è corretto, ma continuare a punirli
quando il macro gira al rialzo significa applicare dati stantii. Questo modulo
fornisce la direzione macro di BTC su base strutturale (EMA lente su timeframe
alto), distinta dal trend a breve di `btc_macro_protection` (reversal detector
su 5m): serve a *sospendere* — non cancellare — le penalità contro-regime.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.db.models import Candle, Market
from app.indicators.core import ema

logger = structlog.get_logger()

DEFAULT_MACRO_DIRECTION_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "timeframe": "4h",
    "fast_ema_period": 50,
    "slow_ema_period": 200,
}


def macro_direction_settings(risk_value: dict[str, Any] | None) -> dict[str, Any]:
    raw = (risk_value or {}).get("macro_direction")
    overrides = raw if isinstance(raw, dict) else {}
    return {**DEFAULT_MACRO_DIRECTION_SETTINGS, **overrides}


def btc_macro_direction(db: Session, risk_value: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classifica il regime macro BTC come bullish / bearish / neutral.

    bullish: close > EMA lenta E EMA veloce > EMA lenta (struttura rialzista)
    bearish: close < EMA lenta E EMA veloce < EMA lenta
    neutral: tutto il resto (transizione) — le penalità restano attive.
    Fail-safe: qualsiasi dato mancante → neutral, mai un'eccezione.
    """
    config = macro_direction_settings(risk_value)
    base = {
        "timeframe": config["timeframe"],
        "fast_ema_period": int(config["fast_ema_period"]),
        "slow_ema_period": int(config["slow_ema_period"]),
        "computed_at": datetime.now(UTC).isoformat(),
    }
    if not bool(config.get("enabled", True)):
        return {**base, "direction": "neutral", "reason": "disabled"}
    btc_market = (
        db.query(Market)
        .filter(
            Market.base == "BTC",
            Market.market_type == "perp",
            Market.active.is_(True),
        )
        .first()
    )
    if btc_market is None:
        return {**base, "direction": "neutral", "reason": "btc_market_not_found"}
    slow_period = int(config["slow_ema_period"])
    fast_period = int(config["fast_ema_period"])
    candles = (
        db.query(Candle)
        .filter(
            Candle.market_id == btc_market.id,
            Candle.timeframe == str(config["timeframe"]),
        )
        .order_by(Candle.open_time.desc())
        .limit(slow_period * 2)
        .all()
    )[::-1]
    if len(candles) < slow_period:
        return {
            **base,
            "direction": "neutral",
            "reason": f"insufficient_candles ({len(candles)} < {slow_period})",
        }
    closes = [float(candle.close) for candle in candles]
    fast_values = ema(closes, fast_period)
    slow_values = ema(closes, slow_period)
    fast_value = fast_values[-1] if fast_values else None
    slow_value = slow_values[-1] if slow_values else None
    if fast_value is None or slow_value is None:
        return {**base, "direction": "neutral", "reason": "ema_unavailable"}
    close = closes[-1]
    if close > slow_value and fast_value > slow_value:
        direction = "bullish"
    elif close < slow_value and fast_value < slow_value:
        direction = "bearish"
    else:
        direction = "neutral"
    return {
        **base,
        "direction": direction,
        "close": round(close, 2),
        "fast_ema": round(fast_value, 2),
        "slow_ema": round(slow_value, 2),
    }


def suspended_penalty_family(
    macro: dict[str, Any], risk_value: dict[str, Any] | None
) -> str | None:
    """Famiglia direzionale le cui penalità vanno sospese nel regime corrente.

    Macro bullish → sospende le penalità LONG (apprese in regime opposto).
    Il caso simmetrico (bearish → sospendi SHORT) è deliberatamente escluso:
    le penalità SHORT correnti sono state apprese *durante* il regime bearish,
    quindi restano valide finché il regime non cambia.
    """
    if not bool((risk_value or {}).get("regime_aware_directional_penalties", True)):
        return None
    if macro.get("direction") == "bullish":
        return "LONG"
    return None
