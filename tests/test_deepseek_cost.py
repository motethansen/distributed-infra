"""DeepSeek cost_usd: computed from config/llm_prices.yaml, summed across the first call and the retry."""
import asyncio

import pytest

from agents import deepseek_agent as ds

PRICES = """deepseek:
  deepseek-chat: {input_fresh: 1.0, input_cache_hit: 0.1, output: 2.0, verified: true}
  deepseek-unverified: {input_fresh: 1.0, input_cache_hit: 0.1, output: 2.0, verified: false}
"""
DSML = "<function_calls><invoke name=\"bash\"></invoke></function_calls>"


@pytest.fixture(autouse=True)
def prices(tmp_path, monkeypatch):
    f = tmp_path / "prices.yaml"
    f.write_text(PRICES)
    monkeypatch.setattr(ds, "PRICES_PATH", f)
    ds._load_prices.cache_clear()
    monkeypatch.setenv("DEEPSEEK_API", "test-key")
    yield
    ds._load_prices.cache_clear()


class _Resp:
    def __init__(self, content, usage):
        self._d = {"choices": [{"message": {"content": content}}]}
        if usage is not None:
            self._d["usage"] = usage

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def _fake_client(monkeypatch, resps):
    import httpx
    queue = list(resps)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return queue.pop(0)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def test_single_call_cost_comes_from_the_table(monkeypatch):
    _fake_client(monkeypatch, [_Resp("hello", {"prompt_tokens": 1_000_000, "completion_tokens": 500_000})])
    out = asyncio.run(ds.run("hi"))
    assert out["agent"] == "deepseek" and out["model"] == "deepseek-chat"
    assert out["usage"] == {"tokens_in": 1_000_000, "tokens_out": 500_000, "cost_usd": pytest.approx(2.0)}


def test_cache_hit_tokens_are_priced_at_the_cache_rate(monkeypatch):
    _fake_client(monkeypatch, [_Resp("ok", {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                                            "prompt_cache_hit_tokens": 600_000, "prompt_cache_miss_tokens": 400_000})])
    out = asyncio.run(ds.run("hi"))
    assert out["usage"]["cost_usd"] == pytest.approx(0.4 * 1.0 + 0.6 * 0.1)


def test_cost_of_first_call_and_retry_is_summed(monkeypatch):
    _fake_client(monkeypatch, [_Resp(DSML, {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}),
                               _Resp("done", {"prompt_tokens": 2_000_000, "completion_tokens": 200_000})])
    out = asyncio.run(ds.run("hi"))
    assert out["ok"] is True
    assert out["usage"]["tokens_in"] == 3_000_000 and out["usage"]["tokens_out"] == 300_000
    assert out["usage"]["cost_usd"] == pytest.approx((1.0 + 0.2) + (2.0 + 0.4))


def test_text_only_failure_after_retry_sums_both_calls(monkeypatch):
    _fake_client(monkeypatch, [_Resp(DSML, {"prompt_tokens": 1_000_000, "completion_tokens": 0}),
                               _Resp(DSML, {"prompt_tokens": 1_000_000, "completion_tokens": 0})])
    out = asyncio.run(ds.run("hi"))
    assert out["ok"] is False and out["model"] == "deepseek-chat"
    assert out["usage"]["cost_usd"] == pytest.approx(2.0)


def test_no_usage_reported_leaves_no_numbers(monkeypatch):
    _fake_client(monkeypatch, [_Resp("hello", None)])
    out = asyncio.run(ds.run("hi"))
    assert out["ok"] is True and "usage" not in out


def test_partial_usage_keeps_missing_fields_none(monkeypatch):
    _fake_client(monkeypatch, [_Resp("hello", {"prompt_tokens": 10})])
    assert asyncio.run(ds.run("hi"))["usage"]["tokens_out"] is None


