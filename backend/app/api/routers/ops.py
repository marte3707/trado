"""Endpoint operativi: funnel bot, info pubbliche, logs, push, guide, telegram."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.routers.system import SYSTEM_CYCLE_EVENTS
from app.core.config import get_settings
from app.db.models import (
    BotUser,
    DecisionLog,
    PushSubscription,
)
from app.db.session import get_db
from app.schemas import (
    PushSubscribePayload,
    PushUnsubscribePayload,
)

_logger = structlog.get_logger()

router = APIRouter()


_TRADE_MANAGEMENT_GUIDE_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Trado · Trade Management Guide</title>
    <style>
      body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #0b1411; color: #ecf4ef; }
      main { max-width: 860px; margin: 0 auto; padding: 40px 20px 72px; line-height: 1.6; }
      h1, h2, h3 { line-height: 1.2; }
      h1 { font-size: 2rem; margin-bottom: 0.25rem; }
      h2 { margin-top: 2rem; border-top: 1px solid #214036; padding-top: 1.5rem; }
      .muted { color: #9bb5ab; }
      .card { background: #11201b; border: 1px solid #214036; border-radius: 14px; padding: 18px 20px; margin-top: 16px; }
      a { color: #57d6a3; }
      ul { padding-left: 1.2rem; }
      code { background: #173028; padding: 0.1rem 0.35rem; border-radius: 6px; }
    </style>
  </head>
  <body>
    <main>
      <h1>Trade Management Guide</h1>
      <p class="muted">Italiano + English glossary for the terms used in Trado Telegram messages.</p>

      <h2 id="trailing-stop">Trailing Stop</h2>
      <div class="card">
        <p><strong>IT:</strong> Uno stop loss che viene spostato a favore della posizione mentre il trade va in profitto, per proteggere parte del guadagno.</p>
        <p><strong>EN:</strong> A stop loss that moves in your favor as the trade becomes profitable, helping protect part of the gains.</p>
      </div>

      <h2 id="breakeven">Breakeven</h2>
      <div class="card">
        <p><strong>IT:</strong> Spostare lo stop in corrispondenza del prezzo di ingresso, così l'uscita non genera più perdita netta sul trade.</p>
        <p><strong>EN:</strong> Moving the stop to the entry price so the trade can no longer produce a net loss.</p>
      </div>

      <h2 id="risk-reward">Risk / Reward</h2>
      <div class="card">
        <p><strong>IT:</strong> Rapporto tra perdita potenziale e profitto atteso. Un <code>R/R 2.0</code> indica che il target teorico vale circa il doppio del rischio iniziale.</p>
        <p><strong>EN:</strong> The ratio between potential loss and expected profit. A <code>R/R 2.0</code> means the theoretical target is about twice the initial risk.</p>
      </div>

      <h2 id="runner">Runner</h2>
      <div class="card">
        <p><strong>IT:</strong> La parte residua della posizione lasciata aperta dopo i take profit parziali, con l'obiettivo di sfruttare ulteriormente il trend.</p>
        <p><strong>EN:</strong> The remaining part of the position left open after partial take profits, aiming to capture more of the trend.</p>
      </div>

      <h2 id="stop-loss">Stop Loss</h2>
      <div class="card">
        <p><strong>IT:</strong> Livello di uscita che limita la perdita massima pianificata se il trade si muove contro la posizione.</p>
        <p><strong>EN:</strong> Exit level designed to cap the planned maximum loss if the trade moves against the position.</p>
      </div>

      <h2 id="martingale">Martingale</h2>
      <div class="card">
        <p><strong>IT:</strong> Strategia che aumenta la size dopo una perdita per cercare di recuperarla. In Trado non viene usata.</p>
        <p><strong>EN:</strong> A strategy that increases position size after a loss to try to recover it. Trado does not use it.</p>
      </div>

      <h2 id="averaging-down">Averaging Down</h2>
      <div class="card">
        <p><strong>IT:</strong> Aggiungere size a una posizione in perdita per abbassare il prezzo medio di ingresso. In Trado non viene usato automaticamente.</p>
        <p><strong>EN:</strong> Adding size to a losing position to lower the average entry price. Trado does not do this automatically.</p>
      </div>
    </main>
  </body>
</html>
"""

