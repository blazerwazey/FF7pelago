"""Parse FF7TK headers to build Archipelago-ready FF7 data."""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

FIELD_HEADER = Path(r"K:/Gold Saucer/ff7tk-1.3.1.3/include/ff7tk/data/FF7FieldItemList.h")
ITEM_HEADER = Path(r"K:/Gold Saucer/ff7tk-1.3.1.3/include/ff7tk/data/FF7Item.h")
OUTPUT_DIR = Path("worlds/ff7/data")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
ITEMS_OUTPUT = OUTPUT_DIR / "items.json"
LOCATIONS_OUTPUT = OUTPUT_DIR / "locations.json"

def canonicalize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name).lower()

def parse_item_header(path: Path) -> Dict[str, str]:
    text = path.read_text(encoding="utf-8")
    enum_block = re.search(r"enum ItemId \{([^}]+)\}", text, re.MULTILINE | re.DOTALL)
    if not enum_block:
        raise ValueError("Unable to locate ItemId enum in FF7Item.h")
    items: Dict[str, str] = {}
    for enum_name in re.findall(r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*0x[0-9A-Fa-f]+", enum_block.group(1)):
        display = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", enum_name)
        display = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", display)
        display = re.sub(r"(?<=\D)(\d)", r" \1", display)
        display = display.replace("_", " ").strip()
        items[canonicalize(display)] = display
    return items

def parse_text(segment: str) -> str:
    segment = segment.strip()
    replacements = re.findall(r'QT_TR_NOOP\("([^\"]+)"\)', segment)
    base_match = re.search(r'StringLiteral\("([^\"]*)"\)', segment)
    if replacements:
        if base_match and "%" in base_match.group(1):
            result = base_match.group(1)
            for idx, repl in enumerate(replacements, start=1):
                result = result.replace(f"%{idx}", repl)
            return result.replace("\\n", " ").strip()
        return replacements[0].replace("\\n", " ").strip()
    if base_match:
        return base_match.group(1).replace("\\n", " ").strip()
    literal_only = re.findall(r'"([^\"]+)"', segment)
    if literal_only:
        return literal_only[-1]
    literal_only = re.findall(r'"([^\"]+)"', segment)
    if literal_only:
        return literal_only[-1]
    return segment

def parse_maps(segment: str) -> List[str]:
    return re.findall(r'StringLiteral\("([^\"]+)"\)', segment)

def parse_field_header(path: Path) -> List[Dict[str, List[str]]]:
    entries: List[Dict[str, List[str]]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("//")[0].strip()
        if "QT_TR_NOOP" not in line:
            continue
        maps = parse_maps(line)
        text = parse_text(line)
        if maps and text:
            entries.append({"maps": maps, "text": text})
    return entries

PROGRESSION_ITEMS = {
    "keycard 60", "keycard 62", "keycard 65", "keycard 66", "keycard 68",
    "keystone", "key to ancients", "key to sector 5", "basement key", "black materia",
    "tiny bronco", "highwind", "submarine", "snowboard", "gold ticket", "phs",
    "a coupon", "b coupon", "c coupon", "lunar harp", "mythril", "huge materia",
    "glacier map", "letter to wife", "letter to daughter", "key to basement",
    "leviathan scales", "member's card", "pharmacy coupon", "potion to mayor", "keycard 62",
}

USEFUL_ITEMS = {
    "ribbon", "megalixir", "elixir", "turbo ether", "hero drink", "protect ring",
    "choco feather", "enemy skill", "knights of the round", "w summon", "hp absorb",
    "mp absorb", "added effect", "counter", "mime", "quadra magic", "safety bit",
    "all", "elemental", "gil plus", "exp plus", "ultima weapon", "mega all",
    "apocalypse", "minerva band", "ziedrich", "save crystal", "speed source",
}

MATERIA_KEYWORDS = {
    "materia", "summon", "bahamut", "shiva", "ramuh", "knights", "phoenix", "alexander",
    "neo bahamut", "typhon", "choco/mog", "hp absorb", "mp absorb", "elemental",
    "quadra magic", "mime", "counter", "mega all", "all", "gil plus", "exp plus",
}

def classify_item_name(name: str) -> str:
    canon = canonicalize(name)
    if canon in PROGRESSION_ITEMS:
        return "progression"
    if canon in USEFUL_ITEMS:
        return "useful"
    return "filler"

def build_item_dataset(item_name_map: Dict[str, str]) -> List[Dict[str, str]]:
    dataset = []
    base_code = 100_000
    seen: set[str] = set()
    for offset, name in enumerate(sorted(item_name_map.values(), key=canonicalize)):
        if name in seen:
            continue
        seen.add(name)
        dataset.append({
            "name": name,
            "code": base_code + len(dataset),
            "classification": classify_item_name(name),
        })
    return dataset

def classify_location(text: str) -> str:
    if text.startswith("KeyItem:"):
        return "key_item"
    canon = canonicalize(text)
    if any(keyword in canon for keyword in MATERIA_KEYWORDS):
        return "materia"
    if canon in {"ribbon", "megalixir", "elixir", "protectring"}:
        return "reward"
    return "standard"

def build_location_dataset(entries: List[Dict[str, List[str]]]) -> List[Dict[str, str]]:
    locations = []
    used_names: Dict[str, int] = defaultdict(int)
    for entry in entries:
        text = entry["text"]
        maps = entry["maps"]
        base_name = f"{maps[0]} - {text}"
        used_names[base_name] += 1
        if used_names[base_name] > 1:
            name = f"{base_name} ({used_names[base_name]})"
        else:
            name = base_name
        locations.append({
            "name": name,
            "code": 200_000 + len(locations),
            "map": maps[0],
            "maps": maps,
            "item_text": text,
            "category": classify_location(text),
        })

    locations.append({
        "name": "Northern Crater - Defeat Sephiroth",
        "code": 299_999,
        "map": "las4_4",
        "maps": ["las4_4"],
        "item_text": "Victory",
        "category": "victory",
    })
    return locations

def write_datasets(items: List[Dict[str, str]], locations: List[Dict[str, str]]) -> None:
    ITEMS_OUTPUT.write_text(json.dumps(items, indent=2), encoding="utf-8")
    LOCATIONS_OUTPUT.write_text(json.dumps(locations, indent=2), encoding="utf-8")
    print(f"Wrote {len(items)} items and {len(locations)} locations")

def build_datasets():
    item_name_map = parse_item_header(ITEM_HEADER)
    field_entries = parse_field_header(FIELD_HEADER)
    items = build_item_dataset(item_name_map)
    locations = build_location_dataset(field_entries)
    write_datasets(items, locations)

if __name__ == "__main__":
    build_datasets()
