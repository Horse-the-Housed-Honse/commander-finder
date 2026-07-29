"""Offline test suite: exercises the full pipeline against fixture data,
with no network access. Run from the sell_tool directory:

    python3 -m pytest tests/ -v
"""

import csv
import json
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
import scoring_engine
import scryfall_fetcher
import cardkingdom_fetcher
import sell_prep_tool
import snapshot_prices
from manabox_parser import parse_manabox_csv

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_CSV = os.path.join(HERE, "..", "sample_manabox_export.csv")
TODAY = date(2026, 7, 29)

# ---- fixtures -------------------------------------------------------------

SID = {
    "abrade": "aaaa1111-2222-3333-4444-555566667001",
    "artisan_foil": "aaaa1111-2222-3333-4444-555566667002",
    "artisan": "aaaa1111-2222-3333-4444-555566667003",
    "atraxa": "aaaa1111-2222-3333-4444-555566667004",
    "bolt_clb": "aaaa1111-2222-3333-4444-555566667005",
    "bolt_2x2": "aaaa1111-2222-3333-4444-555566667006",
    "solring": "aaaa1111-2222-3333-4444-555566667007",
    "solring_foil": "aaaa1111-2222-3333-4444-555566667008",
    "swamp": "aaaa1111-2222-3333-4444-555566667009",
}


def _card(sid, name, set_name, type_line, usd, usd_foil=None,
          edhrec_rank=500, oracle_text=""):
    return {"id": sid, "name": name, "set_name": set_name,
            "type_line": type_line, "oracle_text": oracle_text,
            "edhrec_rank": edhrec_rank,
            "prices": {"usd": usd, "usd_foil": usd_foil, "usd_etched": None}}


FAKE_SCRYFALL = {
    SID["abrade"]: _card(SID["abrade"], "Abrade", "Secret Lair Drop",
                         "Instant", "1.50"),
    SID["artisan_foil"]: _card(SID["artisan_foil"],
                               "Maelstrom Artisan // Rocket Volley",
                               "Duskmourn: House of Horror",
                               "Creature — Human Artificer // Instant",
                               "2.00", "9.00"),
    SID["artisan"]: _card(SID["artisan"],
                          "Maelstrom Artisan // Rocket Volley",
                          "Duskmourn: House of Horror",
                          "Creature — Human Artificer // Instant",
                          "2.00", "9.00"),
    SID["atraxa"]: _card(SID["atraxa"], "Atraxa, Praetors' Voice",
                         "Commander Anthology Volume II",
                         "Legendary Creature — Phyrexian Angel Horror",
                         "22.00", edhrec_rank=40),
    SID["bolt_clb"]: _card(SID["bolt_clb"], "Lightning Bolt",
                           "Commander Legends: Battle for Baldur's Gate",
                           "Instant", "1.20"),
    SID["bolt_2x2"]: _card(SID["bolt_2x2"], "Lightning Bolt",
                           "Double Masters 2022", "Instant", "1.40"),
    SID["solring"]: _card(SID["solring"], "Sol Ring", "Commander Masters",
                          "Artifact", "1.80", "4.00", edhrec_rank=1),
    SID["solring_foil"]: _card(SID["solring_foil"], "Sol Ring",
                               "Commander Masters", "Artifact",
                               "1.80", "4.00", edhrec_rank=1),
    SID["swamp"]: _card(SID["swamp"], "Swamp", "Revised Edition",
                        "Basic Land — Swamp", "0.30", edhrec_rank=None),
}


def _ck(name, edition, foil, buy, retail, qty_buying, variation=""):
    return {"id": name + edition + str(foil), "name": name,
            "variation": variation, "edition": edition,
            "is_foil": "true" if foil else "false",
            "price_retail": str(retail), "qty_retail": 10,
            "price_buy": str(buy), "qty_buying": qty_buying}


FAKE_CK_RAW = [
    _ck("Abrade", "Secret Lair", False, 0.40, 1.99, 8),
    # CK lists only the front face of the DFC:
    _ck("Maelstrom Artisan", "Duskmourn: House of Horror", True, 4.00, 12.99, 4),
    _ck("Maelstrom Artisan", "Duskmourn: House of Horror", False, 0.80, 2.49, 12),
    _ck("Atraxa, Praetors' Voice", "Commander Anthology Volume II",
        False, 14.00, 27.99, 6),
    _ck("Lightning Bolt", "Commander Legends: Battle for Baldur's Gate",
        False, 0.50, 1.49, 20),
    # qty_buying == 0 -> "not currently buying" even though a price exists:
    _ck("Lightning Bolt", "Double Masters 2022", False, 0.60, 1.79, 0),
    _ck("Sol Ring", "Commander Masters", False, 0.75, 2.29, 30),
    _ck("Sol Ring", "Commander Masters", True, 1.50, 4.99, 10),
    # 3rd Edition is CK's name for Scryfall's "Revised Edition":
    _ck("Swamp", "3rd Edition", False, 0.05, 0.35, 100),
]


