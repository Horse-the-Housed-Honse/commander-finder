"""Fetch card data from Scryfall, with SQLite caching.

Uses the documented POST /cards/collection endpoint (up to 75 identifiers
per request) and joins on Scryfall ID — the reliable key carried by the
ManaBox export. Cards whose ID Scryfall doesn't recognize are reported,
not fatal.

Scryfall etiquette: identify ourselves with a User-Agent and sleep 100 ms
between requests (https://scryfall.com/docs/api).
"""

import json
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List

import database

API_COLLECTION = "https://api.scryfall.com/cards/collection"
USER_AGENT = "MTGSellPrepTool/1.0 (personal collection tool)"
BATCH_SIZE = 75
REQUEST_DELAY_S = 0.12
CACHE_TTL_HOURS = 24


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cache_is_fresh(fetched_at: str, ttl_hours: float) -> bool:
    try:
        then = datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    age = datetime.now(timezone.utc).replace(tzinfo=None) - then
    return age.total_seconds() < ttl_hours * 3600


def _post_collection(identifiers: List[dict]) -> dict:
    body = json.dumps({"identifiers": identifiers}).encode("utf-8")
    req = urllib.request.Request(
        API_COLLECTION,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_cards(conn, scryfall_ids: List[str], force: bool = False,
                ttl_hours: float = CACHE_TTL_HOURS,
                _post=_post_collection) -> Dict[str, dict]:
    """Return {scryfall_id: card_json} for every ID we can resolve.

    Serves from the SQLite cache when fresh; batches the rest through
    /cards/collection. `_post` is injectable for offline tests.
    """
    wanted = sorted({s for s in scryfall_ids if s})
    cards: Dict[str, dict] = {}

    cached = database.get_cached_cards(conn, wanted)
    to_fetch = []
    for sid in wanted:
        hit = cached.get(sid)
        if hit and not force and _cache_is_fresh(hit.get("_fetched_at", ""), ttl_hours):
            cards[sid] = hit
        else:
            to_fetch.append(sid)

    not_found: List[str] = []
    fetched_at = _now_iso()
    for i in range(0, len(to_fetch), BATCH_SIZE):
        batch = to_fetch[i:i + BATCH_SIZE]
        payload = _post([{"id": sid} for sid in batch])
        for card in payload.get("data", []):
            sid = card.get("id", "").lower()
            cards[sid] = card
            database.cache_card(conn, sid, card, fetched_at)
        for miss in payload.get("not_found", []):
            not_found.append(miss.get("id", "?"))
        conn.commit()
        if i + BATCH_SIZE < len(to_fetch):
            time.sleep(REQUEST_DELAY_S)

    if not_found:
        print(f"[scryfall] {len(not_found)} ID(s) not found: "
              + ", ".join(not_found[:5])
              + (" ..." if len(not_found) > 5 else ""))
    return cards


def market_price(card: dict, finish: str) -> float | None:
    """Scryfall market price in USD for the finish this inventory row has."""
    prices = card.get("prices", {}) or {}
    key = {"foil": "usd_foil", "etched": "usd_etched"}.get(finish, "usd")
    raw = prices.get(key)
    # Etched/foil printings sometimes only carry one price field — fall back.
    if raw is None:
        for alt in ("usd", "usd_foil", "usd_etched"):
            if prices.get(alt) is not None:
                raw = prices[alt]
                break
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
