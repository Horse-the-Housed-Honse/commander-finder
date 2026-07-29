"""MTGJSON AllPrices bootstrap: seed ~90 days of price history on day one.

Downloads (streamed, never fully in memory — requires the `ijson` package):

  1. AllIdentifiers.json.gz  -> maps MTGJSON UUID -> Scryfall ID; we keep
     only UUIDs whose scryfallId is in the collection.
  2. AllPrices.json.gz       -> per-UUID daily prices from several
     providers; we ingest Card Kingdom buylist/retail and TCGplayer
     retail for the kept UUIDs into price_history.

Rows are inserted with source 'mtgjson:<provider>' so the scoring engine
can prefer our own daily 'ck' snapshots when dates overlap. After this
one-time seed, snapshot_prices.py extends history past MTGJSON's rolling
90-day window.
"""

import gzip
import urllib.request
from typing import Dict, Iterable, Set

import database

ALL_IDENTIFIERS_URL = "https://mtgjson.com/api/v5/AllIdentifiers.json.gz"
ALL_PRICES_URL = "https://mtgjson.com/api/v5/AllPrices.json.gz"
USER_AGENT = "MTGSellPrepTool/1.0 (personal collection tool)"

# provider name in MTGJSON -> (source label, kinds we ingest)
PROVIDERS = {
    "cardkingdom": ("mtgjson:cardkingdom", ("buylist", "retail")),
    "tcgplayer": ("mtgjson:tcgplayer", ("retail",)),
}
# MTGJSON finish keys map 1:1 onto our finish values
FINISHES = ("normal", "foil", "etched")


def _require_ijson():
    try:
        import ijson
        return ijson
    except ImportError:
        raise SystemExit(
            "The MTGJSON bootstrap needs the 'ijson' package for streaming "
            "(the file is ~2 GB uncompressed).\n"
            "Install it with:  pip3 install ijson\n"
            "Then re-run with --bootstrap."
        )


def _open_stream(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    resp = urllib.request.urlopen(req, timeout=600)
    return gzip.GzipFile(fileobj=resp)


def build_uuid_map(scryfall_ids: Set[str], _open=_open_stream) -> Dict[str, str]:
    """Stream AllIdentifiers and return {mtgjson_uuid: scryfall_id} for
    only the cards we own."""
    ijson = _require_ijson()
    wanted = {s.lower() for s in scryfall_ids if s}
    uuid_map: Dict[str, str] = {}
    stream = _open(ALL_IDENTIFIERS_URL)
    try:
        for uuid, card in ijson.kvitems(stream, "data"):
            sid = ((card.get("identifiers") or {}).get("scryfallId") or "").lower()
            if sid in wanted:
                uuid_map[uuid] = sid
    finally:
        stream.close()
    print(f"[mtgjson] identifier map: {len(uuid_map)} printings matched "
          f"out of {len(wanted)} owned Scryfall IDs")
    return uuid_map


def ingest_prices(conn, uuid_map: Dict[str, str], _open=_open_stream) -> int:
    """Stream AllPrices and append history rows for owned cards only."""
    ijson = _require_ijson()
    inserted = 0
    stream = _open(ALL_PRICES_URL)
    try:
        for uuid, entry in ijson.kvitems(stream, "data"):
            sid = uuid_map.get(uuid)
            if not sid:
                continue
            paper = entry.get("paper") or {}
            for provider, (source, kinds) in PROVIDERS.items():
                pdata = paper.get(provider) or {}
                for kind in kinds:
                    kdata = pdata.get(kind) or {}
                    for finish in FINISHES:
                        for date, price in (kdata.get(finish) or {}).items():
                            try:
                                price = float(price)
                            except (TypeError, ValueError):
                                continue
                            if database.add_history_row(
                                    conn, source, sid, finish, kind,
                                    str(date)[:10], price):
                                inserted += 1
    finally:
        stream.close()
    conn.commit()
    print(f"[mtgjson] bootstrap complete: {inserted} history rows added")
    return inserted


def bootstrap(conn, scryfall_ids: Iterable[str],
              _open=_open_stream) -> int:
    """Full bootstrap. Warning printed up front because the downloads are
    large (a few hundred MB compressed) — this is a once-in-a-while job,
    not something the weekly report should redo."""
    print("[mtgjson] downloading AllIdentifiers + AllPrices (large files, "
          "streamed — this can take several minutes)...")
    uuid_map = build_uuid_map(set(scryfall_ids), _open=_open)
    if not uuid_map:
        print("[mtgjson] nothing matched — skipping price ingest")
        return 0
    return ingest_prices(conn, uuid_map, _open=_open)
