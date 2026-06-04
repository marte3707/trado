from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Alert, AppSetting, Position, Signal
from app.notifications.card_renderer import render_close_card

_CHANNEL_CLOSE_REASONS = {
    "tp1",
    "tp2",
    "tp3",
    "take_profit",
    "stop_loss",
    "trailing_stop",
}

logger = structlog.get_logger()

_DIRECTION_ICON = {"LONG": "📈", "SHORT": "📉"}
_SCORE_FILLED = "●"
_SCORE_EMPTY = "○"
_SCORE_STEPS = 10

_REASON_LABELS: dict[str, str] = {
    "tp1": "TP1",
    "tp2": "TP2",
    "tp3": "TP3",
    "take_profit": "Take Profit",
    "stop_loss": "Stop Loss",
    "trailing_stop": "Trailing Stop",
    "manual": "Manual",
    "btc_macro_protection_loss_exit": "BTC Macro Stop",
    "btc_macro_protection_breakeven": "BTC Macro Breakeven",
    "reconciliazione hl": "HL Reconciled",
}

_REASON_ICONS: dict[str, str] = {
    "tp1": "🥉",
    "tp2": "🥈",
    "tp3": "🥇",
    "take_profit": "🎯",
    "stop_loss": "🛑",
    "trailing_stop": "🛑",
    "manual": "✋",
    "btc_macro_protection_loss_exit": "🟠",
    "btc_macro_protection_breakeven": "🟠",
    "reconciliazione hl": "🔄",
}


def _fmt_price(value: Decimal | None) -> str:
    if value is None:
        return "—"
    normalized = value.normalize()
    # keep at least 2 decimal places for readability
    if normalized == normalized.to_integral_value():
        return f"{normalized:,.2f}"
    return f"{normalized:,f}"


def _score_bar(score: int) -> str:
    filled = round(score / 100 * _SCORE_STEPS)
    return _SCORE_FILLED * filled + _SCORE_EMPTY * (_SCORE_STEPS - filled)


def _fmt_reason(reason: str) -> str:
    return _REASON_LABELS.get(reason.lower(), reason.upper())


def _build_live_close_message(
    *,
    symbol: str,
    side: str,
    reason: str,
    entry_price: Decimal,
    exit_price: Decimal | None,
    remaining_size: Decimal | None,
    step_pnl: Decimal | None,
    total_pnl: Decimal | None,
    is_partial: bool,
    pnl_pct: float | None = None,
) -> str:
    direction = side.upper()
    side_icon = _DIRECTION_ICON.get(direction, "🔔")
    sep = "━━━━━━━━━━━━━━━━━━━━"

    reason_key = reason.lower()
    reason_icon = _REASON_ICONS.get(reason_key)

    if is_partial:
        header_icon = reason_icon or "🟡"
        header = f"{header_icon} <b>LIVE · Partial closure</b>"
        pnl_usd = step_pnl
    else:
        pnl_usd = total_pnl
        if reason_icon is not None and reason_key in {"tp1", "tp2", "tp3", "stop_loss"}:
            header_icon = reason_icon
        elif (pnl_pct is not None and pnl_pct >= 0) or (pnl_usd is not None and float(pnl_usd) >= 0):
            header_icon = "✅"
        else:
            header_icon = "🔴"
        header = f"{header_icon} <b>LIVE · Position closed</b>"

    lines = [
        header,
        f"{side_icon} {symbol} · {direction}",
        sep,
        f"📊 Reason:  <code>{_fmt_reason(reason)}</code>",
        f"💰 Entry:   <code>{_fmt_price(entry_price)}</code>",
    ]
    if exit_price is not None:
        lines.append(f"📤 Exit:    <code>{_fmt_price(exit_price)}</code>")
    if is_partial:
        if remaining_size is not None:
            lines.append(f"📦 Rimasto: <code>{remaining_size.normalize():f}</code>")
        if pnl_usd is not None:
            sign = "+" if float(pnl_usd) >= 0 else ""
            pnl_icon = "📈" if float(pnl_usd) >= 0 else "📉"
            lines.append(f"{pnl_icon} PnL: <code>{sign}{float(pnl_usd):.2f} $</code>")
    else:
        if pnl_pct is not None:
            sign = "+" if pnl_pct >= 0 else ""
            pnl_icon = "📈" if pnl_pct >= 0 else "📉"
            lines.append(f"{pnl_icon} PnL: <code>{sign}{pnl_pct:.1f}%</code>")
    lines += [
        sep,
        "🤖 <i>Trado · @crypto_free_live_signals</i>",
        "🔗 Trade on Hyperliquid &amp; get 4% fee discount: https://app.hyperliquid.xyz/join/CRYPTOFREESIGNALS",
    ]
    return "\n".join(lines)


