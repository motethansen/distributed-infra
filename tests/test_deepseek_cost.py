"""DeepSeek cost_usd: computed from config/llm_prices.yaml, summed across the first call and the retry."""
import asyncio

import pytest

from agents import deepseek_agent as ds

PRICES = """deepseek:
  deepseek-chat: {input_fresh: 1.0, input_cache_hit: 0.1, output: 2.0}
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
    assert out["usage"] == {"tokens_in": 10, "tokens_out": 5, "cost_usd": None}


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
    assert {"deepseek-chat", "deepseek-reasoner"} <= set(table)
