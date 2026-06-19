"""Validate locations.json against the authoritative FF7 field-pickup list.

Gold Saucer matches an Archipelago placement to a physical field pickup by
``(map, item_text)`` (see FieldPickupRandomizer_ff7tk.cpp::loadApJson). So:

  * If two location records share the same ``(map, item_text)`` beyond the number
    of real pickups that exist for it, they COLLIDE on one pickup — at most one
    can be placed/detected; the rest are dead checks.  -> ERROR
  * A field-pickup-category location whose ``(map, item_text)`` is unknown to
    ff7tk either uses a different (non-field-list) placement mechanism or is a
    typo.  -> WARN (informational; can't be auto-classified).

Authoritative data lives in ``data/field_pickups.json`` (generated from ff7tk
``FF7FieldItemList.h``: ``{map: {text: pickup_count}}``).

Run standalone:  ``python -m worlds.ff7.validate_locations``  (or run the file).
Importable: ``validate()`` returns ``(errors, warnings)`` lists of strings.
This is intentionally NOT wired as a hard gen-time failure yet — the dataset
still contains un-triaged non-field-list entries that would warn.  Once those
are reconciled, ``errors`` can be promoted to a generate_early assertion.
"""
from __future__ import annotations

from collections import Counter
from typing import Dict, List, Tuple

from ._resources import load_json

# Categories whose locations are expected to be FF7FieldItemList pickups.
_FIELD_PICKUP_CATEGORIES = frozenset({"standard", "materia", "reward"})


def _load(name: str):
    # Zip-safe: works whether ff7 is a folder world or a packaged .apworld.
    return load_json(f"data/{name}")


def validate(
    locations: List[dict] | None = None,
    pickups: Dict[str, Dict[str, int]] | None = None,
) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings). errors = real (map,item_text) over-subscriptions."""
    if locations is None:
        locations = _load("locations.json")
    if pickups is None:
        pickups = _load("field_pickups.json")

    # lower-cased pickup counts for case-insensitive matching (matches Gold Saucer)
    auth: Dict[Tuple[str, str], int] = {}
    for m, texts in pickups.items():
        for t, c in texts.items():
            auth[(m.lower(), t.lower())] = c

    # count AP location records per (map, item_text), field-pickup categories only
    ap: Counter = Counter()
    codes_by_key: Dict[Tuple[str, str], List[int]] = {}
    for loc in locations:
        if loc.get("category", "standard") not in _FIELD_PICKUP_CATEGORIES:
            continue
        key = (loc["map"].lower(), loc["item_text"].lower())
        ap[key] += 1
        codes_by_key.setdefault(key, []).append(loc["code"])

    errors: List[str] = []
    warnings: List[str] = []
    for key, ap_count in sorted(ap.items()):
        ff7_count = auth.get(key, 0)
        allowed = max(ff7_count, 1)  # non-field-list pairs may legitimately appear once
        if ap_count > allowed:
            errors.append(
                f"OVER-SUBSCRIBED {key[0]}/{key[1]!r}: {ap_count} location records "
                f"but only {ff7_count} ff7tk pickup(s) -> {ap_count - allowed} dead/"
                f"colliding check(s). codes={codes_by_key[key]}"
            )
        elif ff7_count == 0:
            warnings.append(
                f"NOT A FIELD PICKUP {key[0]}/{key[1]!r} (code {codes_by_key[key][0]}): "
                f"unknown to ff7tk - relies on another placement mechanism or is a typo."
            )
    return errors, warnings


def main() -> int:
    errors, warnings = validate()
    print(f"FF7 location validation: {len(errors)} error(s), {len(warnings)} warning(s)\n")
    if errors:
        print("== ERRORS (colliding / dead checks) ==")
        for e in errors:
            print("  " + e)
        print()
    if warnings:
        print(f"== WARNINGS ({len(warnings)} not-field-pickup; review for typos) ==")
        for w in warnings:
            print("  " + w)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
