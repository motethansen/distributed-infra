"""Handler for `market_brief` tasks (#2) — watchlist quotes + simple signals.

Public market data via yfinance (free, no key) — NOT privacy-class. Computes a few
well-understood signals per ticker: 1d %, RSI(14), 50/200-day MA cross, opening gap.
Alerts-only — no order routing (that's a gated v2 with #13's risk gates).

payload:
  tickers: list[str]  — optional override; default config/watchlist.yaml.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from shared.models import Task

_WATCHLIST = Path(__file__).parent.parent.parent / "config" / "watchlist.yaml"
_DEFAULT = ["AAPL", "MSFT", "SPY"]


def _load_watchlist() -> list[str]:
    if _WATCHLIST.exists():
        d = yaml.safe_load(open(_WATCHLIST, encoding="utf-8")) or {}
        return [str(t).strip() for t in (d.get("tickers") or []) if str(t).strip()] or _DEFAULT
    return _DEFAULT


def _analyze_all(tickers: list[str]) -> list[dict]:
    """Blocking yfinance work; run via asyncio.to_thread. Sequential to avoid rate limits."""
    import yfinance as yf

    out: list[dict] = []
    for sym in tickers:
        try:
            hist = yf.Ticker(sym).history(period="1y", auto_adjust=True)
            if hist is None or hist.empty or len(hist) < 2:
                out.append({"symbol": sym, "error": "no data"})
                continue
            close = hist["Close"]
            openp = hist["Open"]
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            chg = ((last - prev) / prev * 100) if prev else 0.0

            # RSI(14), simple rolling average of gains/losses
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
            loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
            if loss and loss > 0:
                rsi = 100 - 100 / (1 + gain / loss)
            else:
                rsi = 100.0 if gain else 50.0

            ma50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
            ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
            gap = ((float(openp.iloc[-1]) - prev) / prev * 100) if prev else 0.0

            signals = []
            if rsi >= 70:
                signals.append(f"RSI {rsi:.0f} overbought")
            elif rsi <= 30:
                signals.append(f"RSI {rsi:.0f} oversold")
            if ma50 and ma200:
                signals.append("golden✛" if ma50 > ma200 else "death✚")
            if abs(gap) >= 2:
                signals.append(f"gap {gap:+.1f}%")

            out.append({"symbol": sym, "last": last, "chg": chg, "rsi": rsi, "signals": signals})
        except Exception as exc:  # one bad ticker mustn't sink the brief
            out.append({"symbol": sym, "error": str(exc)[:60]})
    return out


def _format(rows: list[dict]) -> str:
    lines = [f"📈 Market brief ({len(rows)})"]
    for r in rows:
        if r.get("error"):
            lines.append(f"• {r['symbol']}: {r['error']}")
            continue
        arrow = "🔺" if r["chg"] > 0 else ("🔻" if r["chg"] < 0 else "▪")
        sig = ("  · " + " · ".join(r["signals"])) if r.get("signals") else ""
        lines.append(f"{arrow} {r['symbol']}  {r['last']:.2f}  {r['chg']:+.1f}%{sig}")
    return "\n".join(lines)


async def handle_market_brief(task: Task) -> dict:
    tickers = task.payload.get("tickers") or _load_watchlist()
    try:
        rows = await asyncio.to_thread(_analyze_all, tickers)
    except Exception as exc:
        return {"error": f"market brief failed: {exc}"}
    return {"response": _format(rows), "data": rows}
