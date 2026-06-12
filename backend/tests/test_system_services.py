import subprocess

from app.api.routers import system as routes


def test_bot_registered_in_system_services() -> None:
    assert "bot" in routes.SYSTEM_SERVICES
    config = routes.SYSTEM_SERVICES["bot"]
    assert config["label"] == "com.trado.bot"
    assert str(config["plist"]).endswith("com.trado.bot.plist")
    assert str(config["error_log"]).endswith("trado-bot.err.log")


def test_bot_status_parses_running_service(monkeypatch) -> None:  # noqa: ANN001
    output = (
        "com.trado.bot = {\n"
        "\tactive count = 1\n"
        "\tstate = running\n"
        "\tprogram = /usr/bin/python3\n"
        "\tpid = 12345\n"
        "}\n"
    )

    def fake_run(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    status = routes._bot_status()

    assert status["running"] is True
    assert status["pid"] == 12345
    assert status["label"] == "com.trado.bot"


def test_bot_status_handles_missing_service(monkeypatch) -> None:  # noqa: ANN001
    def fake_run(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return subprocess.CompletedProcess(
            args=[], returncode=113, stdout="", stderr="Could not find service"
        )

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    status = routes._bot_status()

    assert status["running"] is False
    assert status["pid"] is None


def test_bot_status_handles_launchctl_failure(monkeypatch) -> None:  # noqa: ANN001
    def fake_run(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError("launchctl not available")

    monkeypatch.setattr(routes.subprocess, "run", fake_run)

    status = routes._bot_status()

    assert status["running"] is False
    assert status["source"] == "launchctl_unavailable"


def test_system_service_action_accepts_bot(monkeypatch) -> None:  # noqa: ANN001
    scheduled: dict[str, str] = {}

    def fake_schedule(service: str, action: str) -> None:
        scheduled["service"] = service
        scheduled["action"] = action

    monkeypatch.setattr(routes, "_schedule_service_action", fake_schedule)

    payload = routes.system_service_action(service="bot", action="reboot")

    assert scheduled == {"service": "bot", "action": "reboot"}
    assert payload["status"] == "scheduled"
    assert payload["label"] == "com.trado.bot"
