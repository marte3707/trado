from app.core.logging import _redact_secrets


def test_redacts_telegram_token_in_url_string() -> None:
    url = "https://api.telegram.org/bot1234567890:AAFqq-x9SOMEVERYLONGTOKEN1234567890/sendMessage"
    redacted = _redact_secrets(url)
    assert "1234567890:AAFqq" not in redacted
    assert "bot<redacted>" in redacted


def test_redacts_token_nested_in_dict() -> None:
    payload = {"err": "POST https://api.telegram.org/bot111:AAA-bbb-ccc-ddd-eee-fff-ggg-hhh failed"}
    redacted = _redact_secrets(payload)
    assert "111:AAA" not in redacted["err"]


def test_passes_through_strings_without_tokens() -> None:
    assert _redact_secrets("worker_tick started") == "worker_tick started"
