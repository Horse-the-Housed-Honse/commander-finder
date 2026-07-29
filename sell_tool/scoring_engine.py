"""Scoring engine: turns one inventory row + market data into a scored,
explainable record.

Factors
-------
momentum   30-day % change of the card's market price series
peak       how close today's price is to its 90-day high (peak detection)
liquidity  is CK actually buying? how strong is their buy/retail spread?
           how popular is the card (EDHREC rank)?
gain       Scryfall market price vs your purchase price (when known)
ck_gain    CK buylist CASH vs your purchase price — the realizable gain,
           since CK is the actual sell destination
excess     copies owned beyond a playset (4), or beyond 1 for
           singleton-oriented cards (legendary creatures / "can be your
           commander" pieces) — sell-eligible regardless of trend

Composite sell_score is 0-100. Cards CK is not currently buying
(qty_buying == 0) are hard-deprioritized: their payout is treated as $0
and their score is capped, even if a stale buy price exists.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

import database
from cardkingdom_fetcher import credit_price

# --- tunables ------------------------------------------------------------

PLAYSET_THRESHOLD = 4        # copies you'd keep of a normal card
SINGLETON_THRESHOLD = 1      # copies you'd keep of a commander-ish card

# ManaBox condition -> fraction of NM buylist price CK realistically pays.
# These are conservative estimates of CK's grading discounts.
CONDITION_MULTIPLIER = {
    "mint": 1.0, "near_mint": 1.0, "nm": 1.0,
    "excellent": 0.80, "ex": 0.80, "light_played": 0.80, "lightly_played": 0.80, "lp": 0.80,
    "good": 0.60, "vg": 0.60, "moderately_played": 0.60, "mp": 0.60, "played": 0.60,
    "poor": 0.40, "heavily_played": 0.40, "hp": 0.40, "damaged": 0.40, "dmg": 0.40,
}

WEIGHTS = {"momentum": 0.25, "peak": 0.20, "liquidity": 0.25,
           "gain": 0.15, "excess": 0.15}

NOT_BUYING_SCORE_CAP = 25.0   # max composite when CK qty_wanted == 0

# History sources in priority order when the same date appears twice:
# our own snapshots beat the MTGJSON backfill.
SOURCE_PRIORITY = {"ck": 0, "mtgjson:cardkingdom": 1, "mtgjson:tcgplayer": 2}


@dataclass
class ScoredItem:
    # identity / location
    binder_name: str
    binder_type: str
    name: str
    set_code: str
    set_name: str
    collector_number: str
    foil: str
    condition: str
    language: str
    quantity: int
    added: str
    scryfall_id: str
    # prices
    purchase_price: Optional[float]
    scryfall_price: Optional[float]
    ck_cash: Optional[float]
    ck_credit: Optional[float]
    ck_buying: bool
    ck_qty_wanted: int
    ck_match_quality: str
    ck_edition: str
    # derived money (per whole row: unit price * condition mult * quantity)
    est_payout_cash: float
    est_payout_credit: float
    gain_scryfall: Optional[float]     # per copy
    gain_ck: Optional[float]           # per copy, cash basis
    # scores
    momentum_30d_pct: Optional[float]
    peak_pct: Optional[float]          # 100 = at 90-day high
    liquidity_score: float
    excess_copies: int
    excess_flag: bool
    excess_note: str
    sell_score: float
    reasons: List[str] = field(default_factory=list)


# --- history helpers -----------------------------------------------------


def _merged_series(conn, scryfall_id: str, finish: str, kind: str,
                   days: int, today: Optional[date] = None) -> Dict[str, float]:
    """date -> price for the last `days` days, best source winning per date."""
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    best: Dict[str, tuple] = {}
    for row in database.get_history(conn, scryfall_id, finish, kind, since=since):
        prio = SOURCE_PRIORITY.get(row["source"], 9)
        cur = best.get(row["date"])
        if cur is None or prio < cur[0]:
            best[row["date"]] = (prio, row["price"])
    return {d: p for d, (_, p) in sorted(best.items())}


def price_series(conn, scryfall_id: str, finish: str,
                 days: int = 90, today: Optional[date] = None) -> Dict[str, float]:
    """Market-price series for trend math: retail preferred (that's the
    market signal), buylist as fallback when no retail history exists."""
    series = _merged_series(conn, scryfall_id, finish, "retail", days, today)
    if len(series) >= 2:
        return series
    return _merged_series(conn, scryfall_id, finish, "buylist", days, today)


def pct_change(series: Dict[str, float], days: int,
               today: Optional[date] = None) -> Optional[float]:
    if len(series) < 2:
        return None
    today = today or date.today()
    cutoff = (today - timedelta(days=days)).isoformat()
    dates = sorted(series)
    baseline_dates = [d for d in dates if d <= cutoff] or dates[:1]
    baseline = series[baseline_dates[-1]]
    current = series[dates[-1]]
    if baseline <= 0:
        return None
    return (current - baseline) / baseline * 100.0


def peak_ratio(series: Dict[str, float]) -> Optional[float]:
    """current / 90-day max, as a percentage (100 = sitting at the high)."""
    if not series:
        return None
    dates = sorted(series)
    high = max(series.values())
    if high <= 0:
        return None
    return series[dates[-1]] / high * 100.0


# --- factor scores (each 0-100) -------------------------------------------


def momentum_score(pct: Optional[float]) -> float:
    """-20% -> 0, flat -> 50, +30% or more -> 100."""
    if pct is None:
        return 50.0
    if pct <= -20:
        return 0.0
    if pct >= 30:
        return 100.0
    if pct < 0:
        return 50.0 + pct * 2.5
    return 50.0 + pct * (50.0 / 30.0)


def liquidity_score_fn(ck_buying: bool, qty_wanted: int,
                       price_buy: Optional[float], price_retail: Optional[float],
                       edhrec_rank: Optional[int]) -> float:
    if not ck_buying:
        return 0.0
    score = 40.0
    score += min(qty_wanted, 8) / 8 * 15.0     # deeper want-list = safer sale
    if price_buy and price_retail and price_retail > 0:
        spread = price_buy / price_retail       # CK buying at 60%+ of retail = hot
        score += min(spread / 0.60, 1.0) * 30.0
    if edhrec_rank is not None:
        score += max(0.0, 1.0 - min(edhrec_rank, 20000) / 20000) * 15.0
    return min(score, 100.0)


def gain_score(gain: Optional[float], purchase: Optional[float]) -> float:
    """Gain over cost basis. Blank purchase price = neutral 50 — the spec
    forbids treating unknown cost as $0."""
    if gain is None or purchase is None:
        return 50.0
    if purchase <= 0:
        return 75.0 if gain > 0 else 50.0
    ratio = gain / purchase
    if ratio <= -0.5:
        return 0.0
    if ratio >= 2.0:
        return 100.0
    return 40.0 + ratio * 30.0


def is_singleton_card(card: Optional[dict]) -> bool:
    """Legendary creatures and 'can be your commander' cards: you rarely
    want more than one, so copy #2+ counts as excess."""
    if not card:
        return False
    type_line = (card.get("type_line") or "").lower()
    oracle = (card.get("oracle_text") or "").lower()
    if "legendary" in type_line and "creature" in type_line:
        return True
    return "can be your commander" in oracle


