import logging
import os
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger("stonk_advisor.market")

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
REQUEST_TIMEOUT = 5

if not FINNHUB_API_KEY:
    logger.warning(
        "FINNHUB_API_KEY not set; market data endpoints will return empty "
        "results until it's configured. Get a free key at finnhub.io."
    )


@dataclass
class Quote:
    symbol: str; name: str; price: float; change: float
    change_percent: float; volume: int; high: float; low: float
    open: float; previous_close: float


def _get(path: str, params: dict) -> Optional[dict]:
    if not FINNHUB_API_KEY:
        return None
    try:
        res = requests.get(
            f"{FINNHUB_BASE_URL}{path}",
            params={**params, "token": FINNHUB_API_KEY},
            timeout=REQUEST_TIMEOUT,
        )
        res.raise_for_status()
        return res.json()
    except requests.RequestException:
        logger.exception("Finnhub request failed: %s %s", path, params)
        return None


class Market:
    @staticmethod
    def get_quote(symbol: str) -> Optional[Quote]:
        data = _get("/quote", {"symbol": symbol})
        # Finnhub returns all-zero fields for an unknown/invalid symbol
        # instead of an error status.
        if not data or data.get("c") in (None, 0):
            return None

        profile = _get("/stock/profile2", {"symbol": symbol}) or {}
        previous_close = data["pc"]
        current = data["c"]

        return Quote(
            symbol=symbol.upper(),
            name=profile.get("name") or symbol,
            price=float(current),
            change=float(data.get("d") or 0),
            change_percent=float(data.get("dp") or 0),
            # Finnhub's basic /quote endpoint doesn't include volume; a
            # real volume figure would need a separate /stock/candle call.
            volume=0,
            high=float(data.get("h") or current),
            low=float(data.get("l") or current),
            open=float(data.get("o") or current),
            previous_close=float(previous_close),
        )

    @staticmethod
    def get_quotes(symbols: list) -> dict:
        return {s: q for s in symbols if (q := Market.get_quote(s))}

    @staticmethod
    def search(query: str) -> list:
        data = _get("/search", {"q": query})
        if not data:
            return []
        return [
            {"symbol": r["symbol"], "name": r.get("description", r["symbol"])}
            for r in data.get("result", [])[:10]
        ]
