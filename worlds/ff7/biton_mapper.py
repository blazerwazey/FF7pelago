"""Core BITON-flag mapper for FF7 field scripts.

Used by both ``tools/map_biton_flags.py`` (offline CLI) and ``FF7Client.py``
(in-process, triggered via /mapbitons command or auto-run on Connected).

Run this against the **randomized** flevel.lgp (Gold Saucer output), not
vanilla, so that moved key-item BITONs are captured correctly.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

try:
    import winreg as _winreg
except ImportError:
    _winreg = None  # type: ignore[assignment]
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# ── Field script opcodes ──────────────────────────────────────────────────────
STITM_OPCODE = 0x58
SMTRA_OPCODE = 0x5B
BITON_OPCODE = 0x82

BITON_WINDOW      = 250   # search radius around STITM/SMTRA
KEY_ITEM_ADDR_MIN = 0x40  # savemap addresses used exclusively for key items
KEY_ITEM_ADDR_MAX = 0x46  # Gold Saucer neutered BITONs write to bank 3 / 0xFE


# ── Auto-detection ────────────────────────────────────────────────────────────

_STEAM_SUBDIR = "steamapps/common/FINAL FANTASY VII"
_COMMON_PATHS = [
    Path("C:/Program Files (x86)/Steam") / _STEAM_SUBDIR,
    Path("C:/Program Files/Steam") / _STEAM_SUBDIR,
    Path("D:/Steam") / _STEAM_SUBDIR,
    Path("D:/SteamLibrary") / _STEAM_SUBDIR,
    Path("C:/Games/Final Fantasy VII"),
    Path("C:/FF7"),
]


def _steam_library_paths() -> List[Path]:
    """Parse Steam's libraryfolders.vdf for extra library roots."""
    try:
        vdf = Path("C:/Program Files (x86)/Steam/steamapps/libraryfolders.vdf")
        if not vdf.exists():
            vdf = Path("C:/Program Files/Steam/steamapps/libraryfolders.vdf")
        if not vdf.exists():
            return []
        text = vdf.read_text(encoding="utf-8", errors="ignore")
        paths: List[Path] = []
        for line in text.splitlines():
            line = line.strip()
            if '"path"' in line.lower():
                parts = line.split('"')
                if len(parts) >= 4:
                    paths.append(Path(parts[3]) / _STEAM_SUBDIR)
        return paths
    except Exception:
        return []