def fake_scryfall_fetch(conn, ids, force=False, **kw):
    fetched_at = "2026-07-29T00:00:00Z"
    out = {}
    for sid in set(i for i in ids if i):
        if sid in FAKE_SCRYFALL:
            out[sid] = FAKE_SCRYFALL[sid]
            database.cache_card(conn, sid, FAKE_SCRYFALL[sid], fetched_at)
    conn.commit()
    return out


def fake_ck_fetch(conn, force=False, **kw):
    rows = cardkingdom_fetcher._clean_rows(FAKE_CK_RAW)
    database.replace_ck_pricelist(conn, rows, "2026-07-29T00:00:00Z")
    return [dict(r) for r in database.load_ck_pricelist(conn)]


def seed_history(conn, sid, finish, kind, prices, end=TODAY):
    """prices = list ordered oldest->newest, one per day ending at `end`."""
    for i, price in enumerate(prices):
        d = (end - timedelta(days=len(prices) - 1 - i)).isoformat()
        database.add_history_row(conn, "mtgjson:tcgplayer" if kind == "retail"
                                 else "mtgjson:cardkingdom",
                                 sid, finish, kind, d, price)
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    c = database.connect(str(tmp_path / "test.db"))
    yield c
    c.close()


def run_pipeline(conn, tmp_path, mode="standard", **overrides):
    args = sell_prep_tool.parse_args(
        ["--csv", SAMPLE_CSV, "--db", str(tmp_path / "test.db"),
         "--mode", mode, "--no-bootstrap"])
    for k, v in overrides.items():
        setattr(args, k, v)
    return sell_prep_tool.build_report(
        conn, args, scryfall_fetch=fake_scryfall_fetch,
        ck_fetch=fake_ck_fetch, today=TODAY)


# ---- parser ----------------------------------------------------------------

class TestParser:
    def test_all_rows_parsed_distinct(self):
        items = parse_manabox_csv(SAMPLE_CSV)
        assert len(items) == 9
        artisan = [i for i in items if i.name.startswith("Maelstrom")]
        assert {i.foil for i in artisan} == {"foil", "normal"}  # never merged

    def test_blank_purchase_price_is_none_not_zero(self):
        items = parse_manabox_csv(SAMPLE_CSV)
        abrade = next(i for i in items if i.name == "Abrade")
        assert abrade.purchase_price is None

    def test_binder_type_and_added_carried_through(self):
        items = parse_manabox_csv(SAMPLE_CSV)
        swamp = next(i for i in items if i.name == "Swamp")
        assert swamp.binder_type == "box"
        assert swamp.added.startswith("2025-03-30")


# ---- CK matching ------------------------------------------------------------

class TestCKMatching:
    def make_index(self):
        return cardkingdom_fetcher.CKIndex(
            cardkingdom_fetcher._clean_rows(FAKE_CK_RAW))

    def test_exact_edition_and_foil(self):
        row, q = self.make_index().match(
            "Sol Ring", "Commander Masters", "foil")
        assert q == "exact" and row["is_foil"] and row["price_buy"] == 1.50

    def test_dfc_matches_front_face(self):
        row, q = self.make_index().match(
            "Maelstrom Artisan // Rocket Volley",
            "Duskmourn: House of Horror", "normal")
        assert row is not None and row["price_buy"] == 0.80

    def test_edition_alias_revised_to_3rd(self):
        row, q = self.make_index().match("Swamp", "Revised Edition", "normal")
        assert q == "alias" and row["edition"] == "3rd Edition"

    def test_secret_lair_alias(self):
        row, q = self.make_index().match("Abrade", "Secret Lair Drop", "normal")
        assert q == "alias" and row["edition"] == "Secret Lair"

    def test_fuzzy_edition(self):
        index = cardkingdom_fetcher.CKIndex(cardkingdom_fetcher._clean_rows(
            [_ck("Atraxa, Praetors' Voice", "Commander Anthology Vol II",
                 False, 14.00, 27.99, 6)]))
        row, q = index.match("Atraxa, Praetors' Voice",
                             "Commander Anthology Volume II", "normal")
        assert q == "fuzzy" and row is not None

    def test_prefix_rule_rejects_different_set_with_long_suffix(self):
        index = cardkingdom_fetcher.CKIndex(cardkingdom_fetcher._clean_rows(
            [_ck("Lightning Bolt", "Commander Legends", False, 0.5, 1.5, 5)]))
        row, q = index.match(
            "Lightning Bolt",
            "Commander Legends: Battle for Baldur's Gate", "normal")
        assert row is None and q == "none"

    def test_no_cross_foil_match(self):
        row, q = self.make_index().match("Abrade", "Secret Lair Drop", "foil")
        assert row is None and q == "none"

    def test_credit_multiplier(self):
        assert cardkingdom_fetcher.credit_price(10.00) == 13.00


