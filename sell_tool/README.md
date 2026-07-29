# MTG Sell Prep Tool — Card Kingdom edition

Tells you, on a weekly basis, which cards from your ManaBox collection are
worth selling to Card Kingdom for credit. Standalone Python — no build
step, no AI/LLM calls, only public endpoints (Scryfall, Card Kingdom,
MTGJSON).

This lives in its own `sell_tool/` folder and does not touch the
Commander Partner Finder web app in the repo root.

## What it does

1. Parses your **ManaBox CSV export** (every printing/foil row kept
   separate, blank purchase prices handled — never treated as $0).
2. Enriches each card from **Scryfall** (joined on Scryfall ID, cached in
   SQLite so repeat runs are fast and polite).
3. Pulls the **Card Kingdom buylist** (`api.cardkingdom.com/api/v2/pricelist`)
   and matches your cards on name + edition + foil, with edition-alias and
   fuzzy handling for CK's naming quirks. `qty_buying == 0` means "CK isn't
   buying" — those rows are hard-deprioritized even if a price exists.
4. Seeds **~90 days of price history** from MTGJSON `AllPrices.json.gz` on
   first run, so trend scoring works on day one.
5. Scores every row: momentum (30-day trend), peak detection (near 90-day
   high?), liquidity (is CK buying, how deep, EDHREC popularity), gain
   over cost (both Scryfall-based and CK-cash-based), and **excess
   copies** (more than 4 of a normal card, more than 1 of a legendary
   creature / commander piece).
6. Writes a **CSV/JSON report grouped by binder**, sorted by estimated CK
   payout (credit by default) descending.

## Setup (one time)

```bash
cd sell_tool
pip3 install -r requirements.txt     # ijson (bootstrap) + pytest (tests)
```

## Weekly report

Export your collection from ManaBox (CSV), then:

```bash
python3 sell_prep_tool.py --csv ~/Downloads/manabox_export.csv
```

First run also downloads the MTGJSON history bootstrap (a few hundred MB,
streamed — takes several minutes, happens once). Useful flags:

| Flag | Meaning |
|---|---|
| `--mode standard\|peak\|liquidity` | all cards / Peak Detection / Liquidity Focus |
| `--format csv\|json\|both` | output format (default csv) |
| `--payout credit\|cash` | which CK payout to sort by (default credit) |
| `--min-payout 1.00` | hide rows worth less than this |
| `--bootstrap` | force a fresh MTGJSON history seed |
| `--refresh` | ignore caches, refetch Scryfall + CK |

## Cron (macOS)

Edit your crontab with `crontab -e` and add **both** lines (adjust the
path). Daily snapshot builds price history; weekly report tells you what
to sell:

```cron
30 9 * * *  cd /path/to/commander-finder/sell_tool && /usr/bin/python3 snapshot_prices.py >> snapshot.log 2>&1
0 10 * * 1  cd /path/to/commander-finder/sell_tool && /usr/bin/python3 sell_prep_tool.py --csv "$HOME/Downloads/manabox_export.csv" --format both >> report.log 2>&1
```

Notes:
- The snapshot job is idempotent — if it fires twice in a day it exits
  without duplicating rows.
- macOS may prompt once to give `cron` Full Disk Access
  (System Settings → Privacy & Security) if your CSV lives in Documents/
  Downloads.
- The weekly line re-reads whatever CSV sits at that path — re-export
  from ManaBox when your collection changes.

## Report columns worth knowing

- `ck_cash` / `ck_credit` — CK buylist prices per copy at NM.
  **Credit is computed as cash × 1.30** (CK's standard trade-in bonus;
  the API only publishes cash) — change `CREDIT_MULTIPLIER` in
  `cardkingdom_fetcher.py` if CK changes the rate.
- `est_payout_*` — per-row total: price × condition multiplier × quantity.
  Condition discounts (NM 100%, LP/EX 80%, MP/played 60%, HP/poor 40%)
  are estimates of CK's grading; tune `CONDITION_MULTIPLIER` in
  `scoring_engine.py`.
- `ck_buying` — false when CK's `qty_buying` is 0; payout shows $0
  because you can't actually sell it to them right now.
- `ck_match_quality` — `exact` / `alias` / `fuzzy` / `none`. A `none`
  means the edition couldn't be matched confidently; the tool never
  guesses across editions because that means a wrong price. Recurring
  misses can be fixed by adding a line to `EDITION_ALIASES`.
- `excess_flag` / `excess_note` — you own more copies (summed across all
  printings) than the keep-threshold: 4 for normal cards, 1 for legendary
  creatures / "can be your commander" cards, unlimited for basic lands.
- `sell_score` — 0–100 composite (momentum 25%, peak 20%, liquidity 25%,
  gain 15%, excess 15%), capped at 25 when CK isn't buying.

## Files

| File | Role |
|---|---|
| `sell_prep_tool.py` | CLI entry point — the weekly report |
| `snapshot_prices.py` | daily cron job — appends CK prices to history |
| `manabox_parser.py` | ManaBox CSV → inventory items |
| `scryfall_fetcher.py` | Scryfall enrichment with SQLite cache |
| `cardkingdom_fetcher.py` | CK pricelist fetch + name/edition/foil matcher |
| `mtgjson_bootstrap.py` | one-time 90-day history seed (streams, needs `ijson`) |
| `scoring_engine.py` | all scoring factors + report modes |
| `database.py` | SQLite schema and helpers |
| `sample_manabox_export.csv` | synthetic sample used by the test suite |
| `tests/` | offline test suite: `python3 -m pytest tests/ -q` |

The SQLite database (`sell_prep.db`), logs, and generated reports are
gitignored — they're local state, not code.
