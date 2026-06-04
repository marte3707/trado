# Landing page + onboarding bot — setup

Phase 1 of the Telegram funnel: a bilingual (IT/EN) public landing page and a
Telegram onboarding bot. Live track-record will be added later.

## Architecture

```
[Reddit / X / blog]  →  GET /join  →  https://t.me/<bot>?start=<src>  →  signals channel
                       (Next.js)        (onboarding bot)              (Telegram channel)
```

Three pieces:
- **Frontend** — public route at `/join` (Next.js, no auth)
- **Backend API** — exposes `GET /api/public/bot-info` (no auth)
- **Bot process** — separate Python process (`app.bot.main`) using Telegram long polling

## Two-bot setup (recommended)

Trado uses **two separate bots** so that admin notifications (account balances,
worker errors, live position events) stay private while a different public-facing
identity handles the signals channel and user DMs.

| Bot                     | Token env var          | Role                                                                |
|-------------------------|------------------------|---------------------------------------------------------------------|
| Admin bot (existing)    | `TELEGRAM_BOT_TOKEN`   | Recap orario, worker errors, live position open/close — DM to you   |
| Public bot (new)        | `LANDING_BOT_TOKEN`    | Posts signals to the public channel + handles onboarding DMs        |

If `LANDING_BOT_TOKEN` is empty, both roles fall back to `TELEGRAM_BOT_TOKEN`
(legacy single-bot mode — convenient for early testing).

### Telegram setup (one time)

1. **Create the PUBLIC bot** via [@BotFather](https://t.me/BotFather)
   - `/newbot` → pick a name and username
   - Save the token → `LANDING_BOT_TOKEN`
   - Save the username (without `@`) → `LANDING_BOT_USERNAME`
   - Set a public description: `/setdescription`, e.g. *"Official Trado bot. Send /start to join the free signals channel."*

2. **Promote the PUBLIC bot to admin** of the signals channel (`@crypto_free_live_signals`)
   - Required permissions: **Post messages** + **Invite users via link**
   - (Optional but recommended) **Remove** the admin bot from the channel admins so it stays purely a private-DM bot

3. **Recover the channel chat ID** (if you don't have it already)
   - Forward any message from the channel to [@RawDataBot](https://t.me/RawDataBot), copy `forward_from_chat.id` (format `-100xxxxxxxxxx`)
   - Put it into `TELEGRAM_SIGNAL_CHAT_ID`

4. **Test the deep link**
   - `https://t.me/<LANDING_BOT_USERNAME>?start=landing` should open Telegram on the public bot with a Start button

## Environment variables

In `.env`:

```bash
# Admin bot — private DMs to you only
TELEGRAM_BOT_TOKEN=123456:ADMIN-token...
TELEGRAM_CHAT_ID=<your-personal-chat-id>

# Public channel
TELEGRAM_SIGNAL_CHAT_ID=-1001234567890

# Public bot — signals to channel + onboarding
LANDING_BOT_TOKEN=789012:PUBLIC-token...
LANDING_BOT_USERNAME=TradoSignalBot           # no leading @
```

`CORS_ALLOWED_ORIGINS` must include the origin you serve the landing from. For
local dev (`npm run frontend:dev`) the default `http://localhost:3000` is fine.

## DB migration

Applied automatically on backend startup. To run manually:

```bash
cd backend && ../venv/bin/python -m alembic upgrade head
```

## Run the bot

Local dev:

```bash
cd backend && ../venv/bin/python -m app.bot.main
```

Production (launchd):

```bash
cp deploy/com.trado.bot.plist ~/Library/LaunchAgents/com.trado.bot.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.bot.plist
```

Restart after backend code changes (alongside backend + worker):

```bash
launchctl bootout gui/$(id -u)/com.trado.bot
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.trado.bot.plist
```

Logs:
- `~/Library/Logs/trado-bot.out.log`
- `~/Library/Logs/trado-bot.err.log`

## Conversation flow

The bot enforces this sequence (state stored in `bot_users` table):

1. `/start [source]` — welcomes the user in both languages, asks language pick
2. Language picked (inline keyboard) → asks experience level
3. Experience picked → shows risk disclaimer + Accept/Decline buttons
4. On Accept → calls `createChatInviteLink` on `TELEGRAM_SIGNAL_CHAT_ID` (single-use, 24h TTL) and DMs the link
5. On Decline → records decline, ends gracefully

Available commands: `/start /help /disclaimer /privacy /pause /resume /lang`.

## Source attribution

Deep links can carry a source tag: `https://t.me/<bot>?start=reddit_algotrading`.
The bot saves the first source it sees for each user (re-running `/start` with a
new source does **not** overwrite the original attribution).

The landing page accepts `?ref=<source>` and `?lang=it|en` via query string. The
default source when none is given is `landing`.

## Adding more languages later

All bot strings live in [backend/app/bot/i18n.py](../backend/app/bot/i18n.py); all
landing strings live in [frontend/lib/landing-i18n.ts](../frontend/lib/landing-i18n.ts).
Adding a third language = adding a `Language` literal and a key bundle in each
file. No code changes required.
