"""Parse a ManaBox collection CSV export into inventory items.

Confirmed real export headers (order can vary; we match by name):

    Binder Name, Binder Type, Name, Set code, Set name, Collector number,
    Foil, Rarity, Quantity, ManaBox ID, Scryfall ID, Purchase price,
    Misprint, Altered, Condition, Language, Purchase price currency, Added

Rules honored here:
- `Purchase price` can be blank -> stored as None, never as 0.
- Rows are never merged by name: the same card can appear as distinct
  rows for foil vs non-foil or different printings.
- `Binder Type` and `Added` are carried through.
- `Scryfall ID` is the preferred join key downstream.
"""

import csv
from dataclasses import dataclass, field, asdict
from typing import Optional, List


@dataclass
class InventoryItem:
    binder_name: str
    binder_type: str
    name: str
    set_code: str
    set_name: str
    collector_number: str
    foil: str                     # "normal" | "foil" | "etched" (as exported)
    rarity: str
    quantity: int
    manabox_id: str
    scryfall_id: str
    purchase_price: Optional[float]   # None when the CSV field is blank
    misprint: str
    altered: str
    condition: str
    language: str
    purchase_currency: str
    added: str                    # ISO timestamp of when it entered the binder
    row_number: int = field(default=0)   # 1-based CSV data row, for error messages

    def to_dict(self):
        return asdict(self)


# CSV header -> InventoryItem attribute
_HEADER_MAP = {
    "binder name": "binder_name",
    "binder type": "binder_type",
    "name": "name",
    "set code": "set_code",
    "set name": "set_name",
    "collector number": "collector_number",
    "foil": "foil",
    "rarity": "rarity",
    "quantity": "quantity",
    "manabox id": "manabox_id",
    "scryfall id": "scryfall_id",
    "purchase price": "purchase_price",
    "misprint": "misprint",
    "altered": "altered",
    "condition": "condition",
    "language": "language",
    "purchase price currency": "purchase_currency",
    "added": "added",
}

REQUIRED_HEADERS = {"name", "quantity"}


class ManaBoxParseError(Exception):
    pass


def _parse_price(raw: str) -> Optional[float]:
    """Blank/whitespace purchase price means 'unknown cost basis', not $0."""
    if raw is None:
        return None
    raw = raw.strip().replace("$", "").replace(",", "")
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_quantity(raw: str, row_number: int) -> int:
    try:
        qty = int(float(raw.strip()))
    except (ValueError, AttributeError):
        raise ManaBoxParseError(
            f"Row {row_number}: Quantity {raw!r} is not a number"
        )
    if qty < 0:
        raise ManaBoxParseError(f"Row {row_number}: negative Quantity {qty}")
    return qty


def parse_manabox_csv(path: str) -> List[InventoryItem]:
    items: List[InventoryItem] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ManaBoxParseError(f"{path}: file is empty")

        # Normalize headers: case-insensitive, tolerant of stray spaces.
        normalized = {h: h.strip().lower() for h in reader.fieldnames}
        mapped = {h: _HEADER_MAP.get(norm) for h, norm in normalized.items()}

        found = {attr for attr in mapped.values() if attr}
        missing = REQUIRED_HEADERS - found
        if missing:
            raise ManaBoxParseError(
                f"{path}: missing required column(s): {sorted(missing)}. "
                f"Found headers: {reader.fieldnames}"
            )

        for row_number, row in enumerate(reader, start=1):
            record = {attr: "" for attr in _HEADER_MAP.values()}
            for header, attr in mapped.items():
                if attr and row.get(header) is not None:
                    record[attr] = row[header].strip()

            if not record["name"]:
                continue  # skip blank/padding rows quietly

            items.append(InventoryItem(
                binder_name=record["binder_name"],
                binder_type=record["binder_type"],
                name=record["name"],
                set_code=record["set_code"].lower(),
                set_name=record["set_name"],
                collector_number=record["collector_number"],
                foil=(record["foil"] or "normal").lower(),
                rarity=record["rarity"].lower(),
                quantity=_parse_quantity(record["quantity"], row_number),
                manabox_id=record["manabox_id"],
                scryfall_id=record["scryfall_id"].lower(),
                purchase_price=_parse_price(record["purchase_price"]),
                misprint=record["misprint"],
                altered=record["altered"],
                condition=record["condition"].lower(),
                language=record["language"],
                purchase_currency=record["purchase_currency"],
                added=record["added"],
                row_number=row_number,
            ))
    return items