def _registry_ff7_path() -> Optional[Path]:
    """Try the classic Square Soft registry key (FF7 1998 / Steam)."""
    if _winreg is None:
        return None
    keys = [
        (_winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Square Soft, Inc\Final Fantasy VII"),
        (_winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Square Soft, Inc\Final Fantasy VII"),
    ]
    for hive, subkey in keys:
        try:
            with _winreg.OpenKey(hive, subkey) as k:
                path, _ = _winreg.QueryValueEx(k, "AppPath")
                p = Path(str(path))
                if p.is_dir():
                    return p
        except Exception:
            pass
    return None


def find_ff7_dir() -> Optional[Path]:
    """Return the FF7 install directory if it can be found automatically."""
    import os
    env = os.environ.get("FF7_DIR")
    if env:
        p = Path(env)
        if _has_flevel(p):
            return p

    reg = _registry_ff7_path()
    if reg and _has_flevel(reg):
        return reg

    candidates = _COMMON_PATHS + _steam_library_paths()
    for p in candidates:
        if _has_flevel(p):
            return p

    return None


def _has_flevel(directory: Path) -> bool:
    return (directory / "data" / "field" / "flevel.lgp").exists()


# ── LZS Decompression ─────────────────────────────────────────────────────────

def lzs_decompress(data: bytes) -> bytes:
    """Decompress FF7 LZS data that begins with a 4-byte decompressed-size header."""
    if len(data) < 4:
        return b""
    decompressed_size = struct.unpack_from("<I", data, 0)[0]
    if decompressed_size == 0 or decompressed_size > 4 * 1024 * 1024:
        return b""

    N       = 4096
    ring    = bytearray(N)      # FF7 LZS ring buffer is zero-initialized
    ring_pos = N - 18
    out     = bytearray()
    pos     = 4
    dlen    = len(data)

    while len(out) < decompressed_size and pos < dlen:
        flags = data[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= decompressed_size or pos >= dlen:
                break
            if flags & (1 << bit):
                c = data[pos]; pos += 1
                out.append(c)
                ring[ring_pos] = c
                ring_pos = (ring_pos + 1) & 0xFFF
            else:
                if pos + 1 >= dlen:
                    break
                b1 = data[pos]; b2 = data[pos + 1]; pos += 2
                back   = b1 | ((b2 & 0x0F) << 8)
                length = (b2 >> 4) + 2
                for j in range(length):
                    if len(out) >= decompressed_size:
                        break
                    c = ring[(back + j) & 0xFFF]
                    out.append(c)
                    ring[ring_pos] = c
                    ring_pos = (ring_pos + 1) & 0xFFF
    return bytes(out)


# ── LGP Archive Reader ────────────────────────────────────────────────────────

def read_lgp(lgp_path: Path) -> Dict[str, bytes]:
    """Extract all files from a SQUARESOFT LGP archive.

    Returns ``{lowercase_filename: lzs_payload_bytes}``.
    """
    data  = lgp_path.read_bytes()
    if len(data) < 16:
        return {}

    pos   = 12
    count = struct.unpack_from("<I", data, pos)[0]
    pos  += 4

    entries: List[Tuple[str, int]] = []
    for _ in range(count):
        if pos + 27 > len(data):
            break
        name   = data[pos: pos + 20].rstrip(b"\x00").decode("ascii", errors="replace").lower()
        offset = struct.unpack_from("<I", data, pos + 20)[0]
        pos   += 27           # 20-byte name + 4-byte offset + 2-byte conflict + 1-byte unknown
        entries.append((name, offset))

    result: Dict[str, bytes] = {}
    for name, offset in entries:
        # Each LGP entry has a 24-byte file header before the content:
        #   char[20] filename  (duplicate of TOC name)
        #   uint32   file_size (bytes of content that follow)
        # The content itself begins with a 4-byte LZS decompressed-size header.
        if offset + 24 > len(data):
            continue
        file_size = struct.unpack_from("<I", data, offset + 20)[0]
        if file_size == 0 or offset + 24 + file_size > len(data):
            continue
        result[name] = data[offset + 24: offset + 24 + file_size]
    return result


# ── Field Script Section Extraction ──────────────────────────────────────────

def _get_script_section(
    compressed: bytes,
    start_hint: Optional[int] = None,
    end_hint: Optional[int] = None,
) -> bytes:
    """Decompress and return script-section (section 0) bytes.

    *start_hint* / *end_hint* are offsets from *sec0_data* as reported by
    Gold Saucer's debug file.  When supplied they override the auto-computed
    formula, eliminating false STITM detections from entity-table bytes.
    """
    dec = lzs_decompress(compressed)
    n   = len(dec)
    # Field file layout (after LZS decompress):
    #   [0:2]   blank (00 00)
    #   [2:6]   section count (uint32 LE, = 9)
    #   [6:42]  9 × uint32 section start offsets  (relative to section_base)
    #   [42:]   section data
    SECTION_BASE = 6 + 9 * 4  # = 42
    if n < SECTION_BASE:
        return b""
    sec0off = struct.unpack_from("<I", dec, 6)[0]  # section 0 relative offset
    sec0_abs = SECTION_BASE + sec0off              # absolute position in dec
    sec0_data = sec0_abs + 4                       # skip 4-byte section size prefix
    if sec0_data + 6 > n:
        return b""
    if start_hint is not None and end_hint is not None:
        s = sec0_data + start_hint
        e = sec0_data + end_hint
        if s < e <= n:
            return dec[s:e]

    sec_size  = struct.unpack_from("<I", dec, sec0_abs)[0]
    pos_texts = struct.unpack_from("<H", dec, sec0_data + 4)[0]
    nb_ent    = dec[sec0_data + 2] if sec0_data + 2 < n else 0
    script_start = sec0_data + 32 + 72 * nb_ent
    if pos_texts > 0:
        # Normal case: section has dialog text; script occupies bytes up to pos_texts.
        script_end = sec0_data + pos_texts
    elif sec_size > 0:
        # Section has no dialog text; the entire section body is script opcodes.
        script_end = sec0_data + sec_size
    else:
        return b""
    if script_start >= script_end or script_end > n:
        script_start = sec0_data + 6  # fall back to just past the sub-header
        if script_start >= script_end:
            return b""
    return dec[script_start:script_end]


# ── STITM / SMTRA + adjacent-BITON scan (regular items) ──────────────────────

def _find_nearby_biton(
    script: bytes, pos: int, n: int,
    used_positions: Optional[set] = None,
) -> Tuple[Optional[int], Optional[Tuple[int, int, int]]]:
    """Return ``(biton_pos, (banks_byte, address, bit))`` or ``(None, None)``."""
    for delta in range(1, BITON_WINDOW + 1):
        for candidate in (pos + delta, pos - delta):
            if used_positions and candidate in used_positions:
                continue
            if 0 <= candidate and candidate + 3 < n and script[candidate] == BITON_OPCODE:
                banks_byte = script[candidate + 1]
                address    = script[candidate + 2]
                bit        = script[candidate + 3]
                dest_bank  = (banks_byte >> 4) & 0xF
                if (banks_byte != 0 and dest_bank <= 3 and bit <= 7
                        and address != 0xFE
                        and not (address == 0 and bit == 0)
                        and not (KEY_ITEM_ADDR_MIN <= address <= KEY_ITEM_ADDR_MAX)):
                    return (candidate, (banks_byte, address, bit))
    return (None, None)


def scan_field_for_pickups(
    compressed: bytes,
    start_hint: Optional[int] = None,
    end_hint: Optional[int] = None,
) -> List[Optional[Tuple[int, int, int]]]:
    """One entry per STITM/SMTRA in script order; None when no adjacent BITON."""
    script = _get_script_section(compressed, start_hint, end_hint)
    sn     = len(script)
    if sn == 0:
        return []
    results: List[Optional[Tuple[int, int, int]]] = []
    used: set = set()  # BITON positions already assigned to an earlier pickup
    i = 0
    while i < sn:
        op = script[i]
        if op == STITM_OPCODE and i + 4 < sn and script[i + 1] == 0x00:
            biton_pos, biton = _find_nearby_biton(script, i, sn, used)
            results.append(biton)
            if biton_pos is not None:
                used.add(biton_pos)
            i += 5; continue
        if op == SMTRA_OPCODE and i + 4 < sn:
            biton_pos, biton = _find_nearby_biton(script, i, sn, used)
            results.append(biton)
            if biton_pos is not None:
                used.add(biton_pos)
            i += 5; continue
        i += 1
    return results


# ── Standalone key-item BITON scan (addresses 0x40–0x46) ─────────────────────

def scan_field_for_key_item_bitons(compressed: bytes) -> List[Tuple[int, int, int]]:
    """All BITON opcodes at key-item addresses (banks 1–2, addr 0x40–0x46)."""
    script = _get_script_section(compressed)
    sn     = len(script)
    results: List[Tuple[int, int, int]] = []
    i = 0
    while i < sn:
        if script[i] == BITON_OPCODE and i + 3 < sn:
            banks_byte = script[i + 1]
            address    = script[i + 2]
            bit        = script[i + 3]
            dest_bank  = (banks_byte >> 4) & 0xF
            if (1 <= dest_bank <= 2
                    and KEY_ITEM_ADDR_MIN <= address <= KEY_ITEM_ADDR_MAX
                    and bit <= 7):
                results.append((banks_byte, address, bit))
        i += 1
    return results


# ── High-level in-memory mapping ──────────────────────────────────────────────

def build_key_item_biton_map(lgp_path: Path, locations: List[dict]) -> Dict[str, Tuple[int, int, int]]:
    """Scan *lgp_path* and return ``{item_text: (bank, address, bit)}`` for key items.

    Each entry maps the vanilla *item_text* of a key-item location (e.g.
    ``"KeyItem: Keycard 60"``) to the raw BITON coords found in that map's
    script section.  Run against **vanilla** flevel.lgp once; commit result
    as ``data/key_item_biton_map.json``.
    """
    lgp_files = read_lgp(lgp_path)

    keyitem_by_map: Dict[str, List[dict]] = defaultdict(list)
    for loc in locations:
        if loc.get("category") == "key_item":
            keyitem_by_map[loc["map"].lower()].append(loc)

    result: Dict[str, Tuple[int, int, int]] = {}

    for map_name, locs in keyitem_by_map.items():
        fd = lgp_files.get(map_name)
        if fd is None:
            continue
        ki_bitons = scan_field_for_key_item_bitons(fd)
        for i, loc in enumerate(locs):
            if i < len(ki_bitons):
                banks_byte, address, bit = ki_bitons[i]
                bank = (banks_byte >> 4) & 0xF
                result[loc["item_text"]] = (bank, address, bit)

    return result


def build_vanilla_biton_maps(
    lgp_path: Path, locations: List[dict]
) -> Tuple[Dict[int, Tuple[int, int, int]], Dict[str, Tuple[int, int, int]]]:
    """Return ``(location_biton_map, key_item_biton_map)`` from a single LGP scan.

    *location_biton_map*: ``{location_code: (bank, address, bit)}`` for standard
    STITM/SMTRA locations.

    *key_item_biton_map*: ``{item_text: (bank, address, bit)}`` for key items.

    Run against **vanilla** flevel.lgp; commit both outputs to the data directory.
    """
    lgp_files = read_lgp(lgp_path)

    regular_by_map: Dict[str, List[dict]] = defaultdict(list)
    keyitem_by_map: Dict[str, List[dict]] = defaultdict(list)
    for loc in locations:
        if loc.get("category") == "victory":
            continue
        m = loc["map"].lower()
        if loc.get("category") == "key_item":
            keyitem_by_map[m].append(loc)
        else:
            regular_by_map[m].append(loc)

    loc_map: Dict[int, Tuple[int, int, int]] = {}
    for map_name, locs in regular_by_map.items():
        fd = lgp_files.get(map_name)
        if fd is None:
            continue
        flags = scan_field_for_pickups(fd)
        for i, loc in enumerate(locs):
            if i < len(flags) and flags[i] is not None:
                banks_byte, address, bit = flags[i]
                loc_map[loc["code"]] = ((banks_byte >> 4) & 0xF, address, bit)

    ki_map: Dict[str, Tuple[int, int, int]] = {}
    for map_name, locs in keyitem_by_map.items():
        fd = lgp_files.get(map_name)
        if fd is None:
            continue
        ki_bitons = scan_field_for_key_item_bitons(fd)
        for i, loc in enumerate(locs):
            if i < len(ki_bitons):
                banks_byte, address, bit = ki_bitons[i]
                ki_map[loc["item_text"]] = ((banks_byte >> 4) & 0xF, address, bit)

    return loc_map, ki_map


# ── Gold Saucer debug-file parser ────────────────────────────────────────────

_RE_GS_FIELD    = re.compile(r'^(\w+): script section bytes (\d+)\.\.(\d+)')
_RE_GS_PLACED   = re.compile(
    r"^\s+PLACED: '(.+?)' -> (\w+) \(sphere \d+\) @(\d+)\s+\[src: (\w+) @(\d+)\]")
_RE_GS_REPLACED = re.compile(
    r'^\s+KEY_ITEM REPLACED\s+@(\d+) new:\s+((?:[0-9a-fA-F]{2} )+)')
_RE_GS_BITON    = re.compile(
    r'^\s+KEY_ITEM BITON @(\d+) -> (.+?)(?:\s+\(existing BITON host\))?\s*$')


def parse_gs_debug(
    debug_path: Path,
) -> Tuple[
    Dict[str, Tuple[int, int]],   # script_bounds
    List[dict],                   # placements
]:
    """Parse a Gold Saucer *field_randomization_debug.txt*.

    Returns
    -------
    script_bounds
        ``{field_name: (start_off, end_off)}`` – offsets from *sec0_data*.
    placements
        List of dicts::

            {item_name, dest_field, dest_offset, src_field, src_offset,
             bank, address, bit}   # bank/address/bit == -1 when unknown
    """
    script_bounds: Dict[str, Tuple[int, int]] = {}
    placements: List[dict] = []

    # dest_offset -> (banks_byte, address, bit) – filled by REPLACED lines
    _pending: Dict[int, Tuple[int, int, int]] = {}
    # (dest_field, dest_offset) -> placement record (primary key)
    _placed_idx: Dict[Tuple[str, int], dict] = {}
    # (dest_offset, item_name) -> [record, ...] (fallback when dest_field unknown)
    _placed_by_off_name: Dict[Tuple[int, str], List[dict]] = defaultdict(list)

    # We track the "active" destination field from NOP lines so REPLACED/BITON
    # lines that appear before the field header can be attributed correctly.
    _re_nop = re.compile(r'^\s+NOP original BITON in (\w+) @(\d+)')
    _re_new = re.compile(r'^\s+KEY_ITEM REPLACING @(\d+)')
    _active_dest_field: Optional[str] = None
    _current_field:     Optional[str] = None

    with open(debug_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")

            m = _RE_GS_FIELD.match(line)
            if m:
                _current_field = m.group(1)
                script_bounds[_current_field] = (int(m.group(2)), int(m.group(3)))
                _active_dest_field = None
                continue

            m = _re_nop.match(line)
            if m:
                _active_dest_field = m.group(1)
                continue

            m = _RE_GS_PLACED.match(line)
            if m:
                item_name   = m.group(1)
                dest_field  = m.group(2)
                dest_offset = int(m.group(3))
                src_field   = m.group(4)
                src_offset  = int(m.group(5))
                rec = {
                    "item_name":   item_name,
                    "dest_field":  dest_field,
                    "dest_offset": dest_offset,
                    "src_field":   src_field,
                    "src_offset":  src_offset,
                    "bank": -1, "address": -1, "bit": -1,
                }
                _placed_idx[(dest_field, dest_offset)] = rec
                _placed_by_off_name[(dest_offset, item_name)].append(rec)
                placements.append(rec)
                continue

            m = _RE_GS_REPLACED.match(line)
            if m:
                offset = int(m.group(1))
                raw = bytes(int(x, 16) for x in m.group(2).strip().split())
                if len(raw) >= 4 and raw[0] == BITON_OPCODE:
                    _pending[offset] = (raw[1], raw[2], raw[3])
                continue

            m = _RE_GS_BITON.match(line)
            if m:
                offset    = int(m.group(1))
                item_name = m.group(2).strip()
                if offset in _pending:
                    banks_byte, address, bit = _pending.pop(offset)
                    bank = (banks_byte >> 4) & 0xF
                    # Try primary key (active dest field) first, then fallback
                    dest_field = _active_dest_field or _current_field
                    matched_rec = _placed_idx.get((dest_field, offset))
                    if matched_rec is None:
                        candidates = _placed_by_off_name.get((offset, item_name), [])
                        matched_rec = candidates[0] if candidates else None
                    if matched_rec is not None:
                        matched_rec["bank"]    = bank
                        matched_rec["address"] = address
                        matched_rec["bit"]     = bit
                continue

    return script_bounds, placements


def build_biton_map_from_debug(
    debug_path: Path,
    lgp_path: Path,
    locations: List[dict],
) -> Dict[int, Tuple[int, int, int]]:
    """Combine Gold Saucer debug-file data with a randomized LGP scan.

    For key items whose BITON was explicitly written by Gold Saucer the debug
    file is authoritative (no scanning needed).  For all other locations the
    scan is run with *corrected script bounds* so false STITM detections from
    entity-table bytes are eliminated.

    Returns ``{location_code: (bank, address, bit)}``.
    """
    script_bounds, placements = parse_gs_debug(debug_path)
    lgp_files = read_lgp(lgp_path)

    result: Dict[int, Tuple[int, int, int]] = {}

    def _norm(s: str) -> str:
        """Normalise item name: strip 'KeyItem: ', collapse ():- to spaces, lowercase."""
        s = s.lower().removeprefix("keyitem: ").strip()
        for c in "():":
            s = s.replace(c, " ")
        return " ".join(s.split())

    # ── Part 1: key-item BITONs from debug file ──────────────────────────────
    # Build per-src_field lookup: src_field -> sorted [rec]
    from_src: Dict[str, List[dict]] = defaultdict(list)
    for rec in placements:
        if rec["bank"] != -1:
            from_src[rec["src_field"]].append(rec)
    for lst in from_src.values():
        lst.sort(key=lambda r: r["src_offset"])

    # Build per-src_field location lookup: src_field -> [loc, ...] in list order
    locs_by_src: Dict[str, List[dict]] = defaultdict(list)
    for loc in locations:
        if loc.get("category") in ("key_item", "standard", "boss"):
            locs_by_src[loc.get("map", "").lower()].append(loc)

    for src_field, recs in from_src.items():
        key_locs = [
            l for l in locs_by_src.get(src_field, [])
            if any(_norm(r["item_name"]) == _norm(l.get("item_text", "")) for r in recs)
        ]
        # Group recs and matching locs by normalised item_name to handle duplicates
        from collections import Counter as _Ctr
        norm_to_recs: Dict[str, List[dict]] = defaultdict(list)
        for r in recs:
            norm_to_recs[_norm(r["item_name"])].append(r)
        for norm_name, item_recs in norm_to_recs.items():
            item_locs = [l for l in key_locs
                         if _norm(l.get("item_text", "")) == norm_name]
            for rec, loc in zip(item_recs, item_locs):
                result[loc["code"]] = (rec["bank"], rec["address"], rec["bit"])

    # ── Part 2: scan remaining locations with corrected script bounds ─────────
    regular_by_map: Dict[str, List[dict]] = defaultdict(list)
    for loc in locations:
        if loc.get("category") == "victory" or loc["code"] in result:
            continue
        regular_by_map[loc.get("map", "").lower()].append(loc)

    for map_name, locs in regular_by_map.items():
        fd = lgp_files.get(map_name)
        if fd is None:
            continue
        bounds = script_bounds.get(map_name)
        sh, eh = (bounds if bounds else (None, None))
        flags = scan_field_for_pickups(fd, sh, eh)
        for i, loc in enumerate(locs):
            if i < len(flags) and flags[i] is not None:
                banks_byte, address, bit = flags[i]
                result[loc["code"]] = ((banks_byte >> 4) & 0xF, address, bit)

    return result


def build_biton_map(lgp_path: Path, locations: List[dict]) -> Dict[int, Tuple[int, int, int]]:
    """Scan *lgp_path* and return ``{location_code: (bank, address, bit)}``.

    Only locations that have a discoverable BITON are included.
    *locations* is the list loaded from ``locations.json``.
    """
    lgp_files = read_lgp(lgp_path)

    regular_by_map: Dict[str, List[dict]] = defaultdict(list)
    keyitem_by_map: Dict[str, List[dict]] = defaultdict(list)
    for loc in locations:
        if loc.get("category") == "victory":
            continue
        m = loc["map"].lower()
        if loc.get("category") == "key_item":
            keyitem_by_map[m].append(loc)
        else:
            regular_by_map[m].append(loc)

    result: Dict[int, Tuple[int, int, int]] = {}

    for map_name, locs in regular_by_map.items():
        fd = lgp_files.get(map_name)
        if fd is None:
            continue
        flags = scan_field_for_pickups(fd)
        for i, loc in enumerate(locs):
            if i < len(flags) and flags[i] is not None:
                banks_byte, address, bit = flags[i]
                result[loc["code"]] = ((banks_byte >> 4) & 0xF, address, bit)

    for map_name, locs in keyitem_by_map.items():
        fd = lgp_files.get(map_name)
        if fd is None:
            continue
        ki_bitons = scan_field_for_key_item_bitons(fd)
        for i, loc in enumerate(locs):
            if i < len(ki_bitons):
                banks_byte, address, bit = ki_bitons[i]
                result[loc["code"]] = ((banks_byte >> 4) & 0xF, address, bit)

    return result
