"""The world outside the charts: scheduled US economic news and how much of the crypto market
BTC makes up. Both come from free public feeds, fetched at most hourly and cached, and the bot
carries on without them if a feed is down."""

import time
from datetime import datetime

import httpx

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
REFRESH = 3600
RETRY = 900


class Macro:
    def __init__(self, client=None, clock=time.time):
        self.client = client or httpx.Client(timeout=15)
        self.clock = clock
        self._cache: dict[str, tuple[float, object]] = {}

    def _get(self, key: str, url: str):
        now = self.clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] < (REFRESH if hit[1] is not None else RETRY):
            return hit[1]
        try:
            r = self.client.get(url)
            r.raise_for_status()
            data = r.json()
        except Exception:
            data = hit[1] if hit else None  # keep the last good copy
            self._cache[key] = (now - REFRESH + RETRY, data) if data is not None else (now, None)
            return data
        self._cache[key] = (now, data)
        return data

    def events(self, impact=("High", "Medium")) -> list[dict]:
        """This week's US economic releases: [{"ts", "title", "impact", "forecast", "previous"}]."""
        rows = self._get("calendar", CALENDAR_URL) or []
        out = []
        for e in rows:
            if e.get("country") != "USD" or e.get("impact") not in impact:
                continue
            try:
                ts = int(datetime.fromisoformat(e["date"]).timestamp())
            except (KeyError, ValueError):
                continue
            out.append({"ts": ts, "title": e.get("title", "?"), "impact": e["impact"],
                        "forecast": e.get("forecast") or "", "previous": e.get("previous") or ""})
        return sorted(out, key=lambda e: e["ts"])

    def btc_dominance(self) -> tuple[float, float] | None:
        """(BTC's share of the whole crypto market in %, total market cap change over 24h in %)."""
        data = (self._get("global", GLOBAL_URL) or {}).get("data") or {}
        dom = (data.get("market_cap_percentage") or {}).get("btc")
        if dom is None:
            return None
        return float(dom), float(data.get("market_cap_change_percentage_24h_usd") or 0.0)


def upcoming_text(events: list[dict], now: int, ahead_hours: float = 48, behind_hours: float = 6) -> str | None:
    rows = [e for e in events if now - behind_hours * 3600 <= e["ts"] <= now + ahead_hours * 3600]
    if not rows:
        return None
    lines = []
    for e in rows:
        mins = (e["ts"] - now) / 60
        when = (f"in {mins / 60:.1f}h" if mins >= 90 else f"in {mins:.0f} min") if mins >= 0 else f"{-mins / 60:.1f}h ago"
        extra = ", ".join(x for x in (e["forecast"] and f"forecast {e['forecast']}", e["previous"] and f"previous {e['previous']}") if x)
        lines.append(f"- {e['title']} ({e['impact'].lower()} impact) {when}" + (f", {extra}" if extra else ""))
    return "\n".join(lines)