# ---- scoring ----------------------------------------------------------------

class TestScoring:
    def test_excess_playset_threshold(self):
        card = FAKE_SCRYFALL[SID["bolt_clb"]]
        assert scoring_engine.excess_for_name(6, card) == 2
        assert scoring_engine.excess_for_name(4, card) == 0

    def test_excess_singleton_for_legendary_creature(self):
        card = FAKE_SCRYFALL[SID["atraxa"]]
        assert scoring_engine.excess_for_name(2, card) == 1

    def test_basic_lands_never_excess(self):
        card = FAKE_SCRYFALL[SID["swamp"]]
        assert scoring_engine.excess_for_name(12, card) == 0

    def test_momentum_from_history(self, conn):
        seed_history(conn, SID["solring"], "normal", "retail",
                     [1.00] * 60 + [1.00 + 0.02 * i for i in range(31)])
        series = scoring_engine.price_series(conn, SID["solring"], "normal",
                                             today=TODAY)
        pct = scoring_engine.pct_change(series, 30, today=TODAY)
        assert pct is not None and 55 <= pct <= 65   # ~+60%

    def test_own_snapshots_beat_mtgjson_on_same_date(self, conn):
        d = TODAY.isoformat()
        database.add_history_row(conn, "mtgjson:tcgplayer",
                                 SID["solring"], "normal", "retail", d, 5.00)
        database.add_history_row(conn, "ck",
                                 SID["solring"], "normal", "retail", d, 2.29)
        series = scoring_engine.price_series(conn, SID["solring"], "normal",
                                             today=TODAY)
        # single date -> falls back to buylist which is empty, so seed one more
        database.add_history_row(conn, "mtgjson:tcgplayer", SID["solring"],
                                 "normal", "retail",
                                 (TODAY - timedelta(days=1)).isoformat(), 5.00)
        series = scoring_engine.price_series(conn, SID["solring"], "normal",
                                             today=TODAY)
        assert series[d] == 2.29


# ---- end-to-end ---------------------------------------------------------------

class TestPipeline:
    def test_standard_report(self, conn, tmp_path):
        scored, payout_key = run_pipeline(conn, tmp_path)
        assert payout_key == "est_payout_credit"
        assert len(scored) == 9
        by_name = {(s.name, s.foil): s for s in scored}

        atraxa = by_name[("Atraxa Praetors' Voice", "normal")]
        assert atraxa.ck_cash == 14.00
        assert atraxa.ck_credit == 18.20            # 14 * 1.30
        assert atraxa.ck_buying is True
        assert atraxa.excess_flag is True           # 2 owned, legendary -> keep 1
        assert atraxa.est_payout_credit == pytest.approx(36.40)  # NM x2
        assert atraxa.gain_ck == pytest.approx(-4.00)

        # blank purchase price -> gain columns stay None, no crash
        abrade = by_name[("Abrade", "normal")]
        assert abrade.purchase_price is None
        assert abrade.gain_ck is None and abrade.gain_scryfall is None

        # qty_buying == 0 -> not buying, zero payout, capped score
        bolt2 = by_name[("Lightning Bolt", "normal")] \
            if by_name[("Lightning Bolt", "normal")].set_code == "2x2" else None
        bolts = [s for s in scored if s.name == "Lightning Bolt"]
        b2x2 = next(s for s in bolts if s.set_code == "2x2")
        assert b2x2.ck_buying is False
        assert b2x2.est_payout_credit == 0.0
        assert b2x2.sell_score <= scoring_engine.NOT_BUYING_SCORE_CAP
        # both bolt printings excess-flagged: 6 total owned, keep 4
        assert all(s.excess_flag and s.excess_copies == 2 for s in bolts)

        # condition multiplier: 'good' Sol Ring foil pays 60% of NM
        srf = by_name[("Sol Ring", "foil")]
        assert srf.est_payout_cash == pytest.approx(1.50 * 0.60, abs=0.01)

    def test_sorted_by_binder_then_payout_desc(self, conn, tmp_path):
        scored, _ = run_pipeline(conn, tmp_path)
        binders = [s.binder_name for s in scored]
        assert binders == sorted(binders, key=str.lower)
        for binder in set(binders):
            payouts = [s.est_payout_credit for s in scored
                       if s.binder_name == binder]
            assert payouts == sorted(payouts, reverse=True)

    def test_liquidity_mode_excludes_not_buying(self, conn, tmp_path):
        scored, _ = run_pipeline(conn, tmp_path, mode="liquidity")
        assert all(s.ck_buying for s in scored)
        assert not any(s.set_code == "2x2" for s in scored)

    def test_peak_mode(self, conn, tmp_path):
        # rising card sits at its 90-day high -> included
        seed_history(conn, SID["solring"], "normal", "retail",
                     [1.00 + 0.01 * i for i in range(90)])
        # falling card is far from its high (today's CK retail of 27.99
        # still sits well under the 60.00 series max) -> excluded
        seed_history(conn, SID["atraxa"], "normal", "retail",
                     [60.00 - 0.25 * i for i in range(90)])
        scored, _ = run_pipeline(conn, tmp_path, mode="peak")
        names = {s.name for s in scored}
        assert "Sol Ring" in names
        assert "Atraxa Praetors' Voice" not in names

    def test_csv_and_json_output(self, conn, tmp_path):
        scored, payout_key = run_pipeline(conn, tmp_path)
        csv_path = str(tmp_path / "report.csv")
        json_path = str(tmp_path / "report.json")
        sell_prep_tool.write_csv(scored, csv_path)
        sell_prep_tool.write_json(scored, json_path, "standard", payout_key)

        with open(csv_path) as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 9
        for col in ("ck_cash", "ck_credit", "ck_buying", "excess_flag",
                    "binder_name", "est_payout_credit"):
            assert col in rows[0]

        with open(json_path) as fh:
            doc = json.load(fh)
        names = [b["binder_name"] for b in doc["binders"]]
        assert names == sorted(names, key=str.lower)
        assert doc["total_payout_credit"] > 0