def _build_channel_close_message(
    *,
    symbol: str,
    side: str,
    reason: str,
    exit_price: Decimal | None,
    is_partial: bool,
    pnl_pct: float | None,
) -> str:
    """Lean English notification posted to the public signals channel as a
    reply to the original signal opening message."""
    direction = side.upper()
    side_icon = _DIRECTION_ICON.get(direction, "🔔")
    reason_key = reason.lower()
    reason_label = _fmt_reason(reason)

    if is_partial:
        reason_icon = _REASON_ICONS.get(reason_key, "🎯")
        header = f"{reason_icon} <b>{reason_label} hit · Partial close</b>"
    else:
        if reason_key in _REASON_ICONS:
            reason_icon = _REASON_ICONS[reason_key]
        elif pnl_pct is not None and pnl_pct < 0:
            reason_icon = "🔴"
        else:
            reason_icon = "✅"
        header = f"{reason_icon} <b>{reason_label} · Position closed</b>"

    lines = [
        header,
        f"{side_icon} {symbol} · {direction}",
    ]
    if exit_price is not None:
        lines.append(f"📤 Exit: <code>{_fmt_price(exit_price)}</code>")
    if pnl_pct is not None:
        sign = "+" if pnl_pct >= 0 else ""
        pnl_icon = "📈" if pnl_pct >= 0 else "📉"
        lines.append(f"{pnl_icon} PnL: <code>{sign}{pnl_pct:.1f}%</code>")
    else:
        lines.append("📊 PnL: <code>N/A</code>")
    return "\n".join(lines)


def _build_btc_macro_message(
    *,
    from_trend: str,
    to_trend: str,
    delta_pct: float,
    vol_ratio: float,
    timeframe: str,
) -> str:
    sep = "━━━━━━━━━━━━━━━━━━━━"
    arrow_icon = "📈" if to_trend.upper() == "BULLISH" else "📉"
    delta_sign = "+" if delta_pct >= 0 else ""
    lines = [
        "⚠️ <b>BTC Macro Reversal Detected</b>",
        sep,
        f"{arrow_icon} Trend: <b>{from_trend} → {to_trend}</b>",
        f"📊 Move: <code>{delta_sign}{delta_pct:.2f}%</code> · Vol: <code>{vol_ratio:.2f}x avg</code>",
        f"⏱️ Timeframe: <code>{timeframe}</code>",
        sep,
        "🛡️ Open positions against the new regime may be reduced, hedged or stopped at breakeven.",
        "🤖 <i>Trado · @crypto_free_live_signals</i>",
    ]
    return "\n".join(lines)


