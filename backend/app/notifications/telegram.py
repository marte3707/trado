from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.runtime_settings import DEFAULT_POSITION_MANAGEMENT_SETTINGS
from app.db.models import Alert, AppSetting, Order, Position, Signal
from app.engine.portfolio_guard import PortfolioGuard
from app.notifications.card_renderer import render_close_card
from app.services.trade_metrics import build_trade_metrics

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

_TERM_ANCHORS: dict[str, str] = {
    "trailing_stop": "trailing-stop",
    "risk_reward": "risk-reward",
    "breakeven": "breakeven",
    "martingale": "martingale",
    "averaging_down": "averaging-down",
    "stop_loss": "stop-loss",
    "runner": "runner",
}
_TERM_FALLBACK_URLS: dict[str, str] = {
    "trailing_stop": "https://www.investopedia.com/terms/t/trailingstop.asp",
    "risk_reward": "https://www.investopedia.com/terms/r/riskrewardratio.asp",
    "breakeven": "https://www.investopedia.com/terms/b/breakevenpoint.asp",
    "martingale": "https://www.investopedia.com/terms/m/martingalesystem.asp",
    "averaging_down": "https://www.investopedia.com/terms/a/averagedown.asp",
    "stop_loss": "https://www.investopedia.com/terms/s/stop-lossorder.asp",
    "runner": "https://www.investopedia.com/terms/t/trailingstop.asp",
}
_DAILY_RECAP_STATE_KEY = "telegram_daily_recap_state"


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


def _macro_risk_icon(level: str, active: bool) -> str:
    if not active:
        return "✅"
    return {
        "extreme": "🚨",
        "high": "⚠️",
        "watch": "🟡",
        "manual": "🛡️",
    }.get(level.lower(), "⚠️")


def _humanize_macro_tokens(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "n/a"
    parts = [part.strip().replace("_", " ") for part in text.split(",") if part.strip()]
    return ", ".join(parts) or "n/a"


def _format_macro_sources(value: object) -> str:
    if not isinstance(value, list):
        return "n/a"
    labels = [str(item).replace("_", " ").strip() for item in value if str(item).strip()]
    return ", ".join(labels) or "n/a"


def _short_text(value: str, max_len: int = 120) -> str:
    text = " ".join(value.split())
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 1].rstrip()}…"


