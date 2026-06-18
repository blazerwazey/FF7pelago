"""Final Fantasy VII Archipelago location definitions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Dict, List

from BaseClasses import Location

from ._resources import load_json, try_load_json


@dataclass(frozen=True)
class FF7LocationData:
    """Static data describing a location in FF7."""

    name: str
    code: int
    map: str
    maps: List[str]
    item_text: str
    category: str
    bank: int = -1
    address: int = -1
    bit: int = -1
    # Shop-slot locations (category == "shop") only:
    token_id: int = -1      # reserved FF7 id the shop sells for this AP slot
    token_type: str = "item"  # "item" (composite id, item inventory) or "materia"
    shop_id: int = -1       # Gold Saucer shop index (0-79)
    region: str = ""        # Free Roam region this shop belongs to (access gate)


def _load_location_dataset() -> List[dict[str, object]]:
    return load_json("data/locations.json")


def _load_shop_dataset() -> List[dict[str, object]]:
    return try_load_json("data/shops.json", [])


def _build_location_table() -> Dict[str, FF7LocationData]:
    table: Dict[str, FF7LocationData] = {}
    for record in _load_location_dataset():
        data = FF7LocationData(
            name=record["name"],
            code=record["code"],
            map=record["map"],
            maps=record.get("maps", [record["map"]]),
            item_text=record.get("item_text", ""),
            category=record.get("category", "standard"),
            bank=record.get("bank", -1),
            address=record.get("address", -1),
            bit=record.get("bit", -1),
        )
        table[data.name] = data
    # Shop-slot AP locations (native-grid Tier-3 shops). Not field pickups —
    # detection is via inventory + gil polling in the client, not a BITON flag.
    for record in _load_shop_dataset():
        data = FF7LocationData(
            name=record["name"],
            code=record["code"],
            map=record.get("region", ""),
            maps=[],
            item_text="",
            category="shop",
            token_id=record["token_id"],
            token_type=record.get("token_type", "item"),
            shop_id=record["shop_id"],
            region=record["region"],
        )
        table[data.name] = data
    return table


ALL_LOCATION_TABLE: Dict[str, FF7LocationData] = _build_location_table()

# Shop-slot locations only (convenience view for region creation + export).
SHOP_LOCATION_TABLE: Dict[str, FF7LocationData] = {
    name: data for name, data in ALL_LOCATION_TABLE.items() if data.category == "shop"
}


def _load_placeable_codes() -> set[int]:
    """Location codes Gold Saucer can actually place an item at + the client can
    detect: those with an explicit vanilla flag, plus those with a natural
    field-item flag from ff7tk (field_pickup_flags.json). Anything else (battle
    arena prizes, shop/sage materia, dialogue gives) is not a real field pickup
    and cannot be tracked — the world drops these from the pool."""
    codes = {data.code for data in ALL_LOCATION_TABLE.values() if data.bank >= 0}
    # Boss locations are tracked by game-moment thresholds (BOSS_CHECKS in the
    # client), not a field-item flag, so include them explicitly.
    codes.update(data.code for data in ALL_LOCATION_TABLE.values() if data.category == "boss")
    raw = try_load_json("data/field_pickup_flags.json", {})
    try:
        codes.update(int(k) for k in raw)
    except Exception:
        pass
    return codes


PLACEABLE_LOCATION_CODES: set[int] = _load_placeable_codes()


def _build_location_groups() -> Dict[str, set[str]]:
    groups: Dict[str, set[str]] = {}

    # Group by primary map/region name
    for name, data in ALL_LOCATION_TABLE.items():
        groups.setdefault(data.map, set()).add(name)

    # Category groupings
    for name, data in ALL_LOCATION_TABLE.items():
        category_group = f"Category: {data.category}"
        groups.setdefault(category_group, set()).add(name)

    return groups


location_name_groups: Dict[str, set[str]] = _build_location_groups()


class FF7Location(Location):
    """Archipelago location wrapper for FF7."""

    game: ClassVar[str] = "Final Fantasy VII"


VICTORY_LOCATION_NAME = "Northern Crater - Defeat Sephiroth"

location_name_to_id: Dict[str, int] = {
    name: data.code
    for name, data in ALL_LOCATION_TABLE.items()
    if name != VICTORY_LOCATION_NAME
}