# ---- snapshot idempotency ------------------------------------------------------

class TestSnapshot:
    def test_snapshot_idempotent_same_day(self, conn, tmp_path):
        run_pipeline(conn, tmp_path)   # loads inventory + records today's rows
        count_before = conn.execute(
            "SELECT COUNT(*) c FROM price_history WHERE source='ck'"
        ).fetchone()["c"]
        assert count_before > 0

        rows = database.load_inventory(conn)
        cards = database.get_cached_cards(
            conn, [r["scryfall_id"] for r in rows])
        index = cardkingdom_fetcher.CKIndex(fake_ck_fetch(conn))
        inserted = sell_prep_tool.snapshot_owned(conn, rows, cards, index,
                                                 today=TODAY)
        assert inserted == 0   # every point already exists for today
        count_after = conn.execute(
            "SELECT COUNT(*) c FROM price_history WHERE source='ck'"
        ).fetchone()["c"]
        assert count_after == count_before


# ---- MTGJSON bootstrap (streaming logic, fixture stream) -----------------------

class TestBootstrap:
    def test_bootstrap_with_fake_streams(self, conn):
        pytest.importorskip("ijson")
        import io, gzip as gz, mtgjson_bootstrap

        identifiers = {"data": {
            "uuid-1": {"identifiers": {"scryfallId": SID["solring"]}},
            "uuid-2": {"identifiers": {"scryfallId": "not-owned"}},
        }}
        prices = {"data": {
            "uuid-1": {"paper": {
                "cardkingdom": {
                    "buylist": {"normal": {"2026-07-01": 0.70, "2026-07-15": 0.75}},
                    "retail": {"normal": {"2026-07-01": 2.19}},
                },
                "tcgplayer": {"retail": {"normal": {"2026-07-01": 1.85}}},
            }},
            "uuid-2": {"paper": {"cardkingdom": {
                "buylist": {"normal": {"2026-07-01": 99.0}}}}},
        }}

        def fake_open(url):
            doc = identifiers if "AllIdentifiers" in url else prices
            buf = io.BytesIO()
            with gz.GzipFile(fileobj=buf, mode="wb") as f:
                f.write(json.dumps(doc).encode())
            buf.seek(0)
            return gz.GzipFile(fileobj=buf)

        n = mtgjson_bootstrap.bootstrap(conn, [SID["solring"]], _open=fake_open)
        assert n == 4   # 2 buylist + 1 ck retail + 1 tcg retail; not-owned skipped
        hist = database.get_history(conn, SID["solring"], "normal", "buylist")
        assert [h["price"] for h in hist] == [0.70, 0.75]
