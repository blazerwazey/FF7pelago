# Final Fantasy VII — Archipelago World (FF7pelago)

Archipelago integration for **Final Fantasy VII** (Steam / PC, `FF7_EN.exe` US v1.02). This apworld defines the
items, locations, regions, and logic; a companion **Final Fantasy VII Client** syncs with the running game, and the
**Gold Saucer** randomizer patches the actual game files from the seed.

For player-facing docs see `docs/en_Final Fantasy VII.md` (game info) and `docs/multiworld_en.md` (setup guide).
A ready-to-edit settings file is in `Final Fantasy VII.yaml`.

## Pipeline

```
YAML  ──generate──▶  AP_<seed>_P<slot>_<name>.apff7   (JSON payload, written by generate_output)
                         │
                         ├─▶ Gold Saucer  ──patches──▶ randomized game files (flevel.lgp, shops, starting gear)
                         │                              run via 7th Heaven (ships shophook.dll)
                         │
        slot_data ───────┴─▶ Final Fantasy VII Client (pymem)
                              • reads Savemap @ 0xDBFD38, fires checks on field-flag 0→1
                              • writes received items/materia/vehicles into the save
                              • injects shophook.dll for Tier-3 shop slots + AP names
                              • applies EXP/Gil/AP battle multipliers
```

The client prefers the server's `slot_data` (shops + BITON map) so no file is needed; `/setjson <.apff7>` is the
offline/debug fallback.

## Layout

| File | Purpose |
|------|---------|
| `__init__.py` | `FF7World` — items, regions (linear + Free Roam), rules, fill, `fill_slot_data`, `generate_output`, launcher component |
| `Options.py` | `FF7Options` (randomizers, Free Roam, multipliers, goal, DeathLink) |
| `Items.py` / `Locations.py` | Item & location tables, name↔id maps, name groups, shop-slot table |
| `Rules.py` | Access-rule application |
| `json_export.py` | `FF7JSONExporter` — builds & writes the `.apff7` payload (placements, shops, BITON map) |
| `biton_mapper.py` | Field-pickup BITON flag resolution / LGP scan fallback |
| `FF7Client.py` | The runtime client (memory I/O, item delivery, shop detection, multipliers) |
| `dll_inject.py` | Injects `shophook.dll` into the running game |
| `data/` | Bundled item/location/flag datasets (`*.json`) |
| `docs/` | Player-facing game info + setup tutorial |
| `test/` | World unit tests |

## Key Behaviors

- **Goal:** defeat Sephiroth (`Northern Crater - Defeat Sephiroth` → grants hidden `FF7 Victory`). In Free Roam the
  victory location is gated behind Highwind + all party members + all 4 Huge Materia.
- **Free Roam (default):** starts at world-map game-moment 1603; traversal (vehicles, Midgar re-entry, Lunar Harp,
  etc.) is gated behind received items. `Green Chocobo` and `Submarine` are forced early.
- **Shops (Tier-3):** reserved item/materia ids act as AP "tokens"; `shophook.dll` shows the AP name/description and
  signals purchases via `shop_buys.txt`, which the client consumes to fire the check.

## Building the .apworld

From the repo that contains `worlds/ff7`:

```
python build_ff7_apworld.py            # writes ./ff7.apworld
python build_ff7_apworld.py <outdir>   # writes <outdir>/ff7.apworld
```

The script zips `worlds/ff7` as a single top-level `ff7/` module (skipping `__pycache__`/`*.pyc`) and verifies the
structure. Install the result into your Archipelago `custom_worlds/` (or `worlds/`).

## Tests

```
python test/general/  # or the AP test runner against worlds.ff7
```

Generation also runs `validate_locations.validate()` in `generate_early`, failing loudly on over-subscribed
`(map, item_text)` groups that would create dead/colliding checks.
