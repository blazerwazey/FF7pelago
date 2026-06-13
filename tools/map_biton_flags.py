"""CLI wrapper: scan FF7's flevel.lgp and write BITON flags into locations.json.

Modes of operation
------------------

**Sidecar mode** (recommended for Gold Saucer Archipelago builds):
    Gold Saucer replaces every STITM/SMTRA with a BITON and emits a JSON
    sidecar mapping every Archipelago location code to its savemap flag.
    This mode ingests that file directly — no LGP scan required.

        python tools/map_biton_flags.py --ap-sidecar path/to/archipelago_bitons.json

    Expected sidecar format::

        {"biton_map": {"<location_code>": [bank, addr, bit], ...}}

**Vanilla mode** (one-time repository bootstrap):
    Run against your **unmodified** FF7 installation.  Populates
    ``locations.json`` with static BITON coords and writes
    ``data/key_item_biton_map.json``.

        python tools/map_biton_flags.py --vanilla "C:/Games/Final Fantasy VII"

**Randomized / debug-file mode** (legacy):
    Run against the Gold Saucer output with the debug text file.

        python tools/map_biton_flags.py "C:/Games/Final Fantasy VII" \\
            --debug-file path/to/field_randomization_debug.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Allow running directly from the repo root without installing.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_REPO_ROOT))

from worlds.ff7.biton_mapper import (  # noqa: E402
    build_biton_map,
    build_biton_map_from_debug,
    build_vanilla_biton_maps,
    find_ff7_dir,
)

LOCATIONS_JSON      = _REPO_ROOT / "worlds" / "ff7" / "data" / "locations.json"
KEY_ITEM_BITON_JSON = _REPO_ROOT / "worlds" / "ff7" / "data" / "key_item_biton_map.json"


# ── Vanilla bootstrap ─────────────────────────────────────────────────────────

def run_vanilla(lgp_path: Path) -> None:
    """Scan vanilla flevel.lgp; write locations.json + key_item_biton_map.json."""
    locations = json.loads(LOCATIONS_JSON.read_text(encoding="utf-8"))
    print(f"Reading  : {lgp_path}")
    print(f"Locations: {len(locations)}")

    loc_map, ki_map = build_vanilla_biton_maps(lgp_path, locations)

    # Remove BITON tuples shared by multiple locations (shared flags track areas,
    # not individual items, and would incorrectly mark multiple locations collected)
    from collections import Counter
    biton_counts = Counter(loc_map.values())
    shared = {b for b, n in biton_counts.items() if n > 1}
    if shared:
        loc_map = {code: b for code, b in loc_map.items() if b not in shared}
        print(f"Removed {sum(biton_counts[b] for b in shared)} locations "
              f"with {len(shared)} shared BITON(s)")

    matched = 0
    for loc in locations:
        code = loc.get("code")
        if code in loc_map:
            bank, address, bit = loc_map[code]
            loc["bank"]    = bank
            loc["address"] = address
            loc["bit"]     = bit
            matched += 1
        else:
            loc["bank"]    = -1
            loc["address"] = -1
            loc["bit"]     = -1

    total = sum(1 for l in locations if l.get("category") != "victory")
    print(f"Standard locations matched : {matched}/{total}")
    print(f"Key items found            : {len(ki_map)}")

    LOCATIONS_JSON.write_text(
        json.dumps(locations, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Written  : {LOCATIONS_JSON}")

    ki_serializable = {
        name: {"bank": bank, "address": address, "bit": bit}
        for name, (bank, address, bit) in ki_map.items()
    }
    KEY_ITEM_BITON_JSON.write_text(
        json.dumps(ki_serializable, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Written  : {KEY_ITEM_BITON_JSON}")


# ── Sidecar mode (Gold Saucer Archipelago JSON) ──────────────────────────────

def run_sidecar(sidecar_path: Path) -> None:
    """Ingest a Gold Saucer archipelago_bitons.json sidecar into locations.json.

    Sidecar format::

        {"biton_map": {"<location_code>": [bank, addr, bit], ...}}

    Location codes are integer strings (str(loc["code"])).
    """
    locations = json.loads(LOCATIONS_JSON.read_text(encoding="utf-8"))
    print(f"Sidecar  : {sidecar_path}")
    print(f"Locations: {len(locations)}")

    raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    biton_map = raw.get("biton_map", {})

    matched = 0
    for loc in locations:
        code = loc.get("code")
        entry = biton_map.get(str(code))
        if entry and len(entry) == 3:
            loc["bank"], loc["address"], loc["bit"] = entry
            matched += 1
        else:
            loc["bank"]    = -1
            loc["address"] = -1
            loc["bit"]     = -1

    total = sum(1 for l in locations if l.get("category") != "victory")
    print(f"Matched BITON flags : {matched}/{total} locations")

    LOCATIONS_JSON.write_text(
        json.dumps(locations, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Written  : {LOCATIONS_JSON}")


# ── Sidecar-match mode (Gold Saucer field-based sidecar) ─────────────────────

def _norm_name(s: str) -> str:
    """Lowercase, strip punctuation variants for loose matching."""
    return s.lower().replace("-", " ").replace("'", "").replace("'", "").strip()


def run_sidecar_match(sidecar_path: Path) -> None:
    """Match a Gold Saucer field-based sidecar into locations.json.

    Sidecar format (archipelago_bitons.json from Gold Saucer)::

        {
          "biton_map": [
            {"field": "mds7st1", "offset": 2908, "is_materia": false,
             "original_item_id": 32, "original_name": "Hi-Potion",
             "bank": 1, "address": 128, "bit": 0},
            ...
          ]
        }

    Matching is done by (field_name, original_name) against locations.json
    ``map`` and ``item_text`` fields.  When a field has multiple entries with
    the same item name the one whose script offset appears earliest is used
    for the first unmatched location with that key, etc. (FIFO).
    """
    locations = json.loads(LOCATIONS_JSON.read_text(encoding="utf-8"))
    raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    biton_entries = raw.get("biton_map", [])

    print(f"Sidecar  : {sidecar_path}")
    print(f"Locations: {len(locations)}")
    print(f"Entries  : {len(biton_entries)}")

    # Build lookup: (norm_field, norm_name) -> list of [bank, addr, bit] ordered
    # by offset (sidecar is written in field-file scan order)
    from collections import defaultdict
    sidecar_lookup: dict[tuple[str, str], list[list[int]]] = defaultdict(list)
    for e in biton_entries:
        key = (_norm_name(e.get("field", "")), _norm_name(e.get("original_name", "")))
        sidecar_lookup[key].append([e["bank"], e["address"], e["bit"]])

    # Track consumed entries (pop from front for repeated keys)
    sidecar_iters = {k: iter(v) for k, v in sidecar_lookup.items()}

    matched = 0
    for loc in locations:
        field = _norm_name(loc.get("map", ""))
        name  = _norm_name(loc.get("item_text", ""))
        key   = (field, name)
        it    = sidecar_iters.get(key)
        entry = next(it, None) if it else None
        if entry:
            loc["bank"], loc["address"], loc["bit"] = entry
            matched += 1
        else:
            loc["bank"]    = -1
            loc["address"] = -1
            loc["bit"]     = -1

    total = sum(1 for l in locations if l.get("category") != "victory")
    print(f"Matched BITON flags : {matched}/{total} locations")

    LOCATIONS_JSON.write_text(
        json.dumps(locations, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Written  : {LOCATIONS_JSON}")


# ── Randomized (legacy) mode ──────────────────────────────────────────────────

def run_randomized(lgp_path: Path, debug_file: Optional[Path] = None) -> None:
    """Scan randomized flevel.lgp; write bank/address/bit into locations.json."""
    locations = json.loads(LOCATIONS_JSON.read_text(encoding="utf-8"))
    print(f"Reading  : {lgp_path}")
    if debug_file:
        print(f"Debug    : {debug_file}")
    print(f"Locations: {len(locations)}")

    if debug_file and debug_file.exists():
        biton_map = build_biton_map_from_debug(debug_file, lgp_path, locations)
    else:
        biton_map = build_biton_map(lgp_path, locations)

    matched = 0
    for loc in locations:
        code = loc.get("code")
        if code in biton_map:
            bank, address, bit = biton_map[code]
            loc["bank"]    = bank
            loc["address"] = address
            loc["bit"]     = bit
            matched += 1
        else:
            loc["bank"]    = -1
            loc["address"] = -1
            loc["bit"]     = -1

    total = sum(1 for l in locations if l.get("category") != "victory")
    print(f"Matched BITON flags : {matched}/{total} locations")

    LOCATIONS_JSON.write_text(
        json.dumps(locations, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Written  : {LOCATIONS_JSON}")


# ── Entry Point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan FF7 flevel.lgp and write BITON flags into locations.json."
    )
    parser.add_argument(
        "ff7_dir", nargs="?", default=None,
        help="FF7 installation directory (auto-detected if omitted)"
    )
    parser.add_argument(
        "--vanilla", action="store_true",
        help="Scan vanilla (unmodified) LGP and also write key_item_biton_map.json"
    )
    parser.add_argument(
        "--debug-file", metavar="PATH",
        help="Path to Gold Saucer's field_randomization_debug.txt for authoritative "
             "key-item BITONs and corrected script bounds"
    )
    parser.add_argument(
        "--ap-sidecar", metavar="PATH",
        help="Sidecar keyed by location code: {biton_map: {code: [bank,addr,bit]}}. "
             "No LGP scan performed."
    )
    parser.add_argument(
        "--ap-sidecar-match", metavar="PATH",
        help="Gold Saucer archipelago_bitons.json (array format keyed by field+name). "
             "Matches entries to locations by (field, original_name). "
             "No LGP scan performed."
    )
    args = parser.parse_args()

    # ── Sidecar modes: no LGP needed ─────────────────────────────────────────
    if args.ap_sidecar:
        sidecar = Path(args.ap_sidecar)
        if not sidecar.exists():
            print(f"ERROR: sidecar not found at {sidecar}")
            sys.exit(1)
        run_sidecar(sidecar)
        return

    if args.ap_sidecar_match:
        sidecar = Path(args.ap_sidecar_match)
        if not sidecar.exists():
            print(f"ERROR: sidecar not found at {sidecar}")
            sys.exit(1)
        run_sidecar_match(sidecar)
        return

    # ── All other modes need flevel.lgp ──────────────────────────────────────
    ff7_dir: Optional[Path] = None
    if args.ff7_dir:
        ff7_dir = Path(args.ff7_dir)
    else:
        ff7_dir = find_ff7_dir()
        if ff7_dir:
            print(f"Auto-detected FF7 dir: {ff7_dir}")

    if ff7_dir is None:
        parser.print_help()
        sys.exit(1)

    lgp_path = ff7_dir / "data" / "field" / "flevel.lgp"
    if not lgp_path.exists():
        print(f"ERROR: flevel.lgp not found at {lgp_path}")
        sys.exit(1)

    debug_file = Path(args.debug_file) if args.debug_file else None
    if args.vanilla:
        run_vanilla(lgp_path)
    else:
        run_randomized(lgp_path, debug_file)


if __name__ == "__main__":
    main()