def _build_message(signal: Signal) -> str:
    symbol = signal.market.symbol if signal.market else "—"
    direction = signal.direction.upper()
    icon = _DIRECTION_ICON.get(direction, "🔔")
    score_bar = _score_bar(signal.score)
    sep = "━━━━━━━━━━━━━━━━━━━━"

    lines = [
        f"{icon} <b>{symbol} · {direction}</b>  |  {signal.timeframe}",
        sep,
        f"⭐ Score  <b>{signal.score}/100</b>  <code>{score_bar}</code>",
        "",
        f"💰 Entry       <code>{_fmt_price(signal.entry_price)}</code>",
    ]

    if signal.stop_loss is not None:
        lines.append(f"🛑 Stop Loss   <code>{_fmt_price(signal.stop_loss)}</code>")

    if signal.take_profit_1 is not None:
        lines.append(f"🎯 TP1         <code>{_fmt_price(signal.take_profit_1)}</code>")
    if signal.take_profit_2 is not None:
        lines.append(f"🎯 TP2         <code>{_fmt_price(signal.take_profit_2)}</code>")
    if signal.take_profit_3 is not None:
        lines.append(f"🎯 TP3         <code>{_fmt_price(signal.take_profit_3)}</code>")

    if signal.risk_reward is not None:
        lines.append(f"⚖️ R/R          <code>{signal.risk_reward:.2f}</code>")

    lines += [sep, "🤖 <i>Trado · @crypto_free_live_signals</i>"]
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _token_for(self, chat_id: str | None) -> str | None:
        """Pick the right bot token based on destination chat. Messages sent to
        the public signals channel use landing_bot_token (the public-facing
        bot); everything else (admin DMs, recaps with private balances, errors)
        keeps using telegram_bot_token (the admin-only bot)."""
        if chat_id and chat_id == self.settings.telegram_signal_chat_id:
            return self.settings.public_bot_token
        return self.settings.telegram_bot_token

    async def _send_text(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> bool:
        target = chat_id or self.settings.telegram_chat_id
        token = self._token_for(target)
        if not token or not target:
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload: dict[str, object] = {
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(url, json=payload)
            return response.is_success
        except Exception as exc:
            logger.warning("telegram_send_failed", error=str(exc))
            return False

    async def _send_photo(
        self,
        png_bytes: bytes,
        caption: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> bool:
        target = chat_id or self.settings.telegram_chat_id
        token = self._token_for(target)
        if not token or not target:
            return False
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        data: dict[str, object] = {
            "chat_id": target,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = reply_to_message_id
            data["allow_sending_without_reply"] = True
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    url,
                    data=data,
                    files={"photo": ("card.png", png_bytes, "image/png")},
                )
            return response.is_success
        except Exception as exc:
            logger.warning("telegram_send_photo_failed", error=str(exc))
            return False

    def _signal_channel_reply_id(self, db: Session, signal_id: int | None) -> int | None:
        """Look up the Telegram message_id of the original signal opening
        message in the public signals channel, so close notifications can be
        sent as a reply to it. Returns None if not available."""
        if signal_id is None:
            return None
        alert = (
            db.query(Alert)
            .filter(
                Alert.signal_id == signal_id,
                Alert.channel == "telegram",
                Alert.status == "sent",
            )
            .order_by(Alert.id.desc())
            .first()
        )
        if alert is None:
            return None
        payload: dict[str, Any] = getattr(alert, "payload", None) or {}
        raw = payload.get("telegram_message_id")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def send_worker_online(self, db: Session) -> None:
        now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        mode = "—"
        score = "—"
        defensive = False
        try:
            mode_setting = db.get(AppSetting, "operation_mode")
            if mode_setting:
                mode = mode_setting.value.get("mode", "—")
            risk_setting = db.get(AppSetting, "risk")
            if risk_setting:
                score = risk_setting.value.get("min_signal_score", "—")
            cal_state = db.get(AppSetting, "auto_calibration_state")
            if cal_state:
                defensive = bool(cal_state.value.get("deep_defensive_active", False))
        except Exception:
            pass
        defensive_line = "\n⚠️ <b>Modalità difensiva attiva</b>" if defensive else ""
        await self._send_text(
            f"✅ <b>Trado Worker ONLINE</b>\n"
            f"<code>{now}</code>\n"
            f"\n⚙️ Config: modalità <b>{mode}</b> · score min <b>{score}</b>"
            f"{defensive_line}"
        )

    async def send_worker_error(self, context: str, exc: Exception) -> None:
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        error_type = type(exc).__name__
        error_msg = str(exc)[:300]
        await self._send_text(
            f"🔴 <b>Trado Worker — Errore</b>\n"
            f"<code>{now}</code>\n\n"
            f"Contesto: <code>{context}</code>\n"
            f"<code>{error_type}: {error_msg}</code>"
        )

    async def send_hourly_recap(self, db: Session, stats: dict) -> None:
        _rome = ZoneInfo("Europe/Rome")
        now_str = datetime.now(_rome).strftime("%d/%m/%Y %H:%M (Roma)")

        # posizioni live aperte: long/short, profitto/perdita, nuove nel periodo
        live_long = live_short = live_profit = live_loss = new_positions = 0
        live_pnl = None
        try:
            live_long = (
                db.query(func.count(Position.id))
                .filter(Position.mode == "live", Position.status == "open", Position.side == "long")
                .scalar() or 0
            )
            live_short = (
                db.query(func.count(Position.id))
                .filter(Position.mode == "live", Position.status == "open", Position.side == "short")
                .scalar() or 0
            )
            live_profit = (
                db.query(func.count(Position.id))
                .filter(
                    Position.mode == "live",
                    Position.status == "open",
                    Position.unrealized_pnl >= 0,
                )
                .scalar() or 0
            )
            live_loss = (
                db.query(func.count(Position.id))
                .filter(
                    Position.mode == "live",
                    Position.status == "open",
                    Position.unrealized_pnl < 0,
                )
                .scalar() or 0
            )
            live_pnl = (
                db.query(func.sum(Position.unrealized_pnl))
                .filter(Position.mode == "live", Position.status == "open")
                .scalar()
            )
            period_start_ts = stats.get("period_start")
            if period_start_ts is not None:
                period_start_dt = datetime.fromtimestamp(period_start_ts, tz=timezone.utc)
                new_positions = (
                    db.query(func.count(Position.id))
                    .filter(
                        Position.mode == "live",
                        Position.opened_at >= period_start_dt,
                    )
                    .scalar() or 0
                )
        except Exception:
            pass

        mode = "—"
        defensive = False
        try:
            mode_setting = db.get(AppSetting, "operation_mode")
            if mode_setting:
                mode = mode_setting.value.get("mode", "—")
            cal_state = db.get(AppSetting, "auto_calibration_state")
            if cal_state:
                defensive = bool(cal_state.value.get("deep_defensive_active", False))
        except Exception:
            pass

        def _pnl(v) -> str:
            if v is None:
                return "n/d"
            sign = "+" if float(v) >= 0 else ""
            return f"{sign}{float(v):.2f} $"

        period_min = stats.get("period_minutes", 60)
        raw_balance = stats.get("live_balance")
        balance_str = f"{float(raw_balance):,.2f} $" if raw_balance is not None else "n/d"
        live_total = live_long + live_short
        defensive_line = "\n\n⚠️ <b>Modalità difensiva attiva</b>" if defensive else ""

        await self._send_text(
            f"📊 <b>Trado — Recap {period_min} min</b>\n"
            f"<code>{now_str}</code>\n\n"
            f"💰 Saldo HL: <b>{balance_str}</b>\n"
            f"⚙️ Modalità: <b>{mode}</b>\n\n"
            f"📌 <b>Posizioni live aperte: {live_total}</b>\n"
            f"  📈 Long: {live_long}  |  📉 Short: {live_short}\n"
            f"  ✅ Profitto: {live_profit}  |  🔴 Perdita: {live_loss}\n"
            f"  PnL: <code>{_pnl(live_pnl)}</code>\n\n"
            f"🆕 Aperte nel periodo: {new_positions}"
            f"{defensive_line}"
        )

    async def send_live_position_opened(self, position: Position) -> None:
        symbol = position.market.symbol if position.market else "?"
        side = position.side.upper()
        icon = "📈" if side == "LONG" else "📉"
        notional = float(position.size) * float(position.entry_price)
        contract_lev = (position.metadata_json or {}).get("contract_leverage")
        leverage = f"{int(contract_lev)}x" if contract_lev is not None else "—"
        now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
        await self._send_text(
            f"{icon} <b>Posizione LIVE aperta</b>\n"
            f"<code>{now_str}</code>\n\n"
            f"🪙 <b>{symbol}</b>\n"
            f"↕️ Side: <b>{side}</b>\n"
            f"💰 Valore: <code>{notional:.2f} $</code>\n"
            f"⚙️ Leva: <b>{leverage}</b> · Entry: <code>{float(position.entry_price):.4f}</code>"
        )

    async def send_live_position_closed(
        self,
        *,
        symbol: str,
        side: str,
        reason: str,
        entry_price: Decimal,
        exit_price: Decimal | None,
        remaining_size: Decimal | None,
        step_pnl: Decimal | None,
        total_pnl: Decimal | None,
        is_partial: bool,
        leverage: Decimal | None = None,
        pnl_pct: float | None = None,
        duration_seconds: int | None = None,
        signal_id: int | None = None,
        db: Session | None = None,
    ) -> None:
        text = _build_live_close_message(
            symbol=symbol,
            side=side,
            reason=reason,
            entry_price=entry_price,
            exit_price=exit_price,
            remaining_size=remaining_size,
            step_pnl=step_pnl,
            total_pnl=total_pnl,
            is_partial=is_partial,
            pnl_pct=pnl_pct,
        )
        # Admin DM: invia la share card PNG per chiusure complete (non parziali)
        if not is_partial:
            pnl_for_card = total_pnl if total_pnl is not None else step_pnl
            try:
                png = render_close_card(
                    symbol=symbol,
                    side=side,
                    leverage=leverage,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    pnl_usd=pnl_for_card,
                    pnl_pct=pnl_pct,
                    duration_seconds=duration_seconds,
                    reason=reason,
                )
                sent = await self._send_photo(png, text)
            except Exception as exc:
                logger.warning("telegram_card_render_failed", error=str(exc))
                sent = False
            if not sent:
                await self._send_text(text)
        else:
            await self._send_text(text)

        # Public signals channel: send a lean English message as a reply to the
        # original signal opening message, so subscribers can follow each trade
        # in a single thread. Only relay closures that map to a public exit
        # reason (TP1/2/3, take_profit, stop_loss, trailing_stop); skip manual,
        # reconciliation and internal BTC-macro management events.
        if (
            self.settings.telegram_signal_chat_id
            and reason.lower() in _CHANNEL_CLOSE_REASONS
        ):
            try:
                reply_to = (
                    self._signal_channel_reply_id(db, signal_id)
                    if db is not None
                    else None
                )
                channel_text = _build_channel_close_message(
                    symbol=symbol,
                    side=side,
                    reason=reason,
                    exit_price=exit_price,
                    is_partial=is_partial,
                    pnl_pct=pnl_pct,
                )
                await self._send_text(
                    channel_text,
                    chat_id=self.settings.telegram_signal_chat_id,
                    reply_to_message_id=reply_to,
                )
            except Exception as exc:
                logger.warning("telegram_signal_close_reply_failed", error=str(exc))

    async def send_btc_macro_detected(
        self,
        *,
        from_trend: str,
        to_trend: str,
        delta_pct: float,
        vol_ratio: float,
        timeframe: str,
    ) -> None:
        """Publish a BTC macro reversal alert to the public signals channel.

        The detection itself is logged in DecisionLog by BtcMacroProtectionEngine;
        this method is the user-facing broadcast so subscribers can react to a
        regime change even before any of their open positions get adjusted."""
        if not self.settings.telegram_signal_chat_id:
            return
        text = _build_btc_macro_message(
            from_trend=from_trend,
            to_trend=to_trend,
            delta_pct=delta_pct,
            vol_ratio=vol_ratio,
            timeframe=timeframe,
        )
        try:
            await self._send_text(text, chat_id=self.settings.telegram_signal_chat_id)
        except Exception as exc:
            logger.warning("telegram_btc_macro_send_failed", error=str(exc))

    async def send_signal(self, db: Session, signal: Signal) -> None:
        """Publish a new signal to the public channel only.

        The admin DM intentionally does NOT receive signals anymore — they're
        already broadcast to the public channel by the public bot, and the
        admin can subscribe there like any other user if they want to see them
        in real time. Position-related notifications (open/close, hourly
        recap) keep going to the admin DM as before, since those are about
        the operator's live account and contain sensitive numbers.
        """
        payload: dict[str, str | int | None] = {
            "signal_id": signal.id,
            "direction": signal.direction,
            "score": signal.score,
            "entry": str(signal.entry_price),
            "stop_loss": str(signal.stop_loss),
            "take_profit_1": str(signal.take_profit_1),
            "take_profit_2": str(signal.take_profit_2),
            "take_profit_3": str(signal.take_profit_3) if signal.take_profit_3 is not None else None,
            "risk_reward": str(signal.risk_reward),
        }
        alert = Alert(signal_id=signal.id, channel="telegram", status="pending", payload=payload)
        db.add(alert)
        db.commit()

        public_token = self.settings.public_bot_token
        if not self.settings.telegram_signal_chat_id or not public_token:
            alert.status = "skipped"
            db.commit()
            return

        public_url = f"https://api.telegram.org/bot{public_token}/sendMessage"
        response_status: int | None = None
        message_id: int | None = None
        success = False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    public_url,
                    json={
                        "chat_id": self.settings.telegram_signal_chat_id,
                        "text": _build_message(signal),
                        "parse_mode": "HTML",
                    },
                )
            response_status = response.status_code
            success = response.is_success
            if success:
                try:
                    body: dict[str, Any] = response.json()
                    result: dict[str, Any] = body.get("result") or {}
                    raw_id: Any = result.get("message_id")
                    if raw_id is not None:
                        message_id = int(raw_id)
                except Exception:
                    message_id = None
        except Exception as exc:
            logger.warning("telegram_signal_channel_send_failed", error=str(exc))

        alert.status = "sent" if success else "failed"
        alert.sent_at = datetime.now(timezone.utc) if success else None
        alert.payload = {
            **payload,
            "telegram_status": response_status,
            "telegram_message_id": message_id,
        }
        db.commit()