def is_basic_land(card: Optional[dict]) -> bool:
    return bool(card) and "basic" in (card.get("type_line") or "").lower()


def excess_for_name(total_owned: int, card: Optional[dict]) -> int:
    if is_basic_land(card):
        return 0
    threshold = SINGLETON_THRESHOLD if is_singleton_card(card) else PLAYSET_THRESHOLD
    return max(0, total_owned - threshold)


# --- main entry ------------------------------------------------------------


def score_item(conn, item, card: Optional[dict], ck_row: Optional[dict],
               ck_match_quality: str, scryfall_price: Optional[float],
               total_owned_by_name: int, today: Optional[date] = None) -> ScoredItem:
    finish = item["foil"] if item["foil"] in ("normal", "foil", "etched") else "normal"

    ck_cash = ck_row["price_buy"] if ck_row else None
    ck_credit = credit_price(ck_cash) if ck_cash is not None else None
    ck_qty_wanted = ck_row["qty_buying"] if ck_row else 0
    ck_buying = bool(ck_row) and ck_qty_wanted > 0 and (ck_cash or 0) > 0
    ck_retail = ck_row["price_retail"] if ck_row else None

    cond_mult = CONDITION_MULTIPLIER.get((item["condition"] or "").lower(), 0.80)

    if ck_buying and ck_cash:
        payout_cash = round(ck_cash * cond_mult * item["quantity"], 2)
        payout_credit = round(credit_price(ck_cash) * cond_mult * item["quantity"], 2)
    else:
        payout_cash = payout_credit = 0.0   # not buying -> no realizable payout

    purchase = item["purchase_price"]
    gain_scry = (round(scryfall_price - purchase, 2)
                 if scryfall_price is not None and purchase is not None else None)
    gain_ck = (round(ck_cash - purchase, 2)
               if ck_cash is not None and purchase is not None else None)

    series = price_series(conn, item["scryfall_id"], finish, days=90, today=today)
    mom_pct = pct_change(series, 30, today=today)
    peak_pct = peak_ratio(series)

    edhrec = card.get("edhrec_rank") if card else None
    liq = liquidity_score_fn(ck_buying, ck_qty_wanted, ck_cash, ck_retail, edhrec)

    excess = excess_for_name(total_owned_by_name, card)
    excess_flag = excess > 0
    threshold = (0 if is_basic_land(card)
                 else SINGLETON_THRESHOLD if is_singleton_card(card)
                 else PLAYSET_THRESHOLD)
    excess_note = (f"own {total_owned_by_name}, keep {threshold}, "
                   f"excess {excess}" if excess_flag else "")

    scores = {
        "momentum": momentum_score(mom_pct),
        "peak": peak_pct if peak_pct is not None else 50.0,
        "liquidity": liq,
        "gain": gain_score(gain_ck if gain_ck is not None else gain_scry, purchase),
        "excess": 100.0 if excess_flag else 0.0,
    }
    composite = sum(scores[k] * w for k, w in WEIGHTS.items())
    if not ck_buying:
        composite = min(composite, NOT_BUYING_SCORE_CAP)
    composite = round(composite, 1)

    reasons = []
    if excess_flag:
        reasons.append(excess_note)
    if mom_pct is not None and mom_pct >= 10:
        reasons.append(f"up {mom_pct:.0f}% in 30d")
    if mom_pct is not None and mom_pct <= -10:
        reasons.append(f"down {abs(mom_pct):.0f}% in 30d")
    if peak_pct is not None and peak_pct >= 95:
        reasons.append("at 90-day high")
    if not ck_buying and ck_row:
        reasons.append("CK not currently buying")
    if not ck_row:
        reasons.append("no CK buylist match")
    if gain_ck is not None and gain_ck > 0:
        reasons.append(f"+${gain_ck:.2f}/copy vs cost at CK")

    return ScoredItem(
        binder_name=item["binder_name"], binder_type=item["binder_type"],
        name=item["name"], set_code=item["set_code"], set_name=item["set_name"],
        collector_number=item["collector_number"], foil=item["foil"],
        condition=item["condition"], language=item["language"],
        quantity=item["quantity"], added=item["added"],
        scryfall_id=item["scryfall_id"],
        purchase_price=purchase, scryfall_price=scryfall_price,
        ck_cash=ck_cash, ck_credit=ck_credit, ck_buying=ck_buying,
        ck_qty_wanted=ck_qty_wanted, ck_match_quality=ck_match_quality,
        ck_edition=(ck_row["edition"] if ck_row else ""),
        est_payout_cash=payout_cash, est_payout_credit=payout_credit,
        gain_scryfall=gain_scry, gain_ck=gain_ck,
        momentum_30d_pct=round(mom_pct, 1) if mom_pct is not None else None,
        peak_pct=round(peak_pct, 1) if peak_pct is not None else None,
        liquidity_score=round(liq, 1),
        excess_copies=excess, excess_flag=excess_flag, excess_note=excess_note,
        sell_score=composite, reasons=reasons,
    )


# --- report modes -----------------------------------------------------------


def filter_for_mode(scored: List[ScoredItem], mode: str) -> List[ScoredItem]:
    """'standard' keeps everything. 'peak' = Peak Detection: near the
    90-day high and not falling. 'liquidity' = Liquidity Focus: CK is
    actively buying right now."""
    if mode == "peak":
        return [s for s in scored
                if s.peak_pct is not None and s.peak_pct >= 90
                and (s.momentum_30d_pct is None or s.momentum_30d_pct >= 0)]
    if mode == "liquidity":
        return [s for s in scored if s.ck_buying]
    return list(scored)