def _macro_risk_news_lines(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    lines: list[str] = []
    for item in value[:2]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        source = str(item.get("source") or "").strip()
        url = str(item.get("url") or "").strip()
        label = escape(_short_text(title))
        source_suffix = f" <i>{escape(source)}</i>" if source else ""
        if url.startswith(("https://", "http://")):
            safe_url = escape(url, quote=True)
            lines.append(f"• <a href=\"{safe_url}\">{label}</a>{source_suffix}")
        else:
            lines.append(f"• {label}{source_suffix}")
    return lines


def _macro_risk_calendar_lines(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    active_events = value.get("active_events")
    if not isinstance(active_events, list):
        return []
    lines: list[str] = []
    for item in active_events[:2]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        source = str(item.get("source") or "").strip()
        url = str(item.get("url") or "").strip()
        label = escape(_short_text(title))
        source_suffix = f" <i>{escape(source)}</i>" if source else ""
        if url.startswith(("https://", "http://")):
            lines.append(f"• <a href=\"{escape(url, quote=True)}\">{label}</a>{source_suffix}")
        else:
            lines.append(f"• {label}{source_suffix}")
    return lines


def _format_italian_datetime(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(ZoneInfo("Europe/Rome"))
    return local.strftime("%d/%m/%Y alle %H:%M")


def _terms_guide_url(anchor: str | None = None) -> str:
    base = get_settings().public_api_base_url.rstrip("/")
    if not base or "localhost" in base or "127.0.0.1" in base:
        return ""
    url = f"{base}/api/learn/trade-management"
    if anchor:
        return f"{url}#{anchor}"
    return url


def _term_link(label: str, key: str) -> str:
    url = _terms_guide_url(_TERM_ANCHORS[key]) or _TERM_FALLBACK_URLS[key]
    return f'<a href="{url}">{label}</a>'


def _tp_allocation_pct(key: str) -> int:
    take_profit = DEFAULT_POSITION_MANAGEMENT_SETTINGS.get("takeProfit", {})
    try:
        return int(take_profit.get(key, 0))
    except (TypeError, ValueError):
        return 0


def _is_profitable_close(
    *,
    pnl_pct: float | None,
    pnl_usd: Decimal | None,
) -> bool:
    if pnl_usd is not None:
        return float(pnl_usd) > 0
    if pnl_pct is not None:
        return pnl_pct > 0
    return False


def _should_send_channel_close_photo(
    *,
    is_partial: bool,
    pnl_pct: float | None,
    pnl_usd: Decimal | None,
) -> bool:
    return not is_partial and _is_profitable_close(pnl_pct=pnl_pct, pnl_usd=pnl_usd)


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
        elif pnl_usd is not None:
            sign = "+" if float(pnl_usd) >= 0 else ""
            pnl_icon = "📈" if float(pnl_usd) >= 0 else "📉"
        else:
            sign = ""
            pnl_icon = "📈"
        if pnl_usd is not None:
            lines.append(f"{pnl_icon} PnL: <code>{sign}{float(pnl_usd):.2f} $</code>")
        if pnl_pct is not None:
            lines.append(f"{pnl_icon} PnL %: <code>{sign}{pnl_pct:.1f}%</code>")
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


def _daily_recap_summary(
    closed_positions: list[Position],
    orders_by_position: dict[int, list[Order]],
) -> dict[str, Any]:
    total_pnl = Decimal("0")
    total_notional = Decimal("0")
    realized_trades = 0
    wins = 0
    r_values: list[Decimal] = []

    for position in closed_positions:
        position_id = position.id if position.id is not None else -1
        metrics = build_trade_metrics(position, orders_by_position.get(position_id, ()))
        realized = metrics.realized_pnl
        if realized is None:
            continue
        realized_trades += 1
        total_pnl += realized
        if metrics.notional is not None:
            total_notional += metrics.notional
        if realized > 0:
            wins += 1
        if metrics.realized_r_multiple is not None:
            r_values.append(metrics.realized_r_multiple)

    avg_r_multiple = (
        sum(r_values, Decimal("0")) / Decimal(len(r_values))
        if r_values
        else None
    )
    total_return_pct = (
        (total_pnl / total_notional) * Decimal("100")
        if total_notional > 0
        else None
    )
    win_rate = (wins / realized_trades) if realized_trades else None
    return {
        "closed_trades": realized_trades,
        "total_pnl": total_pnl,
        "total_return_pct": total_return_pct,
        "win_rate": win_rate,
        "avg_r_multiple": avg_r_multiple,
    }


def _build_daily_recap_message(
    *,
    opened_trades: int,
    total_return_pct: Decimal | None,
    win_rate: float | None,
    avg_r_multiple: Decimal | None,
    window_start: datetime,
    window_end: datetime,
) -> str:
    rome = ZoneInfo("Europe/Rome")
    start_str = window_start.astimezone(rome).strftime("%d/%m %H:%M")
    end_str = window_end.astimezone(rome).strftime("%d/%m %H:%M")
    if total_return_pct is not None:
        pnl_sign = "+" if total_return_pct >= 0 else ""
        pnl_str = f"{pnl_sign}{float(total_return_pct):.2f}%"
    else:
        pnl_str = "n/d"
    win_rate_str = f"{win_rate * 100:.1f}%" if win_rate is not None else "n/d"
    avg_r_str = f"{float(avg_r_multiple):+.2f}R" if avg_r_multiple is not None else "n/d"
    return "\n".join(
        [
            "🗓️ <b>Trado — Daily Recap</b>",
            f"<code>{start_str} → {end_str} (Roma)</code>",
            "",
            f"📌 Opened trades: <b>{opened_trades}</b>",
            f"💰 Total P&amp;L 24h: <code>{pnl_str}</code>",
            f"✅ Win rate: <b>{win_rate_str}</b>",
            f"📏 Medium R-Multiple: <b>{avg_r_str}</b>",
        ]
    )


def _build_channel_strategy_message() -> str:
    tp1_pct = _tp_allocation_pct("tp1ClosePct")
    tp2_pct = _tp_allocation_pct("tp2ClosePct")
    runner_pct = _tp_allocation_pct("runnerPct")
    risk_reward = _term_link("risk/reward", "risk_reward")
    martingale = _term_link("martingale", "martingale")
    averaging_down = _term_link("averaging down", "averaging_down")
    trailing_stop = _term_link("trailing stop", "trailing_stop")
    breakeven = _term_link("breakeven", "breakeven")
    return "\n".join(
        [
            "📌 <b>How We Trade · Come operiamo</b>",
            "",
            "🇮🇹 <b>Italiano</b>",
            f"• Entriamo solo quando più segnali tecnici convergono e il trade supera i filtri minimi di score e {risk_reward}.",
            "• Uno score più alto non è una probabilità matematica di riuscita: è un indicatore interno di qualità e confluenza del setup.",
            f"• Il sizing segue regole di rischio fisse: niente {martingale}, niente {averaging_down} automatico.",
            "• Tradate solo capitali che potete permettervi di perdere completamente e dimensionate la size in base al vostro conto e al rischio del trade.",
            "• Impostate limiti di perdita giornalieri e guardrail personali per proteggere il capitale.",
            f"• Le uscite usano TP1 ({tp1_pct}%) e TP2 ({tp2_pct}%) parziali; la size restante ({runner_pct}%) va gestita personalmente con {trailing_stop}.",
            f"• Non inviamo tutti gli aggiornamenti intermedi di stop: {breakeven} e trailing vanno seguiti e aggiornati manualmente.",
            "• La performance del sistema va letta sul campione completo: saltando alcuni trade il risultato personale può essere diverso, anche inferiore, rispetto al track record registrato.",
            "• Se volete testare il sistema, iniziate con size piccole.",
            "",
            "🇬🇧 <b>English</b>",
            f"• We enter only when multiple technical signals align and the trade clears minimum score and {risk_reward} filters.",
            "• A higher score is not a mathematical probability of success: it is an internal indicator of setup quality and confluence.",
            f"• Position sizing follows fixed risk rules: no {martingale}, no automatic {averaging_down}.",
            "• Trade only capital you can afford to lose completely, and size positions according to your account and the risk of the trade.",
            "• Set daily loss limits and personal guardrails to protect your capital.",
            f"• Exits use partial TP1 ({tp1_pct}%) and TP2 ({tp2_pct}%); the remaining size ({runner_pct}%) should be managed manually with a {trailing_stop}.",
            f"• We do not broadcast every stop update: {breakeven} and trailing adjustments should be monitored and managed personally.",
            "• System performance should be read over the full sample: skipping some trades can lead to personal results that differ from, and may underperform, the recorded track record.",
            "• If you want to test the system, start with small size.",
            "",
            "⚠️ <i>Educational content, not financial advice.</i>",
        ]
    )


def _build_message(signal: Signal) -> str:
    tp1_pct = _tp_allocation_pct("tp1ClosePct")
    tp2_pct = _tp_allocation_pct("tp2ClosePct")
    runner_pct = _tp_allocation_pct("runnerPct")
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
        lines.append(f"🎯 TP1 ({tp1_pct}%)   <code>{_fmt_price(signal.take_profit_1)}</code>")
    if signal.take_profit_2 is not None:
        lines.append(f"🎯 TP2 ({tp2_pct}%)   <code>{_fmt_price(signal.take_profit_2)}</code>")
    lines.append(f"🏃 Runner ({runner_pct}%) Move remaining size to trailing stop")

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

    async def _telegram_json_post(
        self,
        *,
        method: str,
        payload: dict[str, object],
        chat_id: str | None = None,
        timeout: int = 10,
    ) -> dict[str, Any]:
        target = chat_id or self.settings.telegram_chat_id
        token = self._token_for(target)
        if not token or not target:
            return {"ok": False, "description": "missing_token_or_chat_id"}
        url = f"https://api.telegram.org/bot{token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload)
            body = response.json()
            if response.is_success and body.get("ok"):
                return {"ok": True, "result": body.get("result")}
            return {
                "ok": False,
                "status_code": response.status_code,
                "description": body.get("description") or "telegram_api_error",
            }
        except Exception as exc:
            return {"ok": False, "description": str(exc)}

    async def _send_text_result(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any] | None:
        target = chat_id or self.settings.telegram_chat_id
        payload: dict[str, object] = {
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        result = await self._telegram_json_post(
            method="sendMessage",
            payload=payload,
            chat_id=target,
            timeout=10,
        )
        if not result.get("ok"):
            logger.warning("telegram_send_failed", error=result.get("description"))
            return None
        return result.get("result")

    async def _send_text(
        self,
        text: str,
        chat_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> bool:
        return (
            await self._send_text_result(
                text,
                chat_id=chat_id,
                reply_to_message_id=reply_to_message_id,
            )
        ) is not None

    async def _pin_message(
        self,
        *,
        chat_id: str,
        message_id: int,
        disable_notification: bool = True,
    ) -> bool:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": disable_notification,
        }
        result = await self._telegram_json_post(
            method="pinChatMessage",
            payload=payload,
            chat_id=chat_id,
            timeout=10,
        )
        if not result.get("ok"):
            logger.warning("telegram_pin_failed", error=result.get("description"))
            return False
        return True

    async def _unpin_message(
        self,
        *,
        chat_id: str,
        message_id: int,
    ) -> bool:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        result = await self._telegram_json_post(
            method="unpinChatMessage",
            payload=payload,
            chat_id=chat_id,
            timeout=10,
        )
        if not result.get("ok"):
            logger.warning("telegram_unpin_failed", error=result.get("description"))
            return False
        return True

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

    async def send_channel_strategy_message(self, *, pin: bool = True) -> dict[str, Any]:
        if not self.settings.telegram_signal_chat_id:
            return {"status": "skipped", "reason": "telegram_signal_chat_id_not_configured"}
        result = await self._send_text_result(
            _build_channel_strategy_message(),
            chat_id=self.settings.telegram_signal_chat_id,
            disable_web_page_preview=True,
        )
        if result is None:
            return {"status": "failed", "pinned": False}
        raw_message_id = result.get("message_id")
        try:
            message_id = int(raw_message_id) if raw_message_id is not None else None
        except (TypeError, ValueError):
            message_id = None
        pinned = False
        pin_hint: str | None = None
        if pin and message_id is not None:
            pinned = await self._pin_message(
                chat_id=self.settings.telegram_signal_chat_id,
                message_id=message_id,
            )
            if not pinned:
                pin_hint = "verify_public_bot_is_channel_admin_with_message_management_permissions"
        return {
            "status": "sent",
            "message_id": message_id,
            "pinned": pinned,
            "pin_hint": pin_hint,
        }

    async def send_worker_online(self, db: Session) -> None:
        now = datetime.now(UTC).strftime("%d/%m/%Y %H:%M UTC")
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
        now = datetime.now(UTC).strftime("%H:%M UTC")
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
                period_start_dt = datetime.fromtimestamp(period_start_ts, tz=UTC)
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

    async def send_daily_recap(
        self,
        db: Session,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> dict[str, Any]:
        if not self.settings.telegram_signal_chat_id:
            return {"status": "skipped", "reason": "telegram_signal_chat_id_not_configured"}
        end = window_end or datetime.now(UTC)
        start = window_start or (end - timedelta(hours=24))
        opened_trades = 0
        closed_positions: list[Position] = []
        orders_by_position: dict[int, list[Order]] = defaultdict(list)
        try:
            opened_trades = (
                db.query(func.count(Position.id))
                .filter(
                    Position.mode == "live",
                    Position.opened_at >= start,
                    Position.opened_at < end,
                )
                .scalar() or 0
            )
            closed_positions = (
                db.query(Position)
                .filter(
                    Position.mode == "live",
                    Position.status == "closed",
                    Position.closed_at.is_not(None),
                    Position.closed_at >= start,
                    Position.closed_at < end,
                )
                .all()
            )
            position_ids = [position.id for position in closed_positions if position.id is not None]
            if position_ids:
                orders = db.query(Order).filter(Order.position_id.in_(position_ids)).all()
                for order in orders:
                    if order.position_id is not None:
                        orders_by_position[order.position_id].append(order)
        except Exception:
            logger.exception("daily_recap_query_failed")

        summary = _daily_recap_summary(closed_positions, orders_by_position)
        state = db.get(AppSetting, _DAILY_RECAP_STATE_KEY) or AppSetting(
            key=_DAILY_RECAP_STATE_KEY,
            value={},
        )
        previous_message_id: int | None = None
        if isinstance(state.value, dict):
            raw_previous = state.value.get("message_id")
            try:
                previous_message_id = int(raw_previous) if raw_previous is not None else None
            except (TypeError, ValueError):
                previous_message_id = None

        result = await self._send_text_result(
            _build_daily_recap_message(
                opened_trades=opened_trades,
                total_return_pct=summary["total_return_pct"],
                win_rate=summary["win_rate"],
                avg_r_multiple=summary["avg_r_multiple"],
                window_start=start,
                window_end=end,
            ),
            chat_id=self.settings.telegram_signal_chat_id,
            disable_web_page_preview=True,
        )
        if result is None:
            return {"status": "failed", "pinned": False}

        raw_message_id = result.get("message_id")
        try:
            message_id = int(raw_message_id) if raw_message_id is not None else None
        except (TypeError, ValueError):
            message_id = None

        unpinned_previous = False
        if previous_message_id is not None and previous_message_id != message_id:
            unpinned_previous = await self._unpin_message(
                chat_id=self.settings.telegram_signal_chat_id,
                message_id=previous_message_id,
            )

        pinned = False
        if message_id is not None:
            pinned = await self._pin_message(
                chat_id=self.settings.telegram_signal_chat_id,
                message_id=message_id,
            )

        state.value = {
            "message_id": message_id,
            "sent_at": end.isoformat(),
        }
        db.add(state)
        db.commit()
        return {
            "status": "sent",
            "message_id": message_id,
            "pinned": pinned,
            "unpinned_previous": unpinned_previous,
        }

    async def send_live_position_opened(self, position: Position) -> None:
        symbol = position.market.symbol if position.market else "?"
        side = position.side.upper()
        icon = "📈" if side == "LONG" else "📉"
        notional = float(position.size) * float(position.entry_price)
        contract_lev = (position.metadata_json or {}).get("contract_leverage")
        leverage = f"{int(contract_lev)}x" if contract_lev is not None else "—"
        now_str = datetime.now(UTC).strftime("%H:%M UTC")
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
        pnl_for_card = total_pnl if total_pnl is not None else step_pnl
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
        channel_text = _build_channel_close_message(
            symbol=symbol,
            side=side,
            reason=reason,
            exit_price=exit_price,
            is_partial=is_partial,
            pnl_pct=pnl_pct,
        )
        should_send_channel_photo = _should_send_channel_close_photo(
            is_partial=is_partial,
            pnl_pct=pnl_pct,
            pnl_usd=pnl_for_card,
        )
        channel_reason_allowed = reason.lower() in _CHANNEL_CLOSE_REASONS
        channel_should_notify = channel_reason_allowed or should_send_channel_photo

        png: bytes | None = None
        # Admin DM: invia la share card PNG per chiusure complete (non parziali)
        if not is_partial:
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
        # in a single thread. Profitable final closes get the share card; the
        # rest keep the lean text notification.
        if (
            self.settings.telegram_signal_chat_id
            and channel_should_notify
        ):
            try:
                reply_to = (
                    self._signal_channel_reply_id(db, signal_id)
                    if db is not None
                    else None
                )
                if should_send_channel_photo and png is not None:
                    sent = await self._send_photo(
                        png,
                        channel_text,
                        chat_id=self.settings.telegram_signal_chat_id,
                        reply_to_message_id=reply_to,
                    )
                    if not sent:
                        await self._send_text(
                            channel_text,
                            chat_id=self.settings.telegram_signal_chat_id,
                            reply_to_message_id=reply_to,
                        )
                else:
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

    async def send_macro_risk_state_changed(self, state: dict[str, Any]) -> None:
        if not self.settings.telegram_signal_chat_id:
            return
        level_raw = str(state.get("level") or "off")
        level = level_raw.upper()
        is_active = bool(state.get("active"))
        active = "ACTIVE · defensive rules enabled" if is_active else "OFF · normal rules restored"
        icon = _macro_risk_icon(level_raw, is_active)
        summary = escape(str(state.get("summary") or "Macro risk state changed"))
        reason = escape(_humanize_macro_tokens(state.get("reason")))
        sources = escape(_format_macro_sources(state.get("sources")))
        active_until = escape(_format_italian_datetime(state.get("active_until")))
        raw_policy = state.get("policy")
        policy: dict[str, Any] = raw_policy if isinstance(raw_policy, dict) else {}
        disabled_tfs = ", ".join(str(item) for item in policy.get("disable_timeframes", []) or []) or "none"
        mtf = "required" if policy.get("require_mtf_confluence") else "not required"
        expected_r_floor = policy.get("min_expected_r")
        try:
            expected_r_label = f">={float(expected_r_floor):.2f}R"
        except (TypeError, ValueError):
            expected_r_label = "not enforced"
        lines = [
            f"{icon} <b>Macro risk: {level}</b>",
            f"Status: <b>{active}</b>",
            summary,
            "",
            f"🧭 <b>Why:</b> {reason}",
            f"📰 <b>Sources:</b> {sources}",
            f"📊 <b>Risk score:</b> <code>{state.get('combined_score', 0)}/100</code> "
            f"· news <code>{state.get('news_score', 0)}</code> "
            f"· calendar <code>{state.get('calendar_score', 0)}</code> "
            f"· market <code>{state.get('market_stress_score', 0)}</code>",
            "",
            "🛡️ <b>Trading impact</b>",
            f"• Position size: <code>{policy.get('size_multiplier', 1)}x</code>",
            f"• Calibrated expected R: <code>{expected_r_label}</code>",
            f"• Paused timeframes: <code>{escape(disabled_tfs)}</code>",
            f"• MTF confluence: <code>{mtf}</code>",
            f"⏱️ <b>Active until:</b> {active_until}",
        ]
        news_lines = _macro_risk_news_lines(state.get("top_news"))
        if news_lines:
            lines.extend(["", "📰 <b>Top news</b>", *news_lines])
        calendar_lines = _macro_risk_calendar_lines(state.get("calendar"))
        if calendar_lines:
            lines.extend(["", "📅 <b>Macro calendar</b>", *calendar_lines])
        try:
            await self._send_text(
                "\n".join(lines),
                chat_id=self.settings.telegram_signal_chat_id,
            )
        except Exception as exc:
            logger.warning("telegram_macro_risk_send_failed", error=str(exc))

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
        macro_gate = PortfolioGuard(self.settings).evaluate_macro_risk_gate(db, signal)
        if not macro_gate.allowed:
            db.add(
                Alert(
                    signal_id=signal.id,
                    channel="telegram",
                    status="skipped",
                    payload={
                        **payload,
                        "skip_reason": macro_gate.reason,
                        "skip_details": macro_gate.details,
                    },
                )
            )
            db.commit()
            return
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
        alert.sent_at = datetime.now(UTC) if success else None
        alert.payload = {
            **payload,
            "telegram_status": response_status,
            "telegram_message_id": message_id,
        }
        db.commit()
