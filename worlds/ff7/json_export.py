"""Export Archipelago placements as a Gold Saucer seed file (.apff7).

The .apff7 format is a JSON file consumed directly by Gold Saucer.
Gold Saucer reads the file once at game-start and uses the placements array
to override its own internal randomization, then applies the rule options
to configure gameplay restrictions — no further contact with the AP server
is needed by Gold Saucer itself.

The FF7Client (FF7Client.py) handles live AP server communication separately:
- It monitors the game's memory to detect location checks.
- It delivers items received from AP to the game.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ._resources import try_load_json
from .Locations import ALL_LOCATION_TABLE

if TYPE_CHECKING:
    from .__init__ import FF7World

# (The old bank-1 BITON auto-allocator was removed — placements now use each
# pickup's own natural "taken" flag from locations.json / field_pickup_flags.json,
# which is collision-free. See build_biton_map_dict below.)


def _load_key_item_biton_map() -> Dict[str, Dict[str, int]]:
    return try_load_json("data/key_item_biton_map.json", {})


_KEY_ITEM_BITON_MAP: Dict[str, Dict[str, int]] = _load_key_item_biton_map()

# Each field pickup's NATURAL "taken" flag, extracted from ff7tk FieldItemList
# ({location_code: [bank, address, bit]}, field-engine paired-bank model). Using
# each pickup's own flag — instead of auto-allocating bits in a shared region —
# is collision-free: a flag only changes when that specific item is taken, so
# script-heavy maps (Fort Condor, etc.) no longer trigger unrelated locations.
def _load_field_pickup_flags() -> Dict[int, List[int]]:
    raw = try_load_json("data/field_pickup_flags.json", {})
    try:
        return {int(k): list(v) for k, v in raw.items()}
    except Exception:
        return {}


_FIELD_PICKUP_FLAGS: Dict[int, List[int]] = _load_field_pickup_flags()


def _classification_label(item) -> str:
    """Single-word Archipelago classification for a placed item, used in the
    shop-slot description (Progression / Useful / Filler / Trap). Bit-checks the
    ItemClassification flags so combinations (e.g. progression_skip_balancing,
    progression|useful) still resolve to one clear label by priority."""
    if item is None:
        return ""
    flags = int(item.classification)
    if flags & 0b100:        # trap
        return "Trap"
    if flags & 0b001:        # progression (incl. progression_skip_balancing)
        return "Progression"
    if flags & 0b010:        # useful
        return "Useful"
    return "Filler"


class FF7JSONExporter:
    """Creates the .apff7 seed file consumed by Gold Saucer."""

    def __init__(self, world: "FF7World") -> None:
        self.world = world

    @staticmethod
    def _resolve_biton(location_data: Any, item_name: str) -> Dict[str, int]:
        """Return {bank, address, bit} for a placement.

        Resolution order:
        1. Use the vanilla BITON stored on location_data (from locations.json).
        2. Fall back to {-1, -1, -1} if unknown (AP will assign one).

        Note: key_item_biton_map is intentionally NOT used for AP placements
        to avoid BITON collisions with vanilla game logic (e.g., Keycard 62
        collision causing double-checkoffs).
        """
        if location_data is not None:
            return {
                "bank":    location_data.bank,
                "address": location_data.address,
                "bit":     location_data.bit,
            }
        return {"bank": -1, "address": -1, "bit": -1}

    def _sorted_locations(self) -> list:
        """Locations with items, sorted by address for deterministic BITON order."""
        mw = self.world.multiworld
        return sorted(
            (loc for loc in mw.get_locations(self.world.player)
             if loc.item and loc.address is not None),
            key=lambda l: l.address,
        )

    def build_biton_map_dict(self) -> Dict[int, List[int]]:
        """Return {location_id: [bank, address, bit]} for every TRACKABLE location.

        Resolution per location:
          1. An explicit vanilla flag on the location (locations.json, e.g. keycards).
          2. The pickup's natural field-item flag from ff7tk (_FIELD_PICKUP_FLAGS).
          3. Otherwise it is not a real field pickup -> omitted (not trackable).
            The world excludes those from the pool, so they should not appear here.
        """
        result: Dict[int, List[int]] = {}
        for location in self._sorted_locations():
            location_data = ALL_LOCATION_TABLE.get(location.name)
            loc_code = location_data.code if location_data else location.address
            if location_data is not None and location_data.bank >= 0:
                result[loc_code] = [location_data.bank,
                                    location_data.address,
                                    location_data.bit]
                continue
            flag = _FIELD_PICKUP_FLAGS.get(loc_code)
            if flag is not None:
                result[loc_code] = list(flag)
        return result

    def _serialize_placements(self) -> List[Dict[str, Any]]:
        """Serialize every field-item placement for Gold Saucer consumption.

        Each placement entry tells Gold Saucer:
        - Which location (field, map name, field-script offset data)
        - Which item to place there (by FF7 item id)
        - Whether the item belongs to this player or another AP player
        - BITON bank/address/bit for the client to detect location checks
        """
        mw = self.world.multiworld
        placements: List[Dict[str, Any]] = []
        biton_map = self.build_biton_map_dict()

        for location in self._sorted_locations():
            location_data = ALL_LOCATION_TABLE.get(location.name)
            if location_data is not None and location_data.category == "shop":
                continue  # shop slots are emitted in the dedicated "shops" array
            # Use location.code (FF7 location code) as key, not location.address (AP routing address)
            loc_code = location_data.code if location_data else location.address
            b = biton_map.get(loc_code, [-1, -1, -1])

            # ff7_id is the FF7 internal item ID embedded in items.json
            item_ff7_id: Optional[int] = None
            if location.item.code is not None:
                from .Items import ITEM_TABLE
                item_data = ITEM_TABLE.get(location.item.name)
                if item_data is not None:
                    item_ff7_id = item_data.ff7_id

            placements.append({
                "location":              location.name,
                "location_id":           location_data.code if location_data else location.address,
                "map":                   location_data.map if location_data else "",
                "maps":                  location_data.maps if location_data else [],
                "item_text":             location_data.item_text if location_data else "",
                "category":              location_data.category if location_data else "standard",
                "item":                  location.item.name,
                "item_id":               location.item.code,
                "item_ff7_id":           item_ff7_id,
                "item_owner":            mw.get_player_name(location.item.player),
                "item_owner_slot":       location.item.player,
                "item_is_local":         location.item.player == self.world.player,
                "item_classification":   location.item.classification.name,
                "bank":                  b[0],
                "address":               b[1],
                "bit":                   b[2],
            })
        return placements

    def _serialize_shops(self) -> List[Dict[str, Any]]:
        """Serialize native-grid Tier-3 shop slots.

        Each entry tells Gold Saucer to make shop ``shop_id`` sell item
        ``token_id`` (reserved from normal stock), and tells the client to map
        that token id to this AP location + show the AP item's name on it.
        """
        from .Locations import SHOP_LOCATION_TABLE
        from .Items import ITEM_TABLE
        mw = self.world.multiworld
        out: List[Dict[str, Any]] = []
        for location in mw.get_locations(self.world.player):
            shop_data = SHOP_LOCATION_TABLE.get(location.name)
            if shop_data is None:
                continue
            item = location.item
            item_ff7_id: Optional[int] = None
            if item is not None and item.code is not None:
                idata = ITEM_TABLE.get(item.name)
                if idata is not None:
                    item_ff7_id = idata.ff7_id
            out.append({
                "location":      location.name,
                "location_id":   shop_data.code,
                "shop_id":       shop_data.shop_id,
                "token_id":      shop_data.token_id,
                "token_type":    shop_data.token_type,
                "item":          item.name if item else "",
                "item_id":       item.code if item else None,
                "item_ff7_id":   item_ff7_id,
                "item_owner":    mw.get_player_name(item.player) if item else "",
                "item_is_local": (item.player == self.world.player) if item else False,
                "item_classification": _classification_label(item),
            })
        out.sort(key=lambda e: e["location_id"])
        return out

    def _serialize_rules(self) -> Dict[str, Any]:
        """Export Gold Saucer rule configuration from AP options."""
        opts = self.world.options
        return {
            # Randomizers
            "randomize_field_items": bool(opts.randomize_field_items),
            "field_items_mode":      int(opts.field_items_mode),
            "field_items_keep_type": bool(opts.field_items_keep_type),
            "randomize_shops":       bool(opts.randomize_shops),
            # These options were removed from Options.py; emit defaults so the
            # .apff7 schema stays stable for Gold Saucer (features off).
            "disable_shops":         False,
            "randomize_bosses":      False,
            "boss_min_stat_multiplier": 1.0,
            "boss_max_stat_multiplier": 1.0,
            # Base GS features
            "randomize_starting_equipment": bool(opts.randomize_starting_equipment),
            "starting_equipment_tier":      int(opts.starting_equipment_tier),
            # World
            "free_roam":             bool(opts.free_roam),
            # Goal
            "victory_condition":     int(opts.victory_condition),
            "death_link":            bool(opts.death_link),
        }

    def build_payload(self) -> Dict[str, Any]:
        mw = self.world.multiworld
        player = self.world.player
        opts = self.world.options

        # Build features array for Gold Saucer.
        # GS expects a boolean array indexed by the Feature enum:
        #   0=EnemyStatsRandomization, 1=ShopRandomization,
        #   2=FieldPickupRandomization, 3=StartingEquipmentRandomization,
        #   4=ArchipelagoIntegration, 5=TextReplacement,
        #   6=BossProtection, 7=EnemyEncounterRandomization
        features = [
            False,                                    # 0 EnemyStatsRandomization (unsupported)
            bool(opts.randomize_shops),               # 1 ShopRandomization
            bool(opts.randomize_field_items),         # 2 FieldPickupRandomization
            bool(opts.randomize_starting_equipment),  # 3 StartingEquipmentRandomization
            True,                                     # 4 ArchipelagoIntegration (always on)
            True,                                     # 5 TextReplacement
            False,                                    # 6 BossProtection (option removed)
            False,                                    # 7 EnemyEncounterRandomization (unsupported)
        ]

        return {
            "format":    "apff7",
            "version":   1,
            "game":      self.world.game,
            "seed":      mw.seed,
            "seed_name": mw.seed_name,
            "free_roam": bool(opts.free_roam),
            "player": {
                "slot": player,
                "name": mw.get_player_name(player),
            },
            "players": [
                {
                    "slot": slot,
                    "name": mw.get_player_name(slot),
                    "game": mw.game[slot],
                }
                for slot in mw.player_ids
            ],
            "features":   features,
            "rules":      self._serialize_rules(),
            "placements": self._serialize_placements(),
            "shops":      self._serialize_shops(),
        }

    def write_file(self, output_directory: str) -> str:
        payload = self.build_payload()
        # Use the standard AP output base name: "AP_<seed>_P<player>_<name>".
        # The leading "P" on the player segment is REQUIRED — the WebHost
        # (archipelago.gg) parses unrecognised seed files as AP_<seed>_P<n>_<name>
        # and does int(slot_id[1:]) to strip that "P". A hand-rolled name without
        # it (e.g. AP_<seed>_<n>_<name>) makes slot_id="<n>" -> int("") and the
        # host rejects the whole multidata ("invalid literal for int()").
        base = self.world.multiworld.get_out_file_name_base(self.world.player)
        filename = f"{base}.apff7"
        path = Path(output_directory, filename)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return str(path)
