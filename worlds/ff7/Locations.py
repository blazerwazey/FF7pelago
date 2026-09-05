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


# --- AP shop-slot grid -------------------------------------------------------
# The exe gives every shop ten 8-byte slots (ExeShopRecord::SLOT_COUNT = 10), and
# shops.json hand-authors only 1-6 AP slots per shop. The shop_slots_per_shop
# option can ask for more, but location ids are STATIC — location_name_to_id is
# built once at import — so a seed can never mint a new one. Instead the grid is
# pre-declared here up to the cap, and a seed instantiates only the first N of
# each shop. Deterministic, identical for every player.
#
# Existing 320000-320127 codes and their token ids are preserved exactly, so
# in-flight seeds and the hard-coded exclusions in __init__ still line up; only
# the generated slots are new, starting at 320128.
_AP_SHOP_SLOT_CAP = 10
_AP_SHOP_GENERATED_CODE_BASE = 320128

# Token ids for generated slots, REUSED across shops. The whole chain keys on
# (shop_id, section, token_id) — the client's ShopKey, ShopHook's SlotKey, and
# Gold Saucer's per-shop apply — so the same real id can be an AP slot in one
# shop and ordinary stock in another. Minting a unique id per slot the way the
# shipped data does would need ~470 of them; only 320 item and 91 materia ids
# exist, and the shipped 127 slots already burn 110 + 17. Reusing a fixed vector
# also cuts the items withheld from normal stock from 110 to 10.
#
# Both vectors are ids ALREADY shipped as AP tokens, so they are known-good
# rather than newly guessed.
_AP_SHOP_ITEM_TOKENS = (105, 106, 107, 108, 109, 110, 111, 112, 113, 114)
_AP_SHOP_MATERIA_TOKENS = (2, 4, 5, 6, 7, 8, 9, 10, 11, 22)


def _generate_shop_grid(records: List[dict]) -> List[dict]:
    """Fill each shop out to _AP_SHOP_SLOT_CAP slots, returning only the NEW ones.

    A generated slot inherits its shop's type and region and takes the next token
    from the vector that the shop is not already using — the shipped slots carry
    arbitrary ids, so a blind vector[k] could collide with one of them inside the
    same shop, which is the one place a token id must stay unique.
    """
    by_shop: Dict[int, List[dict]] = {}
    for record in records:
        by_shop.setdefault(record["shop_id"], []).append(record)

    generated: List[dict] = []
    code = _AP_SHOP_GENERATED_CODE_BASE
    for shop_id in sorted(by_shop):
        shipped = by_shop[shop_id]
        missing = _AP_SHOP_SLOT_CAP - len(shipped)
        if missing <= 0:
            continue
        template = shipped[0]
        token_type = template.get("token_type", "item")
        prefix = template["name"].rsplit(" - AP Slot", 1)[0]
        used = {r["token_id"] for r in shipped}
        vector = (_AP_SHOP_MATERIA_TOKENS if token_type == "materia"
                  else _AP_SHOP_ITEM_TOKENS)
        spare = [t for t in vector if t not in used]
        for offset in range(missing):
            if offset >= len(spare):
                # Not enough distinct tokens for this shop's type; stop rather
                # than emit a duplicate, which would make two AP slots in one
                # shop indistinguishable to ShopHook.
                break
            generated.append({
                "code": code,
                "name": f"{prefix} - AP Slot {len(shipped) + offset + 1}",
                "shop_id": shop_id,
                "token_id": spare[offset],
                "token_type": token_type,
                "region": template["region"],
            })
            code += 1
    return generated


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
    shop_records = _load_shop_dataset()
    # Order matters only for readability; the generated grid is deterministic.
    for record in list(shop_records) + _generate_shop_grid(shop_records):
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

# Each shop's slots in stable order: the hand-authored ones first (they carry the
# lower codes), then the generated ones. shop_slots_per_shop takes a prefix of
# this, so raising the option only ever ADDS slots and never renumbers existing
# ones — an important property for seeds already in flight.
SHOP_SLOTS_BY_SHOP: Dict[int, List[FF7LocationData]] = {}
for _data in sorted(SHOP_LOCATION_TABLE.values(), key=lambda d: d.code):
    SHOP_SLOTS_BY_SHOP.setdefault(_data.shop_id, []).append(_data)

# Codes shipped in shops.json, i.e. the default per-shop counts. Used when
# shop_slots_per_shop is 0 ("leave the counts as they are").
SHIPPED_SHOP_CODES: set = {
    r["code"] for r in _load_shop_dataset()
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
