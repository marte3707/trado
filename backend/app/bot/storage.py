"""Persistence helpers for the onboarding bot. Thin layer over BotUser ORM."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.models import BotUser


def get_or_create(
    db: Session,
    *,
    telegram_user_id: int,
    telegram_username: str | None,
    first_name: str | None,
    source: str | None,
) -> BotUser:
    """Look up the bot user by telegram_user_id, create if missing. Refreshes
    username and first_name on each /start to track renames."""
    user = (
        db.query(BotUser)
        .filter(BotUser.telegram_user_id == telegram_user_id)
        .one_or_none()
    )
    if user is None:
        user = BotUser(
            telegram_user_id=telegram_user_id,
            telegram_username=telegram_username,
            first_name=first_name,
            source=source,
            onboarding_stage="started",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    # Update lightweight metadata on every /start. Don't overwrite source: the
    # first attribution wins, so a user who started from Reddit and later
    # restarts the flow without parameter is still attributed to Reddit.
    dirty = False
    if telegram_username and user.telegram_username != telegram_username:
        user.telegram_username = telegram_username
        dirty = True
    if first_name and user.first_name != first_name:
        user.first_name = first_name
        dirty = True
    if source and not user.source:
        user.source = source
        dirty = True
    if dirty:
        db.commit()
        db.refresh(user)
    return user


def set_language(db: Session, user: BotUser, language: str) -> BotUser:
    user.language = "it" if language == "it" else "en"
    if user.onboarding_stage == "started":
        user.onboarding_stage = "language_set"
    db.commit()
    db.refresh(user)
    return user


def set_trade_capital(db: Session, user: BotUser, trade_capital: str | None) -> BotUser:
    """Set the user's declared trading capital range. None means the user
    chose to skip the question — still advances onboarding."""
    user.trade_capital = trade_capital
    user.onboarding_stage = "capital_set"
    db.commit()
    db.refresh(user)
    return user


def record_risk_ack(db: Session, user: BotUser, *, invite_link: str | None) -> BotUser:
    user.risk_ack_at = datetime.now(UTC)
    user.invite_link = invite_link
    user.onboarding_stage = "completed" if invite_link else "ack_no_link"
    db.commit()
    db.refresh(user)
    return user


def record_decline(db: Session, user: BotUser) -> BotUser:
    user.onboarding_stage = "declined"
    db.commit()
    db.refresh(user)
    return user


def set_paused(db: Session, user: BotUser, paused: bool) -> BotUser:
    user.paused = paused
    db.commit()
    db.refresh(user)
    return user
