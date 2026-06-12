from app.api.routers import analytics as routes


def test_analytics_outcome_profile_delegates_to_builder(monkeypatch) -> None:  # noqa: ANN001
    seen: dict[str, object] = {}

    def fake_builder(db, *, lookback: int, mode: str | None = None):  # noqa: ANN001
        seen["db"] = db
        seen["lookback"] = lookback
        seen["mode"] = mode
        return {"filters": {"lookback": lookback, "mode": mode}, "overall": {"trades": 0}}

    db = object()
    monkeypatch.setattr(routes, "build_outcome_profile", fake_builder)

    payload = routes.analytics_outcome_profile(lookback=250, mode=None, db=db)  # type: ignore[arg-type]

    assert seen == {"db": db, "lookback": 250, "mode": None}
    assert payload["filters"]["lookback"] == 250


def test_analytics_outcome_profile_forwards_mode_filter(monkeypatch) -> None:  # noqa: ANN001
    seen: dict[str, object] = {}

    def fake_builder(db, *, lookback: int, mode: str | None = None):  # noqa: ANN001
        seen["mode"] = mode
        return {"filters": {"lookback": lookback, "mode": mode}, "overall": {"trades": 0}}

    monkeypatch.setattr(routes, "build_outcome_profile", fake_builder)

    payload = routes.analytics_outcome_profile(lookback=500, mode="paper", db=object())  # type: ignore[arg-type]

    assert seen["mode"] == "paper"
    assert payload["filters"]["mode"] == "paper"
