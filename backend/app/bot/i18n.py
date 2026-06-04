"""Bilingual (IT/EN) text bundle for the onboarding bot.

All user-facing strings live here. Each key is available in both languages so
the bot can switch language at runtime based on the user's pick.
"""
from __future__ import annotations

from typing import Literal

Language = Literal["it", "en"]

_DEFAULT: Language = "en"

# Texts use HTML parse mode (Telegram). Keep tags balanced.
TEXTS: dict[str, dict[Language, str]] = {
    "language_prompt": {
        "it": (
            "👋 Ciao! Sono il bot di <b>Trado</b>.\n\n"
            "Prima di darti accesso al canale segnali, ho un paio di domande veloci "
            "(15 secondi).\n\n"
            "🇬🇧 Hi! I'm the <b>Trado</b> bot. A couple of quick questions before "
            "I send you the channel link (15 seconds).\n\n"
            "🌐 Choose your language / Scegli la lingua:"
        ),
        "en": (
            "👋 Ciao! Sono il bot di <b>Trado</b>.\n\n"
            "Prima di darti accesso al canale segnali, ho un paio di domande veloci "
            "(15 secondi).\n\n"
            "🇬🇧 Hi! I'm the <b>Trado</b> bot. A couple of quick questions before "
            "I send you the channel link (15 seconds).\n\n"
            "🌐 Choose your language / Scegli la lingua:"
        ),
    },
    "language_set": {
        "it": "✅ Lingua impostata su <b>Italiano</b>.",
        "en": "✅ Language set to <b>English</b>.",
    },
    "trade_capital_prompt": {
        "it": (
            "💰 <b>Qual è il capitale che pensi di dedicare al trading crypto?</b>\n\n"
            "Mi aiuta a capire meglio la nostra community. Risposta facoltativa, "
            "non condividiamo questa informazione con nessuno."
        ),
        "en": (
            "💰 <b>How much capital do you plan to dedicate to crypto trading?</b>\n\n"
            "This helps me understand our community better. Optional — we don't "
            "share this with anyone."
        ),
    },
    "disclaimer_prompt": {
        "it": (
            "⚠️ <b>Prima di procedere, leggi e accetta:</b>\n\n"
            "I segnali pubblicati da Trado hanno finalità <b>informativa ed "
            "educativa</b>. Non costituiscono consulenza finanziaria personalizzata "
            "ai sensi del TUF (D.Lgs. 58/1998).\n\n"
            "Il trading di derivati crypto comporta <b>rischio di perdita totale del "
            "capitale</b> ed è adatto solo a investitori che comprendono pienamente i "
            "rischi. I risultati passati non garantiscono risultati futuri.\n\n"
            "Trado è un sistema sperimentale. Tu sei l'unico responsabile delle tue "
            "decisioni di investimento.\n\n"
            "Procedi solo se accetti queste condizioni."
        ),
        "en": (
            "⚠️ <b>Before we continue, please read and accept:</b>\n\n"
            "Trado's signals are provided for <b>informational and educational</b> "
            "purposes only. They do not constitute personalized financial advice.\n\n"
            "Trading crypto derivatives carries <b>risk of total capital loss</b> and "
            "is suitable only for investors who fully understand the risks. Past "
            "performance does not guarantee future results.\n\n"
            "Trado is an experimental system. You are solely responsible for your "
            "own investment decisions.\n\n"
            "Continue only if you accept these terms."
        ),
    },
    "declined": {
        "it": (
            "Nessun problema. Se cambi idea, manda /start per ricominciare.\n"
            "Grazie e in bocca al lupo. 👋"
        ),
        "en": (
            "No worries. If you change your mind, send /start to begin again.\n"
            "Thanks and good luck. 👋"
        ),
    },
    "invite_message": {
        "it": (
            "🎉 <b>Tutto pronto!</b>\n\n"
            "Ecco il tuo invito personale al canale segnali Trado:\n"
            "{invite_link}\n\n"
            "Il link è <b>monouso</b> e scade tra 24 ore.\n\n"
            "Comandi utili:\n"
            "/help — elenco comandi\n"
            "/disclaimer — rivedi il disclaimer\n"
            "/privacy — informativa privacy\n"
            "/pause — sospendi notifiche\n"
            "/lang — cambia lingua"
        ),
        "en": (
            "🎉 <b>You're all set!</b>\n\n"
            "Here's your personal invite to the Trado signals channel:\n"
            "{invite_link}\n\n"
            "The link is <b>single-use</b> and expires in 24 hours.\n\n"
            "Useful commands:\n"
            "/help — list of commands\n"
            "/disclaimer — review the disclaimer\n"
            "/privacy — privacy notice\n"
            "/pause — pause notifications\n"
            "/lang — change language"
        ),
    },
    "invite_failed": {
        "it": (
            "❌ Non riesco a generare il link al canale in questo momento. "
            "Per favore riprova tra qualche minuto con /start, oppure contatta "
            "l'amministratore."
        ),
        "en": (
            "❌ I couldn't generate the channel link right now. Please try /start "
            "again in a few minutes, or contact the admin."
        ),
    },
    "help": {
        "it": (
            "<b>Comandi disponibili:</b>\n\n"
            "/start — inizia o ricomincia l'onboarding\n"
            "/help — questo messaggio\n"
            "/disclaimer — rivedi il disclaimer di rischio\n"
            "/privacy — informativa sul trattamento dati\n"
            "/pause — sospendi le notifiche\n"
            "/resume — riattiva le notifiche\n"
            "/lang — cambia lingua (it / en)"
        ),
        "en": (
            "<b>Available commands:</b>\n\n"
            "/start — start or restart onboarding\n"
            "/help — this message\n"
            "/disclaimer — review the risk disclaimer\n"
            "/privacy — privacy notice\n"
            "/pause — pause notifications\n"
            "/resume — resume notifications\n"
            "/lang — change language (it / en)"
        ),
    },
    "privacy": {
        "it": (
            "<b>Informativa privacy (sintetica)</b>\n\n"
            "Trado registra: il tuo <b>Telegram user ID</b>, lo username (se "
            "presente), la lingua scelta, il livello di esperienza dichiarato e la "
            "fonte da cui sei arrivato. Non raccogliamo né numeri di telefono né "
            "indirizzi email.\n\n"
            "Finalità: gestione del canale segnali ed eventuali comunicazioni di "
            "servizio. Base giuridica: consenso (art. 6.1.a GDPR).\n\n"
            "Puoi chiedere la cancellazione dei tuoi dati in qualsiasi momento "
            "scrivendo all'amministratore."
        ),
        "en": (
            "<b>Privacy notice (summary)</b>\n\n"
            "Trado stores: your <b>Telegram user ID</b>, your username (if any), "
            "your selected language, your declared experience level, and the source "
            "you came from. We do not collect phone numbers or email addresses.\n\n"
            "Purpose: managing the signals channel and service communications. "
            "Legal basis: consent (GDPR art. 6.1.a).\n\n"
            "You can request data deletion at any time by contacting the admin."
        ),
    },
    "paused": {
        "it": "🔕 Notifiche sospese. Usa /resume per riattivarle.",
        "en": "🔕 Notifications paused. Use /resume to re-enable.",
    },
    "resumed": {
        "it": "🔔 Notifiche riattivate.",
        "en": "🔔 Notifications resumed.",
    },
    "lang_prompt": {
        "it": "🌐 Scegli la lingua:",
        "en": "🌐 Choose your language:",
    },
    "unknown_command": {
        "it": "Comando non riconosciuto. Manda /help per la lista dei comandi.",
        "en": "Unknown command. Send /help to see available commands.",
    },
    "start_again_hint": {
        "it": "Manda /start per cominciare.",
        "en": "Send /start to begin.",
    },
    # Button labels
    "btn_lang_it": {"it": "🇮🇹 Italiano", "en": "🇮🇹 Italiano"},
    "btn_lang_en": {"it": "🇬🇧 English", "en": "🇬🇧 English"},
    "btn_cap_under_1k": {"it": "Meno di $1.000", "en": "Less than $1,000"},
    "btn_cap_1k_to_10k": {"it": "$1.000 – $10.000", "en": "$1,000 – $10,000"},
    "btn_cap_over_10k": {"it": "Oltre $10.000", "en": "More than $10,000"},
    "btn_skip": {"it": "🤫 Preferisco non dirlo", "en": "🤫 Prefer not to say"},
    "btn_accept": {"it": "✅ Accetto e procedo", "en": "✅ I accept, continue"},
    "btn_decline": {"it": "❌ Non accetto", "en": "❌ I decline"},
}


def t(key: str, language: str | None) -> str:
    """Lookup a translated string. Falls back to English if the requested
    language is missing or unknown."""
    lang: Language = "it" if language == "it" else _DEFAULT
    bundle = TEXTS.get(key)
    if not bundle:
        return key
    return bundle.get(lang) or bundle.get(_DEFAULT) or key


def normalize_language(value: str | None) -> Language:
    if value and value.lower().startswith("it"):
        return "it"
    return _DEFAULT
