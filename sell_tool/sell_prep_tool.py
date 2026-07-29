#!/usr/bin/env python3
"""MTG Sell Prep Tool — what should I sell to Card Kingdom this week?

Pipeline: ManaBox CSV -> Scryfall enrichment -> Card Kingdom buylist match
-> price-history trend scoring -> report grouped by binder, sorted by
estimated CK payout (credit by default) descending.

Examples:
    python3 sell_prep_tool.py --csv ~/Downloads/manabox_export.csv
    python3 sell_prep_tool.py --csv export.csv --mode liquidity --format json
    python3 sell_prep_tool.py --csv export.csv --bootstrap   # seed history

No LLM/AI calls anywhere; only public endpoints (Scryfall, Card Kingdom,
MTGJSON).
"""

import argparse
import csv as csv_module
import json
import os
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone, date

import database
import scryfall_fetcher
import cardkingdom_fetcher
import scoring_engine
from manabox_parser import parse_manabox_csv

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "sell_prep.db")

BOOTSTRAP_COVERAGE_THRESHOLD = 0.5   # bootstrap when <50% of cards have history

CSV_COLUMNS = [
    "binder_name", "binder_type", "name", "set_code", "set_name",
    "collector_number", "foil", "condition", "language", "quantity", "added",
    "purchase_price", "scryfall_price",
    "ck_cash", "ck_credit", "ck_buying", "ck_qty_wanted", "ck_match_quality",
    "ck_edition", "est_payout_cash", "est_payout_credit",
    "gain_scryfall", "gain_ck",
    "momentum_30d_pct", "peak_pct", "liquidity_score",
    "excess_copies", "excess_flag", "excess_note",
    "sell_score", "reasons",
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", required=True, help="ManaBox export CSV path")
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    p.add_argument("--mode", choices=["standard", "peak", "liquidity"],
                   default="standard",
                   help="standard | peak (Peak Detection) | liquidity (Liquidity Focus)")
    p.add_argument("--format", choices=["csv", "json", "both"], default="csv")
    p.add_argument("--output", default=None,
                   help="Output path without extension (default: "
                        "sell_report_<mode>_<date> next to this script)")
    p.add_argument("--payout", choices=["credit", "cash"], default="credit",
                   help="Which CK payout to sort by (default: credit)")
    p.add_argument("--min-payout", type=float, default=0.0,
                   help="Hide rows whose estimated payout is below this")
    p.add_argument("--bootstrap", action="store_true",
                   help="Force the MTGJSON 90-day history bootstrap")
    p.add_argument("--no-bootstrap", action="store_true",
                   help="Never bootstrap, even if history is thin")
    p.add_argument("--refresh", action="store_true",
                   help="Ignore caches; refetch Scryfall and CK data")
    p.add_argument("--top", type=int, default=15,
                   help="How many rows to print to the console (default 15)")
    return p.parse_args(argv)


def build_report(conn, args, scryfall_fetch=None, ck_fetch=None,
                 bootstrap_fn=None, today=None):
    """Runs the whole pipeline; fetchers injectable for offline tests."""
    scryfall_fetch = scryfall_fetch or scryfall_fetcher.fetch_cards
    ck_fetch = ck_fetch or cardkingdom_fetcher.fetch_pricelist

    # 1. Inventory from CSV (source of truth, reloaded every run)
    items = parse_manabox_csv(args.csv)
    database.replace_inventory(conn, items)
    print(f"[inventory] {len(items)} rows loaded from {args.csv}")
    rows = database.load_inventory(conn)

    # 2. Scryfall enrichment
    ids = [r["scryfall_id"] for r in rows if r["scryfall_id"]]
    cards = scryfall_fetch(conn, ids, force=args.refresh)
    print(f"[scryfall] {len(cards)} of {len(set(ids))} cards resolved")

    # 3. MTGJSON bootstrap when history is thin (or forced)
    if not args.no_bootstrap:
        coverage = database.history_coverage(conn, ids)
        if args.bootstrap or coverage < BOOTSTRAP_COVERAGE_THRESHOLD:
            if bootstrap_fn is None:
                import mtgjson_bootstrap
                bootstrap_fn = mtgjson_bootstrap.bootstrap
            print(f"[history] coverage {coverage:.0%} -> running MTGJSON bootstrap")
            bootstrap_fn(conn, ids)

    # 4. Card Kingdom pricelist + matching
    ck_rows = ck_fetch(conn, force=args.refresh)
    index = cardkingdom_fetcher.CKIndex(ck_rows)

    # 5. Record today's CK observation for owned cards (same logic the
    #    daily cron uses; INSERT OR IGNORE makes this idempotent).
    snapshot_owned(conn, rows, cards, index, today=today)

    # 6. Scoring
    total_by_name = defaultdict(int)
    for r in rows:
        total_by_name[r["name"].lower()] += r["quantity"]

    scored = []
    for r in rows:
        card = cards.get(r["scryfall_id"])
        set_name = card.get("set_name") if card else r["set_name"]
        ck_row, quality = index.match(r["name"], set_name or r["set_name"], r["foil"])
        sc = scoring_engine.score_item(
            conn, r, card, ck_row, quality,
            scryfall_fetcher.market_price(card, r["foil"]) if card else None,
            total_by_name[r["name"].lower()], today=today)
        scored.append(sc)

    # 7. Mode filter + payout floor + grouping/sort
    scored = scoring_engine.filter_for_mode(scored, args.mode)
    payout_key = ("est_payout_credit" if args.payout == "credit"
                  else "est_payout_cash")
    if args.min_payout > 0:
        scored = [s for s in scored if getattr(s, payout_key) >= args.min_payout]
    scored.sort(key=lambda s: (s.binder_name.lower(),
                               -getattr(s, payout_key), -s.sell_score))
    return scored, payout_key


def snapshot_owned(conn, inventory_rows, cards, ck_index, today=None) -> int:
    """Append today's CK buylist+retail observation for every owned card."""
    day = (today or date.today()).isoformat()
    inserted = 0
    seen = set()
    for r in inventory_rows:
        sid, finish = r["scryfall_id"], r["foil"]
        if not sid or (sid, finish) in seen:
            continue
        seen.add((sid, finish))
        card = cards.get(sid)
        set_name = card.get("set_name") if card else r["set_name"]
        ck_row, _ = ck_index.match(r["name"], set_name or r["set_name"], finish)
        if not ck_row:
            continue
        if ck_row["price_buy"] > 0:
            inserted += database.add_history_row(
                conn, "ck", sid, finish, "buylist", day, ck_row["price_buy"])
        if ck_row["price_retail"] > 0:
            inserted += database.add_history_row(
                conn, "ck", sid, finish, "retail", day, ck_row["price_retail"])
    conn.commit()
    if inserted:
        print(f"[snapshot] recorded {inserted} price points for {day}")
    return inserted


# ---------- output ----------

def to_row_dict(s) -> dict:
    d = asdict(s)
    d["reasons"] = "; ".join(s.reasons)
    d["ck_buying"] = bool(s.ck_buying)
    d["excess_flag"] = bool(s.excess_flag)
    return d


def write_csv(scored, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv_module.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for s in scored:
            writer.writerow(to_row_dict(s))


def write_json(scored, path, mode, payout_key):
    binders = defaultdict(list)
    for s in scored:
        binders[s.binder_name].append(to_row_dict(s))
    doc = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": mode,
        "sorted_by": payout_key,
        "binders": [
            {
                "binder_name": name,
                "subtotal_payout_cash": round(sum(r["est_payout_cash"] for r in rows), 2),
                "subtotal_payout_credit": round(sum(r["est_payout_credit"] for r in rows), 2),
                "items": rows,
            }
            for name, rows in sorted(binders.items(), key=lambda kv: kv[0].lower())
        ],
        "total_payout_cash": round(sum(s.est_payout_cash for s in scored), 2),
        "total_payout_credit": round(sum(s.est_payout_credit for s in scored), 2),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)


def print_summary(scored, payout_key, top):
    total_cash = sum(s.est_payout_cash for s in scored)
    total_credit = sum(s.est_payout_credit for s in scored)
    print(f"\n{len(scored)} rows | est. CK payout: "
          f"${total_cash:,.2f} cash / ${total_credit:,.2f} credit\n")
    ranked = sorted(scored, key=lambda s: -getattr(s, payout_key))[:top]
    for s in ranked:
        payout = getattr(s, payout_key)
        if payout <= 0:
            continue
        flags = " [EXCESS]" if s.excess_flag else ""
        print(f"  ${payout:8,.2f}  {s.name} ({s.set_code.upper()} "
              f"{'foil' if s.foil != 'normal' else 'nonfoil'}) x{s.quantity} "
              f"— {s.binder_name}{flags}  score {s.sell_score}")


def main(argv=None):
    args = parse_args(argv)
    if not os.path.exists(args.csv):
        sys.exit(f"CSV not found: {args.csv}")
    conn = database.connect(args.db)
    try:
        scored, payout_key = build_report(conn, args)
    finally:
        conn.commit()

    stamp = date.today().isoformat()
    base = args.output or os.path.join(HERE, f"sell_report_{args.mode}_{stamp}")
    if args.format in ("csv", "both"):
        write_csv(scored, base + ".csv")
        print(f"[output] wrote {base}.csv")
    if args.format in ("json", "both"):
        write_json(scored, base + ".json", args.mode, payout_key)
        print(f"[output] wrote {base}.json")
    print_summary(scored, payout_key, args.top)
    conn.close()


if __name__ == "__main__":
    main()