@router.get("/admin/bot-funnel")
def admin_bot_funnel(db: Session = Depends(get_db)) -> dict:
    """Admin view of the onboarding funnel: counts, conversion, distributions
    and the most recent users. Read-only; no auth here because the API key
    middleware only protects mutating methods (same convention as the rest of
    the dashboard endpoints)."""

    rows = db.query(BotUser).all()
    total = len(rows)
    by_stage: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_language: dict[str, int] = {}
    by_capital: dict[str, int] = {}
    completed = 0
    declined = 0
    paused = 0
    now = datetime.now(UTC)
    completed_last_24h = 0
    completed_last_7d = 0
    for row in rows:
        by_stage[row.onboarding_stage] = by_stage.get(row.onboarding_stage, 0) + 1
        by_source[row.source or "—"] = by_source.get(row.source or "—", 0) + 1
        by_language[row.language] = by_language.get(row.language, 0) + 1
        if row.trade_capital:
            by_capital[row.trade_capital] = by_capital.get(row.trade_capital, 0) + 1
        else:
            by_capital["skipped"] = by_capital.get("skipped", 0) + 1
        if row.onboarding_stage == "completed":
            completed += 1
            if row.risk_ack_at is not None:
                age = now - row.risk_ack_at
                if age <= timedelta(hours=24):
                    completed_last_24h += 1
                if age <= timedelta(days=7):
                    completed_last_7d += 1
        elif row.onboarding_stage == "declined":
            declined += 1
        if row.paused:
            paused += 1

    completion_rate = (completed / total) if total > 0 else 0.0

    # Per-source funnel: count started vs completed for each source so you can
    # tell which channels (reddit, twitter, landing, ...) bring users that
    # actually finish the disclaimer.
    source_funnel: dict[str, dict[str, int]] = {}
    for row in rows:
        src = row.source or "—"
        bucket = source_funnel.setdefault(src, {"started": 0, "completed": 0})
        bucket["started"] += 1
        if row.onboarding_stage == "completed":
            bucket["completed"] += 1

    recent = (
        db.query(BotUser)
        .order_by(BotUser.created_at.desc())
        .limit(20)
        .all()
    )
    recent_payload = [
        {
            "telegram_username": u.telegram_username,
            "first_name": u.first_name,
            "language": u.language,
            "trade_capital": u.trade_capital,
            "source": u.source,
            "onboarding_stage": u.onboarding_stage,
            "paused": u.paused,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "risk_ack_at": u.risk_ack_at.isoformat() if u.risk_ack_at else None,
            "joined_channel_at": u.joined_channel_at.isoformat() if u.joined_channel_at else None,
        }
        for u in recent
    ]

    return {
        "total": total,
        "completed": completed,
        "declined": declined,
        "paused": paused,
        "completion_rate": round(completion_rate, 4),
        "completed_last_24h": completed_last_24h,
        "completed_last_7d": completed_last_7d,
        "by_stage": by_stage,
        "by_source": by_source,
        "by_language": by_language,
        "by_capital": by_capital,
        "source_funnel": source_funnel,
        "recent": recent_payload,
    }


@router.get("/public/bot-info")
def public_bot_info() -> dict:
    """Public, unauthenticated endpoint used by the landing page to render
    the "Join the channel" CTA without leaking any credentials. Returns the
    bot username (so the frontend can build a https://t.me/<username>?start=...
    deep link) and a hint about whether the funnel is configured at all."""
    settings = get_settings()
    return {
        "bot_username": settings.landing_bot_username,
        "configured": bool(settings.landing_bot_username and settings.public_bot_token),
    }


