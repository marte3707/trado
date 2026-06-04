"""Genera una PNG di riepilogo trade, ispirata alle share card di Hyperliquid."""

from __future__ import annotations

import io
import math
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Colori
# ---------------------------------------------------------------------------
BG_COLOR = (10, 22, 18)        # verde scurissimo, come HL
GRID_COLOR = (16, 38, 28)      # sfumatura concentrica (simulata con anelli)
GREEN = (0, 220, 130)
RED = (255, 85, 85)
WHITE = (255, 255, 255)
MUTED = (120, 150, 135)
BADGE_LONG = (0, 180, 100)
BADGE_SHORT = (200, 60, 60)
BADGE_TEXT = (255, 255, 255)

# ---------------------------------------------------------------------------
# Font
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
    "/System/Library/Fonts/Supplemental/Verdana.ttf",
    # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
_FONT_REGULAR_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Verdana.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _find_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_W = 600
_H = 380


def _draw_ripple_bg(img: Image.Image) -> None:
    """Disegna anelli concentrici sfumati come sfondo."""
    draw = ImageDraw.Draw(img)
    cx, cy = _W // 2, _H // 2 + 30
    max_r = int(math.hypot(_W, _H))
    step = 28
    for r in range(step, max_r, step):
        alpha = max(0, 35 - r // 14)
        color = (16 + alpha, 36 + alpha, 28 + alpha)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=1)


def _rounded_rect(draw: ImageDraw.ImageDraw, xy: tuple, radius: int, fill: tuple) -> None:
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)


def _fmt_price(value: Decimal | None) -> str:
    if value is None:
        return "—"
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return f"{float(normalized):,.2f}"
    return f"{float(normalized):,g}"


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m:02d}m" if m else f"{h}h"


# ---------------------------------------------------------------------------
# Entrypoint pubblico
# ---------------------------------------------------------------------------

def render_close_card(
    *,
    symbol: str,
    side: str,
    leverage: Decimal | None,
    entry_price: Decimal,
    exit_price: Decimal | None,
    pnl_usd: Decimal | None,
    pnl_pct: float | None,
    duration_seconds: int | None,
    reason: str,
) -> bytes:
    """
    Restituisce il PNG come bytes pronti per sendPhoto di Telegram.
    """
    img = Image.new("RGB", (_W, _H), BG_COLOR)
    _draw_ripple_bg(img)
    draw = ImageDraw.Draw(img)

    is_long = side.upper() == "LONG"
    is_profit = (pnl_pct is not None and pnl_pct >= 0) or (pnl_usd is not None and float(pnl_usd) >= 0)
    accent = GREEN if is_profit else RED

    # ---- badge LONG/SHORT + leva ----------------------------------------
    badge_color = BADGE_LONG if is_long else BADGE_SHORT
    badge_label = side.upper()
    if leverage is not None and float(leverage) > 1:
        badge_label += f" {int(leverage)}X"
    font_badge = _find_font(_FONT_CANDIDATES, 18)
    bw = draw.textlength(badge_label, font=font_badge) + 24
    _rounded_rect(draw, (24, 24, 24 + bw, 24 + 32), radius=6, fill=badge_color)
    draw.text((24 + 12, 28), badge_label, font=font_badge, fill=BADGE_TEXT)

    # ---- simbolo -----------------------------------------------------------
    font_symbol = _find_font(_FONT_CANDIDATES, 26)
    draw.text((24, 70), symbol, font=font_symbol, fill=WHITE)

    # ---- PnL % grande al centro -------------------------------------------
    pct_text = "—"
    if pnl_pct is not None:
        sign = "+" if pnl_pct >= 0 else ""
        pct_text = f"{sign}{pnl_pct:.1f}%"
    font_big = _find_font(_FONT_CANDIDATES, 90)
    tw = draw.textlength(pct_text, font=font_big)
    draw.text((_W // 2 - tw // 2, 95), pct_text, font=font_big, fill=accent)

    # ---- riga prezzi -------------------------------------------------------
    font_label = _find_font(_FONT_REGULAR_CANDIDATES, 16)
    font_value = _find_font(_FONT_CANDIDATES, 18)

    y_row = 218
    # Entry
    draw.text((24, y_row), "Entry Price", font=font_label, fill=MUTED)
    draw.text((24, y_row + 20), _fmt_price(entry_price), font=font_value, fill=WHITE)
    # Exit price
    if exit_price is not None:
        ex_x = 160
        draw.text((ex_x, y_row), "Exit Price", font=font_label, fill=MUTED)
        draw.text((ex_x, y_row + 20), _fmt_price(exit_price), font=font_value, fill=WHITE)
    # Duration
    if duration_seconds is not None:
        dur_text = _fmt_duration(duration_seconds)
        dur_x = 310
        draw.text((dur_x, y_row), "Duration", font=font_label, fill=MUTED)
        draw.text((dur_x, y_row + 20), dur_text, font=font_value, fill=WHITE)

    # ---- linea separatore --------------------------------------------------
    draw.line([(24, 270), (_W - 24, 270)], fill=(30, 60, 45), width=1)

    font_brand = _find_font(_FONT_REGULAR_CANDIDATES, 14)

    # ---- branding + motivo (prima riga footer) ----------------------------
    draw.text((24, 282), "Trado · @crypto_free_live_signals", font=font_brand, fill=MUTED)

    reason_map = {
        "tp1": "TP1 ✓", "tp2": "TP2 ✓", "tp3": "TP3 ✓",
        "stop_loss": "Stop Loss", "trailing_stop": "Trailing Stop",
        "manual": "Manual", "take_profit": "Take Profit",
        "btc_macro_protection_loss_exit": "BTC Macro Stop",
        "btc_macro_protection_breakeven": "BTC Macro BE",
        "reconciliazione hl": "HL Reconciled",
    }
    reason_label = reason_map.get(reason.lower(), reason.upper())
    rw = draw.textlength(reason_label, font=font_brand)
    draw.text((_W - 24 - rw, 282), reason_label, font=font_brand, fill=MUTED)

    # ---- CTA referral (seconda riga footer) --------------------------------
    draw.text((24, 302), "-> Trade on Hyperliquid & get 4% fee discount", font=font_brand, fill=(60, 160, 110))
    draw.text((24, 318), "   app.hyperliquid.xyz/join/CRYPTOFREESIGNALS", font=font_brand, fill=(60, 160, 110))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
