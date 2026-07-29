#!/usr/bin/env python3
"""Daily Card Kingdom price snapshot — meant to run from cron.

Fetches the current CK pricelist and appends one timestamped observation
per owned card to price_history (never overwrites). Idempotent: if
today's snapshot already ran, it exits immediately, so a double-fire is
harmless.

Inventory comes from the last `sell_prep_tool.py` run's database — run
the main tool at least once before scheduling this.

crontab example (daily at 9:30am):
    30 9 * * * cd /path/to/commander-finder/sell_tool && /usr/bin/python3 snapshot_prices.py >> snapshot.log 2>&1
"""

import argparse
import sys
from datetime import date

import database
import scryfall_fetcher
import cardkingdom_fetcher
from sell_prep_tool import DEFAULT_DB, snapshot_owned


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    p.add_argument("--force", action="store_true",
                   help="Re-run even if today's snapshot already exists")
    args = p.parse_args(argv)

    conn = database.connect(args.db)
    today = date.today().isoformat()

    if database.get_meta(conn, "last_snapshot_date") == today and not args.force:
        print(f"[snapshot] already ran for {today}, nothing to do")
        return 0

    rows = database.load_inventory(conn)
    if not rows:
        print("[snapshot] no inventory in the database yet — "
              "run sell_prep_tool.py --csv <export> first", file=sys.stderr)
        return 1

    # Cached Scryfall data is enough here (set names only); no API calls
    # unless the cache is empty for a card.
    ids = [r["scryfall_id"] for r in rows if r["scryfall_id"]]
    cards = database.get_cached_cards(conn, ids)

    ck_rows = cardkingdom_fetcher.fetch_pricelist(conn, force=True)
    index = cardkingdom_fetcher.CKIndex(ck_rows)

    inserted = snapshot_owned(conn, rows, cards, index)
    database.set_meta(conn, "last_snapshot_date", today)
    print(f"[snapshot] {today}: {inserted} new price points")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