@router.get("/logs")
def logs(
    db: Session = Depends(get_db),
    signal_id: int | None = Query(None),
    position_id: int | None = Query(None),
    market_id: int | None = Query(None),
    event_type: str | None = Query(None),
    include_system_cycles: bool = Query(False),
    limit: int = Query(250, ge=1, le=1000),
) -> list[dict]:
    q = db.query(DecisionLog)
    if signal_id is not None:
        q = q.filter(DecisionLog.signal_id == signal_id)
    if position_id is not None:
        q = q.filter(DecisionLog.position_id == position_id)
    if market_id is not None:
        q = q.filter(DecisionLog.market_id == market_id)
    if event_type is not None:
        q = q.filter(DecisionLog.event_type == event_type)
    elif not include_system_cycles:
        q = q.filter(~DecisionLog.event_type.in_(tuple(SYSTEM_CYCLE_EVENTS)))
    return [
        {
            "id": row.id,
            "timestamp": row.timestamp,
            "event_type": row.event_type,
            "market_id": row.market_id,
            "signal_id": row.signal_id,
            "position_id": row.position_id,
            "message": row.message,
            "data": row.data,
        }
        for row in q.order_by(DecisionLog.timestamp.desc()).limit(limit).all()
    ]


@router.post("/push/subscribe")
def push_subscribe(payload: PushSubscribePayload, db: Session = Depends(get_db)) -> dict:
    sub = db.query(PushSubscription).filter(PushSubscription.endpoint == payload.endpoint).first()
    if sub is None:
        sub = PushSubscription(
            endpoint=payload.endpoint,
            p256dh=payload.keys.get("p256dh", ""),
            auth=payload.keys.get("auth", ""),
        )
        db.add(sub)
    else:
        sub.active = True
        sub.p256dh = payload.keys.get("p256dh", sub.p256dh)
        sub.auth = payload.keys.get("auth", sub.auth)
    db.commit()
    return {"status": "subscribed"}


@router.delete("/push/subscribe")
def push_unsubscribe(payload: PushUnsubscribePayload, db: Session = Depends(get_db)) -> dict:
    sub = db.query(PushSubscription).filter(PushSubscription.endpoint == payload.endpoint).first()
    if sub is not None:
        sub.active = False
        db.commit()
    return {"status": "unsubscribed"}


@router.post("/push/test")
async def push_test(db: Session = Depends(get_db)) -> dict:
    from app.notifications.push import WebPushNotifier
    sent = await WebPushNotifier(get_settings()).send(db, "Trado", "Browser push funzionante!")
    return {"status": "sent", "count": sent}


@router.get("/learn/trade-management", include_in_schema=False, response_class=HTMLResponse)
async def trade_management_guide() -> HTMLResponse:
    return HTMLResponse(_TRADE_MANAGEMENT_GUIDE_HTML)


@router.post("/telegram/test-close-card")
async def telegram_test_close_card() -> dict:
    """Invia una card di test sui canali Telegram configurati."""
    from app.notifications.card_renderer import render_close_card
    from app.notifications.telegram import TelegramNotifier, _build_live_close_message

    settings = get_settings()
    notifier = TelegramNotifier(settings)

    symbol = "BTC"
    side = "LONG"
    entry = Decimal("95000")
    exit_p = Decimal("101650")
    pnl_usd = Decimal("332.50")
    pnl_pct = 7.0
    leverage = Decimal("3")
    duration = 14400  # 4h

    text = _build_live_close_message(
        symbol=symbol,
        side=side,
        reason="tp2",
        entry_price=entry,
        exit_price=exit_p,
        remaining_size=None,
        step_pnl=None,
        total_pnl=pnl_usd,
        is_partial=False,
    )

    png = render_close_card(
        symbol=symbol,
        side=side,
        leverage=leverage,
        entry_price=entry,
        exit_price=exit_p,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        duration_seconds=duration,
        reason="tp2",
    )

    channels: list[str] = []
    if settings.telegram_chat_id:
        ok = await notifier._send_photo(png, text)
        channels.append(f"main({'ok' if ok else 'fail'})")
    if settings.telegram_signal_chat_id:
        ok = await notifier._send_photo(png, text, chat_id=settings.telegram_signal_chat_id)
        channels.append(f"signal({'ok' if ok else 'fail'})")

    if not channels:
        return {"status": "skipped", "reason": "nessun chat_id configurato"}
    return {"status": "sent", "channels": channels}


@router.post("/telegram/channel-strategy-message")
async def telegram_channel_strategy_message(pin: bool = True) -> dict:
    """Pubblica nel canale segnali il messaggio bilingue di strategia e,
    opzionalmente, lo pinna."""
    from app.notifications.telegram import TelegramNotifier

    notifier = TelegramNotifier(get_settings())
    return await notifier.send_channel_strategy_message(pin=pin)
