from app.api import routes


def test_analytics_outcome_profile_delegates_to_builder(monkeypatch) -> None:  # noqa: ANN001
    seen: dict[str, object] = {}

    def fake_builder(db, *, lookback: int):  # noqa: ANN001
        seen["db"] = db
        seen["lookback"] = lookback
        return {"filters": {"lookback": lookback}, "overall": {"trades": 0}}

    db = object()
    monkeypatch.setattr(routes, "build_outcome_profile", fake_builder)

    payload = routes.analytics_outcome_profile(lookback=250, db=db)  # type: ignore[arg-type]

    assert seen == {"db": db, "lookback": 250}
    assert payload["filters"]["lookback"] == 250
