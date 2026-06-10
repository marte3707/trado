import inspect

from app.engine.regime import apply_regime_filter
from app.engine.signal_engine import SignalEngine


def test_regime_filter_kwargs_defaults() -> None:
    kw = SignalEngine._regime_filter_kwargs({})
    assert kw["method"] == "adx"
    assert kw["adx_trending"] == 25.0
    assert kw["adx_ranging"] == 18.0
    assert kw["confirm_candles"] == 2
    assert kw["garch_cluster_threshold"] == 0.2


def test_regime_filter_kwargs_applies_overrides() -> None:
    kw = SignalEngine._regime_filter_kwargs(
        {"regime_method": "HURST", "regime_adx_trending": 30, "regime_hurst_min_lag": 5}
    )
    assert kw["method"] == "hurst"
    assert kw["adx_trending"] == 30.0
    assert kw["hurst_min_lag"] == 5


def test_regime_filter_kwargs_normalises_invalid_method() -> None:
    assert SignalEngine._regime_filter_kwargs({"regime_method": "bogus"})["method"] == "adx"


def test_regime_filter_kwargs_keys_are_valid_for_apply_regime_filter() -> None:
    """Le chiavi prodotte devono essere parametri reali di apply_regime_filter,
    così l'espansione `**kwargs` resta valida anche se la firma cambia."""
    params = set(inspect.signature(apply_regime_filter).parameters)
    produced = set(SignalEngine._regime_filter_kwargs({}))
    assert produced <= params
