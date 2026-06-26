"""Handler for `weather` tasks (#12).

Today's weather for a location, via Open-Meteo (free, no API key).

payload:
  location: str      — optional place name; geocode it, answer for it, AND persist
                       as the new last-known location.
  set_location: str  — optional place name; geocode + persist + confirm only
                       (no forecast). Used by the `set-location` bridge command.
  (none)             — use the last-known location from config/location.yaml.

Returns {"response": <text for WhatsApp>, "data": {...}} on success, or a
needs_human dict when a place can't be geocoded / the API is unreachable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from shared.models import Task

_LOCATION_FILE = Path(__file__).parent.parent.parent / "config" / "location.yaml"

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes → (emoji, text). Open-Meteo returns these in
# `weather_code`. Grouped to the buckets that matter for a one-line forecast.
_WMO = {
    0: ("☀️", "Clear sky"),
    1: ("🌤", "Mainly clear"), 2: ("⛅", "Partly cloudy"), 3: ("☁️", "Overcast"),
    45: ("🌫", "Fog"), 48: ("🌫", "Depositing rime fog"),
    51: ("🌦", "Light drizzle"), 53: ("🌦", "Drizzle"), 55: ("🌦", "Dense drizzle"),
    56: ("🌧", "Freezing drizzle"), 57: ("🌧", "Dense freezing drizzle"),
    61: ("🌦", "Light rain"), 63: ("🌧", "Rain"), 65: ("🌧", "Heavy rain"),
    66: ("🌧", "Freezing rain"), 67: ("🌧", "Heavy freezing rain"),
    71: ("🌨", "Light snow"), 73: ("🌨", "Snow"), 75: ("❄️", "Heavy snow"),
    77: ("🌨", "Snow grains"),
    80: ("🌦", "Light showers"), 81: ("🌧", "Showers"), 82: ("⛈", "Violent showers"),
    85: ("🌨", "Snow showers"), 86: ("🌨", "Heavy snow showers"),
    95: ("⛈", "Thunderstorm"), 96: ("⛈", "Thunderstorm w/ hail"),
    99: ("⛈", "Thunderstorm w/ heavy hail"),
}


def _describe(code: int) -> tuple[str, str]:
    return _WMO.get(int(code), ("🌡", f"Code {code}"))


def _load_location() -> dict:
    if _LOCATION_FILE.exists():
        with open(_LOCATION_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_location(loc: dict) -> None:
    loc = dict(loc)
    loc["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = _LOCATION_FILE.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(loc, f, allow_unicode=True, sort_keys=False)
    tmp.replace(_LOCATION_FILE)


async def _geocode(place: str) -> dict | None:
    """Resolve a place name to {name, latitude, longitude, timezone, country}."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(_GEOCODE_URL, params={
            "name": place, "count": 1, "language": "en", "format": "json",
        })
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    if not results:
        return None
    g = results[0]
    return {
        "name": g.get("name", place),
        "latitude": g.get("latitude"),
        "longitude": g.get("longitude"),
        "timezone": g.get("timezone", "auto"),
        "country": g.get("country", ""),
    }


async def _forecast(loc: dict) -> dict:
    """Today's forecast + current conditions for a resolved location."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(_FORECAST_URL, params={
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "timezone": loc.get("timezone") or "auto",
            "forecast_days": 1,
            "current": "temperature_2m,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max",
        })
        r.raise_for_status()
        return r.json()


def _format(loc: dict, fc: dict) -> str:
    daily = fc.get("daily", {})
    current = fc.get("current", {})
    units = fc.get("daily_units", {})

    code = (daily.get("weather_code") or [0])[0]
    hi = (daily.get("temperature_2m_max") or [None])[0]
    lo = (daily.get("temperature_2m_min") or [None])[0]
    rain = (daily.get("precipitation_probability_max") or [None])[0]
    now_temp = current.get("temperature_2m")
    deg = units.get("temperature_2m_max", "°C")

    emoji, desc = _describe(code)
    place = loc.get("name", "?")
    country = loc.get("country", "")
    where = f"{place}, {country}" if country and country != place else place

    lines = [f"{emoji} Weather — {where}", f"{desc}"]
    if now_temp is not None:
        lines[1] = f"{desc} · now {now_temp:.0f}{deg}"
    if hi is not None and lo is not None:
        lines.append(f"High {hi:.0f}{deg} / Low {lo:.0f}{deg}")
    if rain is not None:
        lines.append(f"Rain chance {rain:.0f}%")
    return "\n".join(lines)


async def handle_weather(task: Task) -> dict:
    place = (task.payload.get("location") or "").strip()
    set_place = (task.payload.get("set_location") or "").strip()

    # set-location: geocode + persist + confirm, no forecast.
    if set_place:
        try:
            loc = await _geocode(set_place)
        except Exception as exc:
            return {"needs_human": True, "notes": f"Geocoding failed for '{set_place}': {exc}"}
        if not loc:
            return {"needs_human": True,
                    "notes": f"Couldn't find a place called '{set_place}'. Try a city name."}
        _save_location(loc)
        where = f"{loc['name']}, {loc['country']}" if loc.get("country") else loc["name"]
        return {"response": f"📍 Location set to {where}.\nSend `weather` for today's forecast.",
                "data": loc}

    # one-off place: geocode + persist as new last-known, then forecast.
    if place:
        try:
            loc = await _geocode(place)
        except Exception as exc:
            return {"needs_human": True, "notes": f"Geocoding failed for '{place}': {exc}"}
        if not loc:
            return {"needs_human": True,
                    "notes": f"Couldn't find a place called '{place}'. Try a city name."}
        _save_location(loc)
    else:
        # no place given: use last-known from config/location.yaml.
        loc = _load_location()
        if not loc.get("latitude") or not loc.get("longitude"):
            return {"needs_human": True,
                    "notes": "No location set. Send `set-location <place>` first."}

    try:
        fc = await _forecast(loc)
    except Exception as exc:
        return {"needs_human": True, "notes": f"Weather lookup failed: {exc}"}

    return {"response": _format(loc, fc), "data": {"location": loc}}
