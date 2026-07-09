"""Regression tests for market_brief NaN serialization (daily morning-brief crash).

The bug: yfinance can yield NaN for a ticker's latest close, which propagated into
last/chg/rsi and made the JSON response non-compliant — Starlette's JSONResponse
serializes with json.dumps(allow_nan=False), raising
"Out of range float values are not JSON compliant: nan".
"""
import json
import math

from worker.handlers.market_brief import _finite, _format


def _strict_json(obj):
    """Mirror Starlette JSONResponse: reject non-finite floats."""
    return json.dumps(obj, allow_nan=False)


def test_finite_scrubs_non_finite():
    assert _finite(float("nan")) is None
    assert _finite(float("inf")) is None
    assert _finite(float("-inf")) is None
    assert _finite(12.5) == 12.5
    assert _finite("AAPL") == "AAPL"


def test_nan_row_is_json_serializable():
    # A row built the way the handler builds it, but with a NaN latest close.
    row = {
        "symbol": "BAD",
        "last": _finite(float("nan")),
        "chg": _finite(float("nan")),
        "rsi": _finite(float("nan")),
        "signals": [],
    }
    # Would raise ValueError before the fix.
    _strict_json({"data": [row], "response": _format([row])})


def test_format_handles_missing_data():
    rows = [
        {"symbol": "AAPL", "last": 195.0, "chg": 1.2, "rsi": 55.0, "signals": []},
        {"symbol": "BAD", "last": None, "chg": None, "rsi": None, "signals": []},
    ]
    out = _format(rows)
    assert "AAPL" in out and "195.00" in out
    assert "BAD: data unavailable" in out


def test_healthy_row_still_serializes():
    row = {"symbol": "SPY", "last": 500.12, "chg": -0.4, "rsi": 48.0, "signals": ["gap +2.1%"]}
    _strict_json({"data": [row], "response": _format([row])})
    assert math.isfinite(row["last"])