def test_unknown_model_gives_cost_none_but_keeps_tokens(monkeypatch):
    _fake_client(monkeypatch, [_Resp("hello", {"prompt_tokens": 10, "completion_tokens": 5})])
    out = asyncio.run(ds.run("hi", model="deepseek-mystery"))
    assert out["usage"] == {"tokens_in": 10, "tokens_out": 5, "cost_usd": None, "cost_basis": "unpriced"}


def test_missing_price_file_gives_cost_none(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "PRICES_PATH", tmp_path / "nope.yaml")
    ds._load_prices.cache_clear()
    _fake_client(monkeypatch, [_Resp("hello", {"prompt_tokens": 10, "completion_tokens": 5})])
    assert asyncio.run(ds.run("hi"))["usage"]["cost_usd"] is None


def test_shipped_price_file_covers_the_repo_models():
    ds._load_prices.cache_clear()
    monkeypatch_path = ds.Path(ds.__file__).resolve().parent.parent / "config" / "llm_prices.yaml"
    import yaml
    table = yaml.safe_load(monkeypatch_path.read_text())["deepseek"]
    assert {"deepseek-chat", "deepseek-flash", "deepseek-v4-pro"} <= set(table)     # the names the code and the API use


def test_unverified_price_gives_cost_none_marked_unpriced(monkeypatch):
    _fake_client(monkeypatch, [_Resp("hello", {"prompt_tokens": 10, "completion_tokens": 5})])
    u = asyncio.run(ds.run("hi", model="deepseek-unverified"))["usage"]
    assert u["cost_usd"] is None and u["cost_basis"] == "unpriced"


def test_verified_price_gives_a_number(monkeypatch):
    _fake_client(monkeypatch, [_Resp("hello", {"prompt_tokens": 10, "completion_tokens": 5})])
    u = asyncio.run(ds.run("hi"))["usage"]
    assert isinstance(u["cost_usd"], float) and "cost_basis" not in u


def test_shipped_prices_only_mark_verified_what_the_official_page_lists():
    """Verified entries are the two models on DeepSeek's price page and the legacy name the API serves as flash;
    deepseek-reasoner is not on the page and must stay unpriced (absent or verified: false), never guessed."""
    ds._load_prices.cache_clear()
    path = ds.Path(ds.__file__).resolve().parent.parent / "config" / "llm_prices.yaml"
    import yaml
    table = yaml.safe_load(path.read_text())["deepseek"]
    verified = {m for m, v in table.items() if v.get("verified") is True}
    assert verified == {"deepseek-flash", "deepseek-v4-pro", "deepseek-chat"}
    assert table["deepseek-chat"]["alias_of"] == "deepseek-flash"


