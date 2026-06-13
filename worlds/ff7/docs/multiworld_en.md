# Final Fantasy VII Multiworld Setup

1. Place the FF7 Archipelago world folder inside your Archipelago `worlds/` directory (or install the packaged
   `.apworld`).
2. Launch the Archipelago Options Creator and choose **Final Fantasy VII** to generate a settings YAML.
3. Host a multiworld with your YAML alongside other players. The host can reuse the standard Archipelago server tools.
4. Run the FF7 client from the Archipelago Launcher ("Final Fantasy VII Client").
5. Connect to the multiworld server using the slot name/password assigned in the YAML.
6. The placeholder client will respond to incoming items and report goal completion automatically once the special
   "FF7 Victory" item is received.

## Gold Saucer Integration

Every time you generate an FF7 slot, Archipelago drops a JSON file alongside the usual output archive:

- **File name:** `FF7_<seed_name>_P<slot>.json`
- **Location:** Inside the generated output folder alongside the spoiler log and patch files.

Import that JSON via Gold Saucer's **"Import JSON…"** button to enable Archipelago mode. Gold Saucer will use the
placement data to put the correct items at each field location when it generates the randomized game files.

### JSON Structure

The exported file contains the following top-level keys:

| Key | Description |
|-----|-------------|
| `game` | Always `"Final Fantasy VII"` |
| `seed` | Numeric seed used for this generation |
| `seed_name` | Human-readable seed identifier |
| `player` | Object with `slot` (int) and `name` (string) for this FF7 player |
| `players` | Array of all players in the multiworld (slot, name, game) |
| `options` | FF7-specific option values (field_randomization, key_item_randomization, etc.) |
| `common_options` | Standard Archipelago options (death_link, etc.) |
| `slot_data` | Object required for Gold Saucer validation — contains `item_name_to_id` and `location_name_to_id` |
| `items` | Full item catalog: name, numeric id, classification |
| `locations` | Full location catalog: name, id, map, maps, item_text, category |
| `item_name_groups` | Named item groups (Progression, Useful, Filler, Vehicles, etc.) |
| `location_name_groups` | Locations grouped by map name and category |
| `placements` | Per-location placement records (see below) |

#### Placement Record Fields

Each entry in `placements` describes one filled location:

| Field | Description |
|-------|-------------|
| `location` | Location name (e.g. `"mds7st1 - Hi-Potion"`) |
| `location_id` | Numeric location id |
| `map` | Primary field map name (e.g. `"mds7st1"`) |
| `maps` | All field map names where this pickup appears |
| `item_text` | Original in-game item text for this pickup slot |
| `category` | One of `standard`, `materia`, `key_item`, `reward`, `victory` |
| `item` | Name of the item placed here |
| `item_id` | Numeric item id (matches ff7tk `ItemId` enum values for native items) |
| `item_owner` | Player name who will receive this item |
| `item_classification` | Archipelago classification: `progression`, `useful`, or `filler` |

### Gold Saucer Validation

Gold Saucer checks for the following fields before enabling Archipelago mode:

- At least one of `seed_name`, `players`, or `slot_data` at the root level.
- `slot_data.item_name_to_id` or `slot_data.id_to_item_name` present.
- `players` array must be non-empty if present.

The Archipelago FF7 exporter satisfies all three requirements.

### Data Sources

Item IDs and names are derived from the ff7tk `FF7Item::ItemId` enum. Location names and map identifiers are derived
from ff7tk `FF7FieldItemList`. This ensures that item IDs in the JSON directly correspond to the IDs Gold Saucer uses
when patching `flevel.lgp`.

> **Note:** This lightweight client is intended for development. Full Gold Saucer/FF7TK scripting integration (writing
> item IDs into field files using placement data) can be layered on top once the datapackage and settings are finalized.
