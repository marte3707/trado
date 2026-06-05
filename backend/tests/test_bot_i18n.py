from app.bot.i18n import t


def test_disclaimer_mentions_score_risk_and_full_sample_in_italian() -> None:
    text = t("disclaimer_prompt", "it")

    assert "sperimentale e gratuito" in text
    assert "score alto" in text
    assert "matematica di successo" in text
    assert "limiti di perdita giornalieri" in text
    assert "intero flusso di trade" in text


def test_help_mentions_risk_and_track_record_in_english() -> None:
    text = t("help", "en")

    assert "How Trado works" in text
    assert "not a mathematical probability of success" in text
    assert "daily loss limits" in text
    assert "portfolio-level" in text


def test_invite_message_mentions_free_channel_in_both_languages() -> None:
    assert "gratuito" in t("invite_message", "it")
    assert "free" in t("invite_message", "en")