def test_cache_breakdown_on_one_attempt_does_not_underprice_the_other(monkeypatch):
    _fake_client(monkeypatch, [
        _Resp(DSML, {"prompt_tokens": 100, "completion_tokens": 0}),
        _Resp("ok", {"prompt_tokens": 100, "completion_tokens": 0,
                     "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40})])
    u = asyncio.run(ds.run("hi"))["usage"]
    assert u["tokens_in"] == 200
    # attempt 1: 100 miss * 1.0; attempt 2: 40 miss * 1.0 + 60 hit * 0.1  (per million)
    assert u["cost_usd"] == pytest.approx((100 * 1.0 + 40 * 1.0 + 60 * 0.1) / 1_000_000)


def test_retry_without_usage_does_not_record_the_first_call_as_the_whole_cost(monkeypatch):
    """First call reports usage, the retry reports none: the retry was paid for but cannot be priced, so the total is unknown (NULL), not the first call's cost."""
    _fake_client(monkeypatch, [_Resp(DSML, {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}),
                               _Resp("done", None)])
    out = asyncio.run(ds.run("hi"))
    assert out["ok"] is True
    assert out["usage"]["cost_usd"] is None and out["usage"]["cost_basis"] == "unpriced"
    assert out["usage"]["tokens_in"] == 1_000_000          # the tokens that WERE reported are still kept


def test_first_call_without_usage_and_priced_retry_is_also_unknown(monkeypatch):
    _fake_client(monkeypatch, [_Resp(DSML, None), _Resp("done", {"prompt_tokens": 1_000_000, "completion_tokens": 0})])
    out = asyncio.run(ds.run("hi"))
    assert out["usage"]["cost_usd"] is None


# ---- time-aware official prices (DeepSeek price page, 2026-11-06): off-peak is half of peak ----------------------------------------------------
from datetime import datetime, timezone

OFFICIAL = """deepseek:
  deepseek-flash:
    verified: true
    offpeak: {input_fresh: 0.15, input_cache_hit: 0.003, output: 0.60}
    peak:    {input_fresh: 0.30, input_cache_hit: 0.006, output: 1.20}
  deepseek-chat:
    alias_of: deepseek-flash
    verified: true
  deepseek-broken-alias:
    alias_of: deepseek-flash
    verified: false
"""
USAGE_1M = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1_000_000}


def _pin(monkeypatch, tmp_path, y, mo, d, h):
    f = tmp_path / "official.yaml"
    f.write_text(OFFICIAL)
    monkeypatch.setattr(ds, "PRICES_PATH", f)
    ds._load_prices.cache_clear()
    monkeypatch.setattr(ds, "_now", lambda: datetime(y, mo, d, h, 30, tzinfo=timezone.utc))


@pytest.mark.parametrize("hour,peak", [(0, False), (1, True), (3, True), (4, False), (5, False), (6, True), (9, True), (10, False), (23, False)])
def test_peak_hours_are_01_04_and_06_10_utc_on_weekdays(monkeypatch, tmp_path, hour, peak):
    _pin(monkeypatch, tmp_path, 2026, 11, 6, hour)            # 2026-11-06 is a Friday
    assert ds._cost_usd(USAGE_1M, "deepseek-flash") == pytest.approx(1.50 if peak else 0.75)


def test_weekend_is_always_off_peak(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path, 2026, 11, 7, 7)               # Saturday 07:30 UTC would be peak on a weekday
    assert ds._cost_usd(USAGE_1M, "deepseek-flash") == pytest.approx(0.75)


def test_legacy_name_is_priced_as_flash_and_a_broken_alias_is_unpriced(monkeypatch, tmp_path):
    _pin(monkeypatch, tmp_path, 2026, 11, 6, 7)
    assert ds._cost_usd(USAGE_1M, "deepseek-chat") == ds._cost_usd(USAGE_1M, "deepseek-flash") == pytest.approx(1.50)
    assert ds._cost_usd(USAGE_1M, "deepseek-broken-alias") is None
    assert ds._cost_usd(USAGE_1M, "deepseek-reasoner") is None


def test_shipped_price_file_matches_the_official_page_numbers(monkeypatch):
    """The real config/llm_prices.yaml: flash and pro at the official peak and off-peak rates, the legacy name aliased."""
    import pathlib
    real = pathlib.Path(ds.__file__).resolve().parents[1] / "config" / "llm_prices.yaml"
    monkeypatch.setattr(ds, "PRICES_PATH", real)
    ds._load_prices.cache_clear()
    sat = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    fri_peak = datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc)
    assert ds._rates("deepseek-flash", sat) == {"input_fresh": 0.15, "input_cache_hit": 0.003, "output": 0.60}
    assert ds._rates("deepseek-flash", fri_peak) == {"input_fresh": 0.30, "input_cache_hit": 0.006, "output": 1.20}
    assert ds._rates("deepseek-v4-pro", sat) == {"input_fresh": 0.66, "input_cache_hit": 0.022, "output": 1.98}
    assert ds._rates("deepseek-v4-pro", fri_peak) == {"input_fresh": 1.32, "input_cache_hit": 0.044, "output": 3.96}
    assert ds._rates("deepseek-chat", fri_peak) == ds._rates("deepseek-flash", fri_peak)
    assert ds._rates("deepseek-reasoner", fri_peak) is None
    ds._load_prices.cache_clear()
