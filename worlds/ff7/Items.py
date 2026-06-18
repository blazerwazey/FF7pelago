"""Final Fantasy VII Archipelago item definitions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Dict

from BaseClasses import Item, ItemClassification

from ._resources import load_json


@dataclass(frozen=True)
class FF7ItemData:
    """Static data for an FF7 item."""

    name: str
    code: int
    classification: ItemClassification
    ff7_id: int | None = None
    category: str | None = None  # item, weapon, armor, accessory, materia, key_item
    count: int = 1  # number of copies to add to the item pool

CLASSIFICATION_MAP = {
    "progression": ItemClassification.progression,
    "useful": ItemClassification.useful,
    "filler": ItemClassification.filler,
}


def _load_item_dataset() -> list[dict[str, str]]:
    return load_json("data/items.json")


def _build_item_table() -> Dict[str, FF7ItemData]:
    table: Dict[str, FF7ItemData] = {}
    for record in _load_item_dataset():
        classification = CLASSIFICATION_MAP.get(record["classification"], ItemClassification.filler)
        ff7_id = record.get("ff7_id")  # None for key items / materia / unknowns
        item = FF7ItemData(
            record["name"],
            record["code"],
            classification,
            ff7_id,
            record.get("category"),
            record.get("count", 1),
        )
        table[item.name] = item
    return table


ITEM_TABLE: Dict[str, FF7ItemData] = _build_item_table()


item_name_groups: Dict[str, set[str]] = {
    "Progression": {name for name, data in ITEM_TABLE.items() if data.classification is ItemClassification.progression},
    "Useful": {name for name, data in ITEM_TABLE.items() if data.classification is ItemClassification.useful},
    "Filler": {name for name, data in ITEM_TABLE.items() if data.classification is ItemClassification.filler},
    "Vehicles": {name for name in ITEM_TABLE if any(keyword in name for keyword in ("Tiny Bronco", "Highwind", "Submarine", "Snowboard"))},
    "Key Cards": {name for name in ITEM_TABLE if name.startswith("Keycard")},
    "Coupons": {name for name in ITEM_TABLE if name.endswith("Coupon")},
}


class FF7Item(Item):
    """Archipelago item wrapper for FF7."""

    game: ClassVar[str] = "Final Fantasy VII"


def get_item_data(name: str) -> FF7ItemData:
    """Look up the FF7 item metadata, raising if missing."""

    try:
        return ITEM_TABLE[name]
    except KeyError as exc:  # pragma: no cover - guard rails
        raise KeyError(f"Unknown FF7 item: {name}") from exc


def create_ff7_item(name: str, player: int) -> FF7Item:
    """Create an Archipelago FF7 item instance."""

    data = get_item_data(name)
    return FF7Item(name, data.classification, data.code, player)


item_name_to_id: Dict[str, int] = {name: data.code for name, data in ITEM_TABLE.items()}
