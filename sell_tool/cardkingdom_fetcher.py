"""Card Kingdom buylist integration.

Public JSON pricelist, no auth:
    https://api.cardkingdom.com/api/v2/pricelist

CK does not key by Scryfall ID, so matching is name + edition + foil.
Edition names rarely match Scryfall's verbatim (the classic MTG
edition-alias problem), so we normalize both sides, apply an explicit
alias table for known offenders, and fall back to fuzzy similarity.
An item that can't be matched confidently gets ck fields left empty and
match_quality 'none' — we never guess across editions, because a wrong
edition means a wrong price.

CK's API exposes only the cash buylist price. Credit is CK's standard
trade-in bonus on top of cash (30% at the time of writing) — computed
here via CREDIT_MULTIPLIER, adjustable in one place.
"""

import json
import re
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

import database

PRICELIST_URL = "https://api.cardkingdom.com/api/v2/pricelist"
USER_AGENT = "MTGSellPrepTool/1.0 (personal collection tool)"
CACHE_MAX_AGE_HOURS = 12
CREDIT_MULTIPLIER = 1.30      # CK trade-in credit = cash * 1.30
FUZZY_EDITION_THRESHOLD = 0.85

# Scryfall set_name (normalized) -> CK edition (normalized).
# Extend this table whenever the unmatched report shows a recurring miss.
EDITION_ALIASES = {
    "limited edition alpha": "alpha",
    "limited edition beta": "beta",
    "unlimited edition": "unlimited",
    "revised edition": "3rd edition",
    "fourth edition": "4th edition",
    "fifth edition": "5th edition",
    "sixth edition": "6th edition",
    "classic sixth edition": "6th edition",
    "seventh edition": "7th edition",
    "eighth edition": "8th edition",
    "ninth edition": "9th edition",
    "tenth edition": "10th edition",
    "secret lair drop": "secret lair",
    "secret lair drop series": "secret lair",
}

# A prefix match only counts when the leftover suffix is this short —
# otherwise "Commander Legends" would swallow "Commander Legends: Battle
# for Baldur's Gate", which is a different set with different prices.
PREFIX_SUFFIX_MAX = 6


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize(text: str) -> str:
    """Lowercase, unify punctuation/whitespace so CK and Scryfall names of
    the same thing collide."""
    text = (text or "").lower()
    text = text.replace("&", "and").replace("’", "'")
    text = re.sub(r"[:,.\-–—/()\[\]']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _download_pricelist() -> List[dict]:
    req = urllib.request.Request(
        PRICELIST_URL,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("data", payload if isinstance(payload, list) else [])


def _clean_rows(raw_rows: List[dict]) -> List[dict]:
    rows = []
    for r in raw_rows:
        rows.append({
            "ck_id": str(r.get("id", "")),
            "name": r.get("name", "") or "",
            "variation": r.get("variation", "") or "",
            "edition": r.get("edition", "") or "",
            "is_foil": _to_bool(r.get("is_foil", False)),
            "price_retail": _to_float(r.get("price_retail")),
            "qty_retail": _to_int(r.get("qty_retail")),
            "price_buy": _to_float(r.get("price_buy")),
            "qty_buying": _to_int(r.get("qty_buying")),
        })
    return rows


def fetch_pricelist(conn, force: bool = False,
                    max_age_hours: float = CACHE_MAX_AGE_HOURS,
                    _download=_download_pricelist) -> List[dict]:
    """Return the CK pricelist, refreshing from the API when the stored
    copy is older than max_age_hours. `_download` is injectable for tests."""
    fetched_at = database.get_meta(conn, "ck_fetched_at")
    if fetched_at and not force:
        try:
            then = datetime.strptime(fetched_at, "%Y-%m-%dT%H:%M:%SZ")
            age_h = (datetime.now(timezone.utc).replace(tzinfo=None) - then
                     ).total_seconds() / 3600
            if age_h < max_age_hours:
                return [dict(r) for r in database.load_ck_pricelist(conn)]
        except ValueError:
            pass

    rows = _clean_rows(_download())
    database.replace_ck_pricelist(conn, rows, _now_iso())
    print(f"[cardkingdom] pricelist refreshed: {len(rows)} rows")
    return [dict(r) for r in database.load_ck_pricelist(conn)]


class CKIndex:
    """Pricelist indexed by normalized card name for fast lookup."""

    def __init__(self, rows: List[dict]):
        self.by_name: Dict[str, List[dict]] = {}
        for r in rows:
            self.by_name.setdefault(normalize(r["name"]), []).append(r)

    def _name_candidates(self, scryfall_name: str) -> List[dict]:
        # CK usually lists only the front face of double-faced/split cards.
        for candidate in (scryfall_name,
                          scryfall_name.split("//")[0].strip()):
            rows = self.by_name.get(normalize(candidate))
            if rows:
                return rows
        return []

    def match(self, name: str, scryfall_set_name: str,
              finish: str) -> Tuple[Optional[dict], str]:
        """Return (ck_row, match_quality). Quality is one of
        'exact' | 'alias' | 'fuzzy' | 'none'."""
        want_foil = finish in ("foil", "etched")
        candidates = [r for r in self._name_candidates(name)
                      if bool(r["is_foil"]) == want_foil]
        if not candidates:
            return None, "none"

        want_edition = normalize(scryfall_set_name)

        exact = [r for r in candidates if normalize(r["edition"]) == want_edition]
        if exact:
            return self._pick(exact), "exact"

        alias = EDITION_ALIASES.get(want_edition)
        if alias:
            hits = [r for r in candidates if normalize(r["edition"]) == alias]
            if hits:
                return self._pick(hits), "alias"

        best, best_score = None, 0.0
        for r in candidates:
            ck_edition = normalize(r["edition"])
            score = SequenceMatcher(None, want_edition, ck_edition).ratio()
            longer, shorter = ((want_edition, ck_edition)
                               if len(want_edition) >= len(ck_edition)
                               else (ck_edition, want_edition))
            if (longer.startswith(shorter)
                    and len(longer) - len(shorter) <= PREFIX_SUFFIX_MAX):
                score = max(score, FUZZY_EDITION_THRESHOLD)
            if score > best_score:
                best, best_score = r, score
        if best is not None and best_score >= FUZZY_EDITION_THRESHOLD:
            return best, "fuzzy"
        return None, "none"

    @staticmethod
    def _pick(rows: List[dict]) -> dict:
        """Same name+edition+foil can still have variations (borderless,
        showcase...). Without collector-number data on CK's side we take
        the plain (no-variation) row when present, else the highest buy
        price is the optimistic-but-visible choice."""
        plain = [r for r in rows if not (r["variation"] or "").strip()]
        pool = plain or rows
        return max(pool, key=lambda r: r["price_buy"])


def credit_price(cash: float) -> float:
    return round(cash * CREDIT_MULTIPLIER, 2)
