"""Patch all key-item BITONs into locations.json using the canonical savemap
addresses.  Run once after vanilla scanning to fill gaps the scanner missed.

    python tools/patch_key_item_bitons.py
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

LOCATIONS_JSON = (
    Path(__file__).resolve().parent.parent / "worlds/ff7/data/locations.json"
)

# Canonical key-item savemap flags (bank=1, address=0x40-0x46, bit=0-7)
# Derived from FF7 savemap + Gold Saucer getKeyItemName()
KEY_ITEM_BITON: dict[str, tuple[int, int, int]] = {
    "KeyItem: Cotton Dress":              (1, 0x40, 0),
    "KeyItem: Satin Dress":               (1, 0x40, 1),
    "KeyItem: Silk Dress":                (1, 0x40, 2),
    "KeyItem: Wig":                       (1, 0x40, 3),
    "KeyItem: Dyed Wig":                  (1, 0x40, 4),
    "KeyItem: Blonde Wig":                (1, 0x40, 5),
    "KeyItem: Glass Tiara":               (1, 0x40, 6),
    "KeyItem: Ruby Tiara":                (1, 0x40, 7),
    "KeyItem: Diamond Tiara":             (1, 0x41, 0),
    "KeyItem: Cologne":                   (1, 0x41, 1),
    "KeyItem: Flower Cologne":            (1, 0x41, 2),
    "KeyItem: Sexy Cologne":              (1, 0x41, 3),
    "KeyItem: Member's Card":             (1, 0x41, 4),
    "KeyItem: Lingerie":                  (1, 0x41, 5),
    "KeyItem: Mystery Panties":           (1, 0x41, 6),
    "KeyItem: Bikini briefs":             (1, 0x41, 7),
    "KeyItem: Pharmacy Coupon":           (1, 0x42, 0),
    "KeyItem: Disinfectant":              (1, 0x42, 1),
    "KeyItem: Deodorant":                 (1, 0x42, 2),
    "KeyItem: Digestive":                 (1, 0x42, 3),
    "KeyItem: Huge Materia: Fort Condor": (1, 0x42, 4),
    "KeyItem: Huge Materia: Corel":       (1, 0x42, 5),
    "KeyItem: Huge Materia: UnderWater":  (1, 0x42, 6),
    "KeyItem: Huge Materia: Underwater":  (1, 0x42, 6),  # case variant
    "KeyItem: Huge Materia: Rocket":      (1, 0x42, 7),
    "KeyItem: Key to Ancients":           (1, 0x43, 0),
    "KeyItem: Letter to a Daughter":      (1, 0x43, 1),
    "KeyItem: Letter to a Wife":          (1, 0x43, 2),
    "KeyItem: Lunar Harp":                (1, 0x43, 3),
    "KeyItem: Key To Basement":           (1, 0x43, 4),
    "KeyItem: Basement Key":              (1, 0x43, 4),  # alternate name
    "KeyItem: Key To Sector 5":           (1, 0x43, 5),
    "KeyItem: Key to Sector 5":           (1, 0x43, 5),  # case variant
    "KeyItem: Keycard 60":                (1, 0x43, 6),
    "KeyItem: Keycard 62":                (1, 0x43, 7),
    "KeyItem: Keycard 65":                (1, 0x44, 0),
    "KeyItem: Keycard 66":                (1, 0x44, 1),
    "KeyItem: KeyCard 68":                (1, 0x44, 2),
    "KeyItem: Keycard 68":                (1, 0x44, 2),  # case variant
    "KeyItem: PHS":                       (1, 0x45, 0),
    "KeyItem: Gold Ticket":               (1, 0x45, 1),
    "KeyItem: Keystone":                  (1, 0x45, 2),
    "KeyItem: Leviathan Scales":          (1, 0x45, 3),
    "KeyItem: Glacier Map":               (1, 0x45, 4),
    "KeyItem: A Coupon":                  (1, 0x45, 5),
    "KeyItem: B Coupon":                  (1, 0x45, 6),
    "KeyItem: C Coupon":                  (1, 0x45, 7),
    "KeyItem: Black Materia":             (1, 0x46, 0),
    "KeyItem: Mythril":                   (1, 0x46, 1),
    "KeyItem: Snowboard":                 (1, 0x46, 2),
}

# Midgar Parts: 5 separate bits assigned sequentially to matching locations
MIDGAR_PARTS_BITONS: list[tuple[int, int, int]] = [
    (1, 0x44, 3), (1, 0x44, 4), (1, 0x44, 5), (1, 0x44, 6), (1, 0x44, 7),
]


def main() -> None:
    locations = json.loads(LOCATIONS_JSON.read_text(encoding="utf-8"))

    # Track Midgar Parts iterator (sequential assignment)
    midgar_iter = iter(MIDGAR_PARTS_BITONS)
    patched = 0

    for loc in locations:
        if loc.get("bank", -1) >= 0:
            continue  # already matched
        item = loc.get("item_text", "")
        if item == "KeyItem: Midgar parts":
            biton = next(midgar_iter, None)
            if biton:
                loc["bank"], loc["address"], loc["bit"] = biton
                patched += 1
            continue
        biton = KEY_ITEM_BITON.get(item)
        if biton:
            loc["bank"], loc["address"], loc["bit"] = biton
            patched += 1

    LOCATIONS_JSON.write_text(
        json.dumps(locations, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    still_unmatched = sum(
        1 for l in locations
        if l.get("bank", -1) < 0 and l.get("category") != "victory"
    )
    print(f"Patched {patched} key-item locations.")
    print(f"Still unmatched: {still_unmatched} (non-key-item, needs BITON injection)")
    print(f"Written: {LOCATIONS_JSON}")


if __name__ == "__main__":
    main()
