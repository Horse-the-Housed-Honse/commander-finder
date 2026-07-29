"""SQLite storage layer for the sell prep tool.

Tables
------
inventory       Reloaded wholesale from the ManaBox CSV on every report run
                (the CSV is the source of truth for what you own).
scryfall_cache  One JSON blob per Scryfall ID with a fetched_at timestamp,
                so repeat runs don't re-hit the API inside the TTL.
ck_pricelist    Latest Card Kingdom pricelist rows (replaced on each fetch).
price_history   Append-only. One row per (source, scryfall_id, finish, kind,
                date). This is what trend/momentum scoring reads.
meta            Small key/value store (last fetch timestamps etc.).
"""

import json
import sqlite3
from typing import Iterable, List, Optional, Dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binder_name TEXT, binder_type TEXT, name TEXT,
    set_code TEXT, set_name TEXT, collector_number TEXT,
    foil TEXT, rarity TEXT, quantity INTEGER,
    manabox_id TEXT, scryfall_id TEXT,
    purchase_price REAL, misprint TEXT, altered TEXT,
    condition TEXT, language TEXT, purchase_currency TEXT,
    added TEXT, row_number INTEGER
);

CREATE TABLE IF NOT EXISTS scryfall_cache (
    scryfall_id TEXT PRIMARY KEY,
    card_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ck_pricelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ck_id TEXT, name TEXT, variation TEXT, edition TEXT,
    is_foil INTEGER, price_retail REAL, qty_retail INTEGER,
    price_buy REAL, qty_buying INTEGER, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ck_name ON ck_pricelist(name);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,        -- 'ck' | 'mtgjson:cardkingdom' | 'mtgjson:tcgplayer' ...
    scryfall_id TEXT NOT NULL,
    finish TEXT NOT NULL,        -- 'normal' | 'foil' | 'etched'
    kind TEXT NOT NULL,          -- 'buylist' | 'retail'
    date TEXT NOT NULL,          -- YYYY-MM-DD
    price REAL NOT NULL,
    UNIQUE(source, scryfall_id, finish, kind, date)
);
CREATE INDEX IF NOT EXISTS idx_hist_card ON price_history(scryfall_id, finish, kind, date);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------- meta ----------

def get_meta(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# ---------- inventory ----------

def replace_inventory(conn, items: Iterable) -> int:
    conn.execute("DELETE FROM inventory")
    count = 0
    for it in items:
        conn.execute(
            """INSERT INTO inventory
               (binder_name, binder_type, name, set_code, set_name,
                collector_number, foil, rarity, quantity, manabox_id,
                scryfall_id, purchase_price, misprint, altered, condition,
                language, purchase_currency, added, row_number)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (it.binder_name, it.binder_type, it.name, it.set_code, it.set_name,
             it.collector_number, it.foil, it.rarity, it.quantity, it.manabox_id,
             it.scryfall_id, it.purchase_price, it.misprint, it.altered,
             it.condition, it.language, it.purchase_currency, it.added,
             it.row_number),
        )
        count += 1
    conn.commit()
    return count


def load_inventory(conn) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM inventory ORDER BY id").fetchall()


# ---------- scryfall cache ----------

def get_cached_cards(conn, scryfall_ids: Iterable[str]) -> Dict[str, dict]:
    out = {}
    for sid in scryfall_ids:
        row = conn.execute(
            "SELECT card_json, fetched_at FROM scryfall_cache WHERE scryfall_id = ?",
            (sid,),
        ).fetchone()
        if row:
            card = json.loads(row["card_json"])
            card["_fetched_at"] = row["fetched_at"]
            out[sid] = card
    return out


def cache_card(conn, scryfall_id: str, card: dict, fetched_at: str) -> None:
    card = {k: v for k, v in card.items() if not k.startswith("_")}
    conn.execute(
        "INSERT INTO scryfall_cache(scryfall_id, card_json, fetched_at) VALUES(?,?,?) "
        "ON CONFLICT(scryfall_id) DO UPDATE SET card_json = excluded.card_json, "
        "fetched_at = excluded.fetched_at",
        (scryfall_id, json.dumps(card), fetched_at),
    )


# ---------- CK pricelist ----------

def replace_ck_pricelist(conn, rows: Iterable[dict], fetched_at: str) -> int:
    conn.execute("DELETE FROM ck_pricelist")
    count = 0
    for r in rows:
        conn.execute(
            """INSERT INTO ck_pricelist
               (ck_id, name, variation, edition, is_foil, price_retail,
                qty_retail, price_buy, qty_buying, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (r["ck_id"], r["name"], r["variation"], r["edition"],
             1 if r["is_foil"] else 0, r["price_retail"], r["qty_retail"],
             r["price_buy"], r["qty_buying"], fetched_at),
        )
        count += 1
    set_meta(conn, "ck_fetched_at", fetched_at)
    conn.commit()
    return count


def load_ck_pricelist(conn) -> List[sqlite3.Row]:
    return conn.execute("SELECT * FROM ck_pricelist").fetchall()


# ---------- price history ----------

def add_history_row(conn, source: str, scryfall_id: str, finish: str,
                    kind: str, date: str, price: float) -> bool:
    """Append one observation. Returns True if inserted, False if that
    (source, card, finish, kind, date) point already existed."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO price_history"
        "(source, scryfall_id, finish, kind, date, price) VALUES(?,?,?,?,?,?)",
        (source, scryfall_id, finish, kind, date, price),
    )
    return cur.rowcount > 0


def get_history(conn, scryfall_id: str, finish: str, kind: str,
                since: Optional[str] = None) -> List[sqlite3.Row]:
    """All observations for a card ordered by date. When multiple sources
    have the same date, the caller decides how to merge (scoring engine
    prefers our own 'ck' snapshots over MTGJSON backfill)."""
    q = ("SELECT source, date, price FROM price_history "
         "WHERE scryfall_id = ? AND finish = ? AND kind = ?")
    args = [scryfall_id, finish, kind]
    if since:
        q += " AND date >= ?"
        args.append(since)
    q += " ORDER BY date"
    return conn.execute(q, args).fetchall()


def history_coverage(conn, scryfall_ids: List[str]) -> float:
    """Fraction of the given cards that have at least one history row.
    Used to decide whether the MTGJSON bootstrap is needed."""
    ids = [s for s in set(scryfall_ids) if s]
    if not ids:
        return 1.0
    have = 0
    for sid in ids:
        row = conn.execute(
            "SELECT 1 FROM price_history WHERE scryfall_id = ? LIMIT 1", (sid,)
        ).fetchone()
        if row:
            have += 1
    return have / len(ids)
