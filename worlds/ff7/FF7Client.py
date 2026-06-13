"""Final Fantasy VII Archipelago client.

Connects to a running FF7_EN.exe process (launched via 7th Heaven or standalone),
reads the Savemap at 0xDBFD38, and sends LocationChecks whenever a field-pickup
BITON flag transitions from 0 to 1.

Savemap base address confirmed from FFNx source (externals_102_us.h):
    ff7_externals.savemap = (savemap *)0xDBFD38

Per-seed BITON mapping
----------------------
BITON coordinates are embedded in the Archipelago JSON file produced at seed
generation (``FF7_<seed>_P<slot>.json``).  Point the client at this file with
the ``/setjson`` command; BITONs are then loaded instantly at connect time with
no flevel.lgp scan required.

For debugging or legacy use, ``/mapbitons`` still triggers a live LGP scan.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from CommonClient import CommonContext, ClientCommandProcessor, logger, server_loop
from NetUtils import ClientStatus
from Utils import user_path

try:
    from Utils import gui_enabled
except ImportError:
    try:
        import kivy  # noqa: F401
        gui_enabled = True
    except ImportError:
        gui_enabled = False

try:
    import pymem
    import pymem.exception
    _PYMEM_AVAILABLE = True
except ImportError:
    _PYMEM_AVAILABLE = False

# ── Savemap constants (FF7_EN.exe v1.02 US) ───────────────────────────────────
SAVEMAP_BASE       = 0xDBFD38
BANK_OFFSET        = 0x0BA4   # Bank 1 base (game-state flags)
POLL_INTERVAL      = 0.2

# Northern Crater gate. The client sets this savemap byte to 1 once every goal
# item has been received; the Gold Saucer field-gate injected into the crater
# entrance reads Var[3][131] and bounces the player out while it is 0.
#   bank 3 base = 0x0CA4, Var[3][131] = 0x0CA4 + 0x83 = 0x0D27
CRATER_LOCK_OFFSET = 0x0D27
CRATER_REQUIRED_ITEMS = frozenset({
    "Highwind",
    "Barret", "Tifa", "Aerith", "Red XIII", "Cait Sith", "Cid",
    "Huge Materia (Fort Condor)", "Huge Materia (Corel)",
    "Huge Materia (Underwater)", "Huge Materia (Rocket)",
})
GAME_MOMENT_OFFSET = 0x0BA4   # uint16 LE "mprogress" Main Progress var; >= 3000 = post-Sephiroth
GAME_MOMENT_GOAL   = 3000
# Ultimate Weapon (Free Roam): his kill flag weapons_killed.bit[0] is set by his
# FINAL BATTLE (no wm0 model-11 function writes it). The chase that whittles his HP
# down to make that battle lethal is set up by the disc-2 intro, which Free Roam
# skips (jumps to moment 1603) — so he never goes down. Once the player has engaged
# him (submarine_flags.bit[3], set on the first ram), we finish him by setting
# weapons_killed.bit[0] (death + the "Defeat Ultimate Weapon" check). The Ancient
# Forest is separate — its entrance only needs the player on foot/chocobo.
WEAPONS_KILLED_OFFSET  = 0x0C1F  # byte: bit0 = killed, bit2 = HP < 20,000
SUBMARINE_FLAGS_OFFSET = 0x0F2A  # byte: bit3 = Ultimate Weapon chase started/engaged
# Boss checks: the only tracked bosses are Ultimate/Emerald/Ruby Weapon, and
# they are detected like any other location via their savemap defeat flag
# (byte 0x0C1F = bank-1 0x7B; Ultimate bit0, Ruby bit3, Emerald bit4) carried in
# the biton_map — no game-moment thresholds. The final Sephiroth fight is the
# victory condition (game moment >= GAME_MOMENT_GOAL), handled below.
# Victory condition: Escape from Midgar when game moment >= 335
MIDGAR_ESCAPE_MOMENT = 335
PROCESS_NAMES      = ("FF7_EN.exe", "ff7.exe", "ff7_en.exe")

# ── Item inventory layout ─────────────────────────────────────────────────────
# Offsets verified from ff7tk FF7Save_Types.h FF7SLOT packed struct:
#   quint16 items[320]    at [0x04FC]
#   materia materias[200] at [0x077C]
# The live savemap at SAVEMAP_BASE mirrors the FF7SLOT struct byte-for-byte.
ITEM_LIST_OFFSET    = 0x04FC  # 320 slots × 2 bytes, format: QQQQQQQXXXXXXXXX
ITEM_SLOT_COUNT     = 320
MATERIA_LIST_OFFSET = 0x077C  # 200 slots × 4 bytes (1-byte id + 3-byte AP)
MATERIA_SLOT_COUNT  = 200
GIL_OFFSET          = 0x0B7C  # quint32 party gil (ff7tk FF7SLOT [0x0B7C])
EMPTY_ITEM_WORD     = 0xFFFF  # id=511 qty=127 ⇒ FF7 uninitialized slot sentinel
EMPTY_MATERIA_BYTE  = 0xFF    # id=0xFF ⇒ empty materia slot

_SETTINGS_FILE = Path(user_path("ff7_client_settings.json"))


def _load_settings() -> dict:
    try:
        if _SETTINGS_FILE.exists():
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_settings(data: dict) -> None:
    try:
        _SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug(f"FF7 settings save failed: {exc}")


# ── Command processor ─────────────────────────────────────────────────────────

def _biton_map_from_placements(placements: List[dict]) -> Dict[int, Tuple[int, int, int]]:
    """Build ``{location_id: (bank, address, bit)}`` from a JSON placements list."""
    result: Dict[int, Tuple[int, int, int]] = {}
    for p in placements:
        bank    = p.get("bank",    -1)
        address = p.get("address", -1)
        bit     = p.get("bit",     -1)
        loc_id  = p.get("location_id")
        if loc_id is not None and bank >= 0 and address >= 0 and bit >= 0:
            result[int(loc_id)] = (bank, address, bit)
    return result


# FF7 get_kernel_text section ids for shop name display (a3=8 = name), confirmed
# from shophook_log.txt: the shop draws EVERY carried-item name (consumable,
# weapon, armor, accessory) via section 4 indexed by the COMPOSITE item id, and
# materia names via section 13 indexed by the materia id.
KTEXT_ITEM    = 4    # consumables/weapons/armor/accessories (composite id)
KTEXT_MATERIA = 13   # materia (materia id)


def _token_section_index(token_type: str, tid: int) -> Tuple[int, int]:
    """Map a shop token to the (kernel text section, index) the FF7 shop grid uses
    to draw its name, so shophook.dll can override it with the AP name."""
    if token_type == "materia":
        return KTEXT_MATERIA, tid
    return KTEXT_ITEM, tid                   # composite id (consumable/weapon/armor/accessory)


def _shops_from_apff7(
    shops: List[dict],
) -> Tuple[Dict[int, int], Dict[int, int], Dict[int, str], Dict[int, str]]:
    """From the .apff7 ``shops`` array build, split by token space:
        (item_token->loc, materia_token->loc, item_token->name, materia_token->name)
    Item-space tokens are composite item ids (consumable/weapon/armor/accessory,
    all detected in the item inventory); materia tokens live in the materia
    inventory. Display name format ``A <Item> @ <Owner>`` (the leading "A " marks
    it as an Archipelago slot), trimmed to fit the shop grid."""
    item_loc: Dict[int, int] = {}
    mat_loc: Dict[int, int] = {}
    item_names: Dict[int, str] = {}
    mat_names: Dict[int, str] = {}
    for s in shops:
        token = s.get("token_id")
        loc   = s.get("location_id")
        if token is None or loc is None:
            continue
        item  = (s.get("item") or "AP Item").strip()
        owner = (s.get("item_owner") or "").strip()
        name  = (f"A {item} @ {owner}" if owner else f"A {item}")[:30]
        if s.get("token_type", "item") == "materia":
            mat_loc[int(token)] = int(loc)
            mat_names[int(token)] = name
        else:
            item_loc[int(token)] = int(loc)
            item_names[int(token)] = name
    return item_loc, mat_loc, item_names, mat_names


def _write_shop_ap_txt(
    exe_dir: Path, item_names: Dict[int, str], materia_names: Dict[int, str]
) -> None:
    """Write shop_ap.txt (read by shophook.dll). Format: ``<section>:<index>=<name>``
    so the hook can override item/weapon/materia names alike. (Legacy ``<id>=name``
    lines are still accepted by the hook and treated as section 4.)"""
    try:
        lines = ["# Auto-generated from the .apff7 shop placements.\n"]
        for tok, name in sorted(item_names.items()):
            sec, idx = _token_section_index("item", tok)
            lines.append(f"{sec}:{idx}={name}\n")
        for tok, name in sorted(materia_names.items()):
            sec, idx = _token_section_index("materia", tok)
            lines.append(f"{sec}:{idx}={name}\n")
        (exe_dir / "shop_ap.txt").write_text("".join(lines), encoding="utf-8")
    except Exception as exc:
        logger.debug(f"shop_ap.txt write failed: {exc}")


_code_to_item_name: Dict[int, str] = {}


def _get_code_to_item_name() -> Dict[int, str]:
    global _code_to_item_name
    if not _code_to_item_name:
        from worlds.ff7.Items import ITEM_TABLE
        _code_to_item_name = {data.code: name for name, data in ITEM_TABLE.items()}
    return _code_to_item_name


def _item_name_to_ff7_id(item_name: str) -> Optional[Tuple[str, int]]:
    """Return ``(category, ff7_id)`` for an Archipelago item name, or None.

    Categories: ``'item'`` (inventory index 0-127), ``'weapon'`` (128-255),
    ``'armor'`` (256-287), ``'accessory'`` (288-319), ``'materia'``,
    ``'key_item'``.

    Requires ``ff7_id`` field in ``items.json``.
    """
    try:
        from worlds.ff7.Items import ITEM_TABLE
        data = ITEM_TABLE.get(item_name)
        if data is None:
            return None
        if data.category == "key_item":
            return None  # handled before this call via KEY_ITEM_FLAGS
        if data.ff7_id is None:
            return None
        ff7_id = data.ff7_id
        if data.category is not None:
            return (data.category, ff7_id)
        # Infer category from ff7_id range (legacy items without explicit category)
        if ff7_id < 128:
            return ("item", ff7_id)
        elif ff7_id < 256:
            return ("weapon", ff7_id)
        elif ff7_id < 288:
            return ("armor", ff7_id)
        elif ff7_id < 320:
            return ("accessory", ff7_id)
    except Exception:
        pass
    return None


class FF7CommandProcessor(ClientCommandProcessor):
    ctx: "FF7Context"

    def _cmd_setjson(self, path: str = "") -> bool:
        """Point the client at the Archipelago FF7 file for this seed.
        Usage: /setjson <path_to_AP_seed_slot_player.apff7>
        The file is produced by Archipelago at generation time and contains
        pre-computed BITON coordinates for every location.
        """
        if not path.strip():
            logger.warning(
                "Usage: /setjson <path>  "
                "(e.g. /setjson C:/AP/AP_MySeed_1_Cloud.apff7)"
            )
            return False

        json_path = Path(path.strip())
        if not json_path.exists():
            logger.warning(f"JSON file not found: {json_path}")
            return False

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            biton_map = _biton_map_from_placements(data.get("placements", []))
            item_loc, mat_loc, item_names, mat_names = \
                _shops_from_apff7(data.get("shops", []))
        except Exception as exc:
            logger.warning(f"Failed to read JSON: {exc}")
            return False

        self.ctx.json_path = json_path
        self.ctx.biton_map = biton_map
        self.ctx.shop_token_to_location = item_loc
        self.ctx.shop_materia_to_location = mat_loc
        self.ctx._shop_apff7_names = item_names
        self.ctx._shop_apff7_materia_names = mat_names
        if item_loc or mat_loc:
            logger.info(f"Shop slots loaded: {len(item_loc) + len(mat_loc)} AP shop check(s).")

        settings = _load_settings()
        settings["json_path"] = str(json_path)
        _save_settings(settings)

        logger.info(
            f"JSON loaded: {json_path.name}  "
            f"({len(biton_map)} locations tracked)"
        )
        return True

    def _cmd_wdump(self, model: str = "") -> bool:
        """[Debug] Dump live world-map state. No arg: player pos + entity list.
        With a model id (e.g. /wdump 5): hex-dump that entity's raw bytes so a
        broken (invisible) vehicle can be diffed against a known-good save.
        """
        import struct
        pm = getattr(self.ctx, "pm", None)
        if pm is None:
            logger.warning("Not attached to FF7 — open the game on the world map first.")
            return False
        want: Optional[int] = None
        if model.strip():
            try:
                want = int(model.strip(), 0)
            except ValueError:
                logger.warning(f"[wdump] bad model id: {model!r}")
                return False
        try:
            px, pz, py, _ = struct.unpack("<4i", pm.read_bytes(0xE04918, 16))
            logger.info(f"[wdump] world player pos (X,Z,Y) = {px}, {pz}, {py}")
            # Globals that may gate vehicle model loading / spawning.
            try:
                moment = pm.read_ushort(SAVEMAP_BASE + GAME_MOMENT_OFFSET)
                choco  = pm.read_uchar(SAVEMAP_BASE + 0x0C22)
                veh    = pm.read_uchar(SAVEMAP_BASE + 0x0C23)
                wprog  = pm.read_int(0xE28CB4)
                locid  = pm.read_ushort(SAVEMAP_BASE + 0x0B96)
                crater = pm.read_uchar(SAVEMAP_BASE + CRATER_LOCK_OFFSET)
                logger.info(
                    f"[wdump] moment={moment} chocobyte=0x{choco:02X} vehbyte=0x{veh:02X} "
                    f"world_progress={wprog} locationid={locid} crater_lock={crater}"
                )
                for nm, off in (("leader", 0x0F5C), ("wchoco", 0x0F64),
                                ("tc", 0x0F6C), ("bh", 0x0F74), ("sub", 0x0F7C)):
                    c1 = pm.read_uint(SAVEMAP_BASE + off)
                    c2 = pm.read_uint(SAVEMAP_BASE + off + 4)
                    logger.info(
                        f"[wdump] {nm}_world=0x{c1:08X}/0x{c2:08X} "
                        f"X={c1 & 0x7FFFF} id={(c1 >> 19) & 0x1F} ang={c1 >> 24} "
                        f"Y={c2 & 0x3FFFF} Z={c2 >> 18}"
                    )
            except Exception as exc:
                logger.info(f"[wdump] globals read failed: {exc}")
            head = pm.read_uint(0xE39AD8)
            logger.info(f"[wdump] entity ptr @0xE39AD8 = 0x{head:08X}; walking next_ptr:")
            seen = set()
            ptr = head
            for _ in range(48):
                if ptr == 0 or ptr < 0x400000 or ptr in seen:
                    break
                seen.add(ptr)
                try:
                    model_id = pm.read_uchar(ptr + 0x50)
                    ex, ez, ey, _ = struct.unpack("<4i", pm.read_bytes(ptr + 0x0C, 16))
                    if want is None:
                        logger.info(f"[wdump]   0x{ptr:08X} model_id={model_id:3d} pos(X,Z,Y)={ex},{ez},{ey}")
                    elif model_id == want:
                        logger.info(f"[wdump] entity 0x{ptr:08X} model_id={model_id} raw bytes:")
                        raw = pm.read_bytes(ptr, 0xC4)
                        for off in range(0, 0xC4, 16):
                            row = raw[off:off + 16]
                            hexs = " ".join(f"{b:02X}" for b in row)
                            logger.info(f"[wdump]   +0x{off:02X}: {hexs}")
                    ptr = pm.read_uint(ptr + 0x00)
                except Exception:
                    break
            logger.info("[wdump] done — share these lines.")
        except Exception as exc:
            logger.warning(f"[wdump] failed: {exc}")
        return True

    def _cmd_setwp(self, value: str = "") -> bool:
        """[Debug] Read/set the live world_progress (0xE28CB4). With no arg it
        prints the current value; with a number it writes it. Used to test
        whether world_progress gates which world-map vehicle models load.
        After setting, walk into a field and back to the world map.
        """
        pm = getattr(self.ctx, "pm", None)
        if pm is None:
            logger.warning("Not attached to FF7 — open the game first.")
            return False
        try:
            if not value.strip():
                logger.info(f"[setwp] world_progress = {pm.read_int(0xE28CB4)}")
                return True
            v = int(value.strip(), 0)
            pm.write_int(0xE28CB4, v)
            logger.info(f"[setwp] world_progress set to {v} — re-enter the world map to test.")
        except Exception as exc:
            logger.warning(f"[setwp] failed: {exc}")
        return True

    def _cmd_mapbitons(self, path: str = "") -> bool:
        """[Debug] Scan flevel.lgp and rebuild the BITON map.
        Usage: /mapbitons [ff7_install_dir]
        Prefer /setjson in normal use — this is a fallback for debugging.
        """
        from worlds.ff7.biton_mapper import build_biton_map, find_ff7_dir

        ff7_dir: Optional[Path] = None
        if path.strip():
            ff7_dir = Path(path.strip())
        elif self.ctx.ff7_dir:
            ff7_dir = self.ctx.ff7_dir
        else:
            ff7_dir = find_ff7_dir()
            if ff7_dir:
                logger.info(f"Auto-detected FF7 dir: {ff7_dir}")

        if ff7_dir is None:
            logger.warning(
                "FF7 install directory not found.  "
                'Run: /mapbitons "C:/Games/Final Fantasy VII"'
            )
            return False

        lgp_path = ff7_dir / "data" / "field" / "flevel.lgp"
        if not lgp_path.exists():
            logger.warning(f"flevel.lgp not found at: {lgp_path}")
            return False

        logger.info(f"Scanning {lgp_path} …")
        try:
            from worlds.ff7.Locations import ALL_LOCATION_TABLE
            locations = [
                {"code": loc.code, "map": loc.map, "category": loc.category}
                for loc in ALL_LOCATION_TABLE.values()
            ]
            biton_map = build_biton_map(lgp_path, locations)
        except Exception as exc:
            logger.warning(f"BITON scan failed: {exc}")
            return False

        self.ctx.ff7_dir  = ff7_dir
        self.ctx.biton_map = biton_map

        settings = _load_settings()
        settings["ff7_dir"] = str(ff7_dir)
        _save_settings(settings)

        logger.info(f"BITON map updated: {len(biton_map)} locations tracked.")
        return True


# ── Client context ────────────────────────────────────────────────────────────

class FF7Context(CommonContext):
    """Archipelago client context for Final Fantasy VII."""

    game             = "Final Fantasy VII"
    command_processor: type = FF7CommandProcessor
    items_handling   = 0b111

    def __init__(self, server_address: Optional[str], password: Optional[str]) -> None:
        super().__init__(server_address, password)
        self.finished_game: bool = False
        self.game_connected: bool = False
        self._checked_this_session: Set[int] = set()
        # Names of every AP item received this connection (for the crater gate).
        self._received_item_names: Set[str] = set()
        # Live pymem handle (set by game_watcher) so debug commands can read memory.
        self.pm = None
        # Model ids of delivered vehicles still needing relocation off the (0,0)
        # sea tile (only specific vehicles, so we never disturb the submarine etc).
        self._pending_vehicle_models: Set[int] = set()

        settings = _load_settings()
        stored_dir  = settings.get("ff7_dir")
        stored_json = settings.get("json_path")
        self.ff7_dir:  Optional[Path] = Path(stored_dir)  if stored_dir  else None
        self.json_path: Optional[Path] = Path(stored_json) if stored_json else None
        self.biton_map: Dict[int, Tuple[int, int, int]] = {}
        # Item delivery state (persists across poll cycles)
        self._delivered_item_indices: Set[int] = set()
        self._pending_items: List[Tuple[int, object]] = []
        # Boss checks that have been sent (location_id)
        self._boss_checks_sent: Set[int] = set()
        # Baseline established once per game connection: locations whose detection
        # bit is already set at connect (Free Roam starts at game moment 1603,
        # which leaves savemap progress noise). Suppressed so we never
        # false-report them as fresh checks.
        self._baseline_locations: Set[int] = set()
        self._baseline_established: bool = False
        # Whether the shop hook DLL has been injected this game connection.
        self._hook_injected: bool = False
        # Victory condition: 0 = defeat_sephiroth (default), 1 = escape_midgar
        self.victory_condition: int = 0
        # Free Roam mode (from slot_data) — gates Free-Roam-only savemap fixups.
        self.free_roam: bool = False
        # ── Shop-purchase detection (Tier-3 native-grid AP shops) ────────────
        # {ff7_item_id: location_code} for shop-slot "token" items. Buying the
        # token (sold by Gold Saucer's shop Hext, displayed with the AP name by
        # shophook.dll) fires that location and is swapped for a Potion.
        self.shop_token_to_location: Dict[int, int] = {}
        # Materia-space tokens (slot type 1) are detected in the materia inventory.
        self.shop_materia_to_location: Dict[int, int] = {}
        # token_id -> display name, parsed from the .apff7 shops array (used to
        # (re)write shop_ap.txt with the correct cross-player names at attach).
        self._shop_apff7_names: Dict[int, str] = {}
        self._shop_apff7_materia_names: Dict[int, str] = {}
        # {ff7_item_id: display_name} mirror of shop_ap.txt — drives the per-id
        # baseline even before location mappings exist (lets us test detection
        # against the manual shop_ap.txt before the full pipeline is wired).
        self._shop_token_names: Dict[int, str] = {}
        self._shop_materia_names: Dict[int, str] = {}
        # Inventory counts of token ids at connect; a later increase = a purchase.
        self._shop_token_baseline: Dict[int, int] = {}
        self._shop_materia_baseline: Dict[int, int] = {}
        self._shop_baseline_established: bool = False
        # Party gil at the previous poll — a token-count rise only counts as a
        # purchase if gil also fell (battles never cost gil), which lets ordinary
        # item ids serve as tokens without false-firing on battle drops/steals.
        self._prev_gil: int = -1

    async def server_auth(self, password_requested: bool = False) -> None:
        await super().server_auth(password_requested)
        if not self.auth:
            await self.get_username()
            await self.send_connect()

    def on_package(self, cmd: str, args: dict) -> None:
        super().on_package(cmd, args)
        if cmd == "Connected":
            self._checked_this_session.update(self.checked_locations)
            # Read victory condition from slot data (0 = defeat_sephiroth, 1 = escape_midgar)
            self.victory_condition = args.get("slot_data", {}).get("victory_condition", 0)
            self.free_roam = bool(args.get("slot_data", {}).get("free_roam", False))
            raw = args.get("slot_data", {}).get("biton_map", {})
            if raw:
                self.biton_map = {int(k): tuple(v) for k, v in raw.items()}
                logger.info(
                    f"BITON map received from server: {len(self.biton_map)} locations tracked."
                )
            else:
                self._load_biton_map_from_json()
            # Shop slots: prefer the server (slot_data) so no .apff7/`/setjson`
            # is needed; fall back to the .apff7 file only if not present.
            raw_shops = args.get("slot_data", {}).get("shops", [])
            if raw_shops:
                self.shop_token_to_location, self.shop_materia_to_location, \
                    self._shop_apff7_names, self._shop_apff7_materia_names = \
                    _shops_from_apff7(raw_shops)
                if self.shop_token_to_location or self.shop_materia_to_location:
                    logger.info(
                        "Shop slots from server: "
                        f"{len(self.shop_token_to_location) + len(self.shop_materia_to_location)}"
                        " AP shop check(s)."
                    )
            else:
                self._load_shops_from_json()
        elif cmd == "ReceivedItems":
            # Queue items for delivery on the next game_watcher tick
            index = args.get("index", 0)
            for offset, net_item in enumerate(args.get("items", [])):
                item_index = index + offset
                if item_index not in self._delivered_item_indices:
                    self._pending_items.append((item_index, net_item))

    def _load_biton_map_from_json(self) -> None:
        """Load BITON coordinates from the stored Archipelago JSON path."""
        json_path = self.json_path
        if json_path is None or not json_path.exists():
            logger.info(
                "No Archipelago JSON path configured — BITON tracking disabled.  "
                "Run /setjson <path_to_FF7_seed_P1.json> to enable it."
            )
            return
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.biton_map = _biton_map_from_placements(data.get("placements", []))
            self.shop_token_to_location, self.shop_materia_to_location, \
                self._shop_apff7_names, self._shop_apff7_materia_names = \
                _shops_from_apff7(data.get("shops", []))
            logger.info(
                f"BITON map loaded from {json_path.name}: "
                f"{len(self.biton_map)} locations tracked."
            )
        except Exception as exc:
            logger.warning(f"Failed to load BITON map from JSON: {exc}")

    def _load_shops_from_json(self) -> None:
        """Load AP shop slots from the .apff7 (token_id->location + names)."""
        json_path = self.json_path
        if json_path is None or not json_path.exists():
            return
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.shop_token_to_location, self.shop_materia_to_location, \
                self._shop_apff7_names, self._shop_apff7_materia_names = \
                _shops_from_apff7(data.get("shops", []))
            if self.shop_token_to_location:
                logger.info(
                    f"Shop slots loaded: {len(self.shop_token_to_location)} AP shop check(s)."
                )
        except Exception as exc:
            logger.debug(f"Failed to load shop slots from JSON: {exc}")


# ── Item delivery ─────────────────────────────────────────────────────────────

# FF7 field-script memory banks. Banks come in pairs sharing one 256-byte
# savemap region (odd = 8-bit view, even = 16-bit view), matching the field
# engine the Gold Saucer BITON opcodes target. This MUST agree with how Gold
# Saucer writes BITONs, or the client polls the wrong byte. Detection now uses
# each pickup's natural field-item flag, which lives across regions B-E.
#   1/2 -> 0x0BA4 (A)   3/4 -> 0x0CA4 (B)   5/6 -> 0x0DA4 (C)
#   11/12 -> 0x0EA4 (D) 13/14 -> 0x0FA4 (E) 15 -> 0x10A4 (F)
_BANK_BASE: Dict[int, int] = {
    0: 0x0BA4,
    1: 0x0BA4, 2: 0x0BA4,
    3: 0x0CA4, 4: 0x0CA4,
    5: 0x0DA4, 6: 0x0DA4,
    11: 0x0EA4, 12: 0x0EA4,
    13: 0x0FA4, 14: 0x0FA4,
    15: 0x10A4,
}


def _biton_byte_addr(bank: int, address: int) -> int:
    """Return absolute savemap byte offset for a BITON (bank, address) pair."""
    base = _BANK_BASE.get(bank, 0x0BA4)
    return SAVEMAP_BASE + base + address


# ── Key item flag map ─────────────────────────────────────────────────────────
# Each entry maps an item name to the list of (bank_address, bit) pairs to set
# in the FF7 savemap at SAVEMAP_BASE + 0x0BA4 + address.
# Derived from FieldPickupRandomizer_ff7tk::getKeyItemName (addresses 0x40-0x46).

# Wall Market disguise quest-state bytes (in addition to inventory flags above):
#   Bank 1, 0xA0 "Wall Market disguise items 1":
#     bit 0 Cologne obtained, bit 1 Flower Cologne, bit 2 Sexy Cologne,
#     bit 3 Wig, bit 4 Dyed Wig, bit 5 Blonde Wig,
#     bit 6 Pharmacy coupon, bit 7 Any wig obtained
#   Bank 1, 0xA1 "Wall Market disguise items 2":
#     bit 0 Poor make-up, bit 1 Average make-up, bit 2 Best make-up,
#     bit 3 Obtaining dress process, bit 4 Dress selected,
#     bit 5 Cotton Dress, bit 6 Satin Dress, bit 7 Silk Dress
# These quest-state bits must be set when AP delivers a disguise item
# remotely; otherwise the Wall Market scripts clear the disguise state.

KEY_ITEM_FLAGS: Dict[str, List[Tuple[int, int]]] = {
    # 0x40 (inventory) + 0xA1 (quest state: dress process + dress selected + specific dress)
    "Cotton Dress":               [(0x40, 0), (0xA1, 3), (0xA1, 4), (0xA1, 5)],
    "Satin Dress":                [(0x40, 1), (0xA1, 3), (0xA1, 4), (0xA1, 6)],
    "Silk Dress":                 [(0x40, 2), (0xA1, 3), (0xA1, 4), (0xA1, 7)],
    # 0x40 (inventory) + 0xA0 (quest state: specific wig + any wig obtained)
    "Wig":                        [(0x40, 3), (0xA0, 3), (0xA0, 7)],
    "Dyed Wig":                   [(0x40, 4), (0xA0, 4), (0xA0, 7)],
    "Blonde Wig":                 [(0x40, 5), (0xA0, 5), (0xA0, 7)],
    "Glass Tiara":                [(0x40, 6)],
    "Ruby Tiara":                 [(0x40, 7)],
    # 0x41 (inventory) + 0xA0 (quest state) for colognes
    "Diamond Tiara":              [(0x41, 0)],
    "Cologne":                    [(0x41, 1), (0xA0, 0)],
    "Flower Cologne":             [(0x41, 2), (0xA0, 1)],
    "Sexy Cologne":               [(0x41, 3), (0xA0, 2)],
    "Members Card":               [(0x41, 4)],
    "Lingerie":                   [(0x41, 5)],
    "Mystery Panties":            [(0x41, 6)],
    "Bikini Briefs":              [(0x41, 7)],
    # 0x42 (inventory) + 0xA0 bit 6 (quest state: pharmacy coupon obtained)
    "Pharmacy Coupon":            [(0x42, 0), (0xA0, 6)],
    "Disinfectant":               [(0x42, 1)],
    "Deodorant":                  [(0x42, 2)],
    "Digestive":                  [(0x42, 3)],
    "Huge Materia (Fort Condor)": [(0x42, 4)],
    "Huge Materia (Corel)":       [(0x42, 5)],
    "Huge Materia (Underwater)":  [(0x42, 6)],
    "Huge Materia (Rocket)":      [(0x42, 7)],
    # 0x43
    "Key to Ancients":            [(0x43, 0)],
    "Letter to a Daughter":       [(0x43, 1)],
    "Letter to a Wife":           [(0x43, 2)],
    "Lunar Harp":                 [(0x43, 3)],
    "Basement Key":               [(0x43, 4)],
    "Key to Sector 5":            [(0x43, 5)],
    "Keycard 60":                 [(0x43, 6)],
    "Keycard 62":                 [(0x43, 7)],
    # 0x44
    "Keycard 65":                 [(0x44, 0)],
    "Keycard 66":                 [(0x44, 1)],
    "Keycard 68":                 [(0x44, 2)],
    "Midgar Parts 1":             [(0x44, 3)],
    "Midgar Parts 2":             [(0x44, 4)],
    "Midgar Parts 3":             [(0x44, 5)],
    "Midgar Parts 4":             [(0x44, 6)],
    "Midgar Parts 5":             [(0x44, 7)],
    # 0x45
    "PHS":                        [(0x45, 0)],
    "Gold Ticket":                [(0x45, 1)],
    "Keystone":                   [(0x45, 2)],
    "Leviathan Scales":           [(0x45, 3)],
    "Glacier Map":                [(0x45, 4)],
    "A Coupon":                   [(0x45, 5)],
    "B Coupon":                   [(0x45, 6)],
    "C Coupon":                   [(0x45, 7)],
    # 0x46
    "Black Materia":              [(0x46, 0)],
    "Mythril":                    [(0x46, 1)],
    "Snowboard":                  [(0x46, 2)],
}

# Vehicle unlock flags for Free Roam mode.
# world_map_vehicles is at FF7SLOT offset 0x0C23.
# Bank-1 base is at FF7SLOT offset 0x0BA4, so bank-1 address = 0x0C23 - 0x0BA4 = 0x7F.
#   bit 2 (0x04) = Tiny Bronco visible on world map
#   bit 4 (0x10) = Highwind visible on world map
#   bit 0 (0x01) = Buggy (not used as AP item but coexists in same byte)
# tut_sub is at FF7SLOT offset 0x0C1E → bank-1 address 0x7A.
#   bit 2 (0x04) = sub tutorial seen; grants submarine access
VEHICLE_ITEM_FLAGS: Dict[str, Tuple[int, int, int]] = {
    # item_name: (bank1_address, vehicle_byte_mask, sub_tutorial_addr_or_zero)
    # Tiny Bronco removed (invisible world-map model in Free Roam); movement is
    # the Highwind + chocobos.
    "Highwind":    (0x7F, 0x10, 0),
    "Submarine":   (0x7A, 0x04, 0),  # sets tut_sub bit 2 to unlock sub
}


# ── Wall Market NPC quest-state side-effects ─────────────────────────────────
# mktpb (Wall Market bar) init script logic (confirmed via Makou Reactor):
#   If ANY dress flag (0x40 bits 0-2) is set → Var[5][16] = 1 → old man hides.
#   If Var[1][0xA1] & 0xE0 == 0 AND Var[5][16] == 0 → old man visible.
# The old man gives Pharmacy Coupon in vanilla.  Delivering a dress via AP
# skips his dialogue, so we must auto-deliver Pharmacy Coupon and mark him
# as processed (set bit 5 of Var[1][0xA1]) to avoid a quest softlock.
_MKTPB_OLD_MAN_VAR_ADDR  = 0xA1  # Var[1][161] in mktpb field script
_MKTPB_OLD_MAN_PROC_MASK = 0xE0  # bits 5-7: old man has given his item
_MKTPB_OLD_MAN_DONE_BIT  = 5     # bit we write to mark him processed
_DRESS_ITEMS = frozenset({"Cotton Dress", "Satin Dress", "Silk Dress"})


def _ensure_mktpb_old_man_processed(pm: "pymem.Pymem") -> None:
    """Side-effect for remote dress delivery.

    When a dress arrives via Archipelago the old man in the Wall Market bar
    (mktpb) silently hides on the next field entry, preventing the player
    from ever receiving the Pharmacy Coupon he normally hands out.
    This function delivers Pharmacy Coupon automatically (if not already
    obtained) and sets the 'old man processed' flag so the init script
    hides him cleanly rather than blocking the quest chain.
    """
    try:
        var_addr = _biton_byte_addr(1, _MKTPB_OLD_MAN_VAR_ADDR)
        current  = pm.read_uchar(var_addr)
        if (current & _MKTPB_OLD_MAN_PROC_MASK) == 0:
            # Old man hasn't given his item yet — deliver Pharmacy Coupon
            pharm_addr = _biton_byte_addr(1, 0x42)
            pharm_val  = pm.read_uchar(pharm_addr)
            if not (pharm_val & 0x01):   # bit 0 = Pharmacy Coupon
                pm.write_uchar(pharm_addr, pharm_val | 0x01)
                logger.info(
                    "Wall Market side-effect: delivered Pharmacy Coupon "
                    "(mktpb old man bypassed by remote dress delivery)"
                )
            pm.write_uchar(var_addr, current | (1 << _MKTPB_OLD_MAN_DONE_BIT))
            logger.info("Wall Market side-effect: mktpb old man marked as processed")
    except Exception as exc:
        logger.debug(f"Wall Market side-effect failed: {exc}")


# Sector 5 world-map gate side-effect.
# The world-map entrance to Midgar runs a world-script gate that deactivates
# its entrance triangle while a world-script flag is OFF. That flag is NOT the
# Key to Sector 5 *inventory* bit (bank-1 0x43.5) that delivery sets — it is the
# world-script var the gate tests: Var[15][38] bit 3. World-script savemap banks
# are 256 bytes from the savemap start, so Var[15][38] = byte 0x0F26 (15*256+38),
# bit 3. Set it so the gate opens once the player owns the key.
# NOTE: 0x0F26/bit3 is decoded from the tool's Var[15][38] notation; verify in
# Landscaper (or live via the Ultima editor) if the gate still won't open.
_SECTOR5_GATE_OFFSET = 0x0F26   # FF7SLOT offset (live addr = SAVEMAP_BASE + this)
_SECTOR5_GATE_BIT    = 3        # bit 3 (mask 0x08)


def _ensure_sector5_world_gate(pm: "pymem.Pymem") -> None:
    """Open the world-map Sector 5 / Midgar entrance gate (Free Roam)."""
    try:
        addr = SAVEMAP_BASE + _SECTOR5_GATE_OFFSET
        current = pm.read_uchar(addr)
        if not (current & (1 << _SECTOR5_GATE_BIT)):
            pm.write_uchar(addr, current | (1 << _SECTOR5_GATE_BIT))
            logger.info(
                "Sector 5 side-effect: opened world-map gate "
                f"(0x{_SECTOR5_GATE_OFFSET:04X} bit {_SECTOR5_GATE_BIT})"
            )
    except Exception as exc:
        logger.debug(f"Sector 5 world-gate side-effect failed: {exc}")


def _deliver_key_item_flag(pm: "pymem.Pymem", item_name: str) -> bool:
    """Set the savemap bit flag(s) for a key item.  Returns True on success."""
    flags = KEY_ITEM_FLAGS.get(item_name)
    if not flags:
        logger.warning(f"No flag mapping for key item '{item_name}' — cannot deliver")
        return False
    try:
        for address, bit in flags:
            byte_addr = _biton_byte_addr(1, address)
            current = pm.read_uchar(byte_addr)
            pm.write_uchar(byte_addr, current | (1 << bit))
        logger.info(f"Delivered key item: {item_name}")
        if item_name in _DRESS_ITEMS:
            _ensure_mktpb_old_man_processed(pm)
        if item_name == "Key to Sector 5":
            _ensure_sector5_world_gate(pm)
        return True
    except Exception as exc:
        logger.debug(f"Key item flag write failed for '{item_name}': {exc}")
        return False


# Live world-map memory (absolute VAs; FF7 is non-ASLR). The savemap coords are
# NOT used by the live world map, so we move the vehicle's live entity instead.
# Player position is a vector4<int> in (X, Z, Y) order. World entities are a
# linked list of world_event_data: next_ptr@+0x00, position(X,Z,Y)@+0x0C.
# An AP-delivered vehicle whose spawn script never ran sits stranded at (0,0);
# we relocate it to the player's X/Y so it's reachable.
_WORLD_PLAYER_POS = 0xE04918   # vector4<int> X@+0, Z@+4, Y@+8
_WORLD_ENTITY_PTR = 0xE39AD8   # world_event_data** (current entity)
_WE_NEXT  = 0x00               # world_event_data.next_ptr
_WE_POS   = 0x0C               # world_event_data.position (X@+0, Z@+4, Y@+8)
_WE_MODEL = 0x50               # world_event_data.model_id (byte)

# Vehicle world-map model ids (from /wdump). Only listed vehicles get relocated,
# so the submarine and roaming Weapons are never touched. (Tiny Bronco, model 5,
# was removed — invisible world-map model in the Free Roam state.)
_VEHICLE_MODEL_IDS: Dict[str, int] = {
    "Highwind": 3,
    "Submarine": 13,
}
# Fixed spawn per model id as (X, Z, Y) — Z (height) matters or the vehicle
# sinks into / floats above the terrain.
_VEHICLE_FIXED_POS: Dict[int, Tuple[int, int, int]] = {
    # Highwind — player position captured via /wdump (X, Z, Y). The Highwind's
    # model renders fine at this game state; it just needs positioning off (0,0).
    3: (200728, 315, 115347),
    # Submarine — accessible surface spot captured via /wdump (X, Z, Y).
    13: (170091, -240, 149648),
}
# Model ids safe to drop on the player's position (flying vehicles only).
_VEHICLE_PLAYER_OK: frozenset = frozenset()

# (X, Y) spots earlier client builds wrongly spawned vehicles at; a queued
# vehicle found here is migrated to its proper target.
_VEHICLE_LEGACY_BAD_SPOTS: frozenset = frozenset()

# Savemap parked-vehicle coord slots (FF7SLOT offsets) by model id. The game
# spawns the parked vehicle's MODEL from the id packed into this coord.
# Packing (ff7tk): chunk1 = X(&0x7FFFF) | id<<19 | angle<<24; chunk2 = Y(&0x3FFFF) | Z<<18.
_VEHICLE_SAVEMAP_SLOT: Dict[int, Tuple[int, int]] = {
    3:  (0x0F74, 0x0F78),   # Highwind  -> bh_world / bh_world2
    13: (0x0F7C, 0x0F80),   # Submarine -> sub_world / sub_world2
}


def _write_vehicle_savemap_coord(pm: "pymem.Pymem", model_id: int) -> None:
    """Populate the savemap parked-vehicle coord (with the model id) so the game
    loads the vehicle's model on the next world-map (re)spawn — fixes the
    'usable but invisible' vehicle. Z is left 0; the game derives water height."""
    slot = _VEHICLE_SAVEMAP_SLOT.get(model_id)
    target = _VEHICLE_FIXED_POS.get(model_id)
    if slot is None or target is None:
        return
    x, _z, y = target
    chunk1 = (x & 0x7FFFF) | ((model_id & 0x1F) << 19) | ((16 & 0xFF) << 24)
    chunk2 = (y & 0x3FFFF)
    try:
        pm.write_uint(SAVEMAP_BASE + slot[0], chunk1)
        pm.write_uint(SAVEMAP_BASE + slot[1], chunk2)
    except Exception as exc:
        logger.debug(f"savemap vehicle coord write failed: {exc}")


def _place_stranded_vehicles(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Relocate a delivered vehicle stranded at (0,0). Targets only the specific
    vehicle model ids in ctx._pending_vehicle_models (never the submarine), and
    uses a fixed beach coord where set, else the player's position."""
    if not ctx._pending_vehicle_models:
        return
    try:
        px = pm.read_int(_WORLD_PLAYER_POS + 0)
        pz = pm.read_int(_WORLD_PLAYER_POS + 4)
        py = pm.read_int(_WORLD_PLAYER_POS + 8)
    except Exception:
        return
    if px == 0 and py == 0:
        return  # not on the world map yet — retry next tick
    try:
        ptr = pm.read_uint(_WORLD_ENTITY_PTR)
    except Exception:
        return
    seen: Set[int] = set()
    placed: Set[int] = set()
    for _ in range(48):
        if ptr == 0 or ptr < 0x400000 or ptr in seen:
            break
        seen.add(ptr)
        try:
            model_id = pm.read_uchar(ptr + _WE_MODEL)
            if model_id in ctx._pending_vehicle_models:
                # target = (X, Z, Y)
                target = _VEHICLE_FIXED_POS.get(model_id)
                if target is None and model_id in _VEHICLE_PLAYER_OK:
                    target = (px, pz, py)
                if target is not None:
                    ex = pm.read_int(ptr + _WE_POS + 0)
                    ez = pm.read_int(ptr + _WE_POS + 4)
                    ey = pm.read_int(ptr + _WE_POS + 8)
                    stranded  = (ex == 0 and ey == 0)
                    # fix a vehicle at the target X/Y but wrong height (sunk),
                    # and migrate one stuck at a previous bad spawn spot.
                    wrong_z   = (ex == target[0] and ey == target[2] and ez != target[1])
                    legacy    = (ex, ey) in _VEHICLE_LEGACY_BAD_SPOTS
                    if stranded or wrong_z or legacy:
                        pm.write_int(ptr + _WE_POS + 0, target[0])
                        pm.write_int(ptr + _WE_POS + 4, target[1])
                        pm.write_int(ptr + _WE_POS + 8, target[2])
                        # Also write the savemap parked coord (with the model id)
                        # so a reload spawns the vehicle WITH its model (visible).
                        _write_vehicle_savemap_coord(pm, model_id)
                        placed.add(model_id)
            ptr = pm.read_uint(ptr + _WE_NEXT)
        except Exception:
            break
    if placed:
        ctx._pending_vehicle_models -= placed
        logger.info(f"Relocated vehicle(s) model_id={sorted(placed)} to spawn coords")


def _enforce_crater_lock(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Drive the savemap Northern Crater gate byte: 1 once every goal item is
    received (Highwind + full party + 4 Huge Materia), else 0. The Gold Saucer
    field-gate reads this and bounces the player out while it is 0."""
    unlocked = CRATER_REQUIRED_ITEMS.issubset(ctx._received_item_names)
    try:
        addr = SAVEMAP_BASE + CRATER_LOCK_OFFSET
        if pm.read_uchar(addr) != (1 if unlocked else 0):
            pm.write_uchar(addr, 1 if unlocked else 0)
            if unlocked:
                logger.info("Northern Crater unlocked — all goal items received.")
    except Exception as exc:
        logger.debug(f"crater lock write failed: {exc}")


def _resolve_ultimate_weapon(pm: "pymem.Pymem") -> None:
    """Finish Ultimate Weapon in Free Roam. His kill flag (weapons_killed.bit[0])
    is set by his final battle, which never becomes lethal because the disc-2 chase
    that whittles his HP is skipped at game moment 1603. Once the player has engaged
    him (submarine_flags.bit[3], set the moment they first ram him), set
    weapons_killed.bit[0] so he dies, stops respawning, and the AP 'Defeat Ultimate
    Weapon' check fires. No-op until engaged / once dead."""
    try:
        wk_addr = SAVEMAP_BASE + WEAPONS_KILLED_OFFSET
        wk = pm.read_uchar(wk_addr)
        if wk & 0x01:            # bit0 — already defeated
            return
        sf = pm.read_uchar(SAVEMAP_BASE + SUBMARINE_FLAGS_OFFSET)
        if sf & 0x08:            # bit3 — chase started (player has engaged him)
            pm.write_uchar(wk_addr, wk | 0x01)
            logger.info("Ultimate Weapon defeated (Free Roam) — weapons_killed.bit[0] set.")
    except Exception as exc:
        logger.debug(f"resolve ultimate weapon failed: {exc}")


def _deliver_vehicle_item(pm: "pymem.Pymem", item_name: str) -> bool:
    """Unlock a Free Roam vehicle by writing its flag byte in the savemap.

    Uses VEHICLE_ITEM_FLAGS to OR-set the vehicle bit mask into the
    world_map_vehicles byte (FF7SLOT offset 0x0C23, bank-1 addr 0x7F)
    or the tut_sub byte (0x0C1E, bank-1 addr 0x7A) for Submarine.
    """
    entry = VEHICLE_ITEM_FLAGS.get(item_name)
    if entry is None:
        return False
    bank1_addr, mask, _ = entry
    try:
        byte_addr = _biton_byte_addr(1, bank1_addr)
        current = pm.read_uchar(byte_addr)
        pm.write_uchar(byte_addr, current | mask)
        logger.info(f"Vehicle unlocked: {item_name} (addr=0x{bank1_addr:02X} mask=0x{mask:02X})")
        return True
    except Exception as exc:
        logger.debug(f"Vehicle flag write failed for '{item_name}': {exc}")
        return False


# ── Green Chocobo delivery (Free Roam: cross the Junon-area mountain crater) ───
# In Free Roam the only land route to Junon is blocked by the "Junon Area crater"
# world-map alternative (mountain terrain).  A mountain-capable (green) chocobo
# crosses it.  Only a *stabled, bred* chocobo carries the green colour, so we
# write a green FF7CHOCOBO record into Chocobo Farm stable slot 0 (foot-reachable
# from Kalm) and set the stable bookkeeping so Choco Billy will release it.
# All offsets are FF7SLOT offsets (live addr = SAVEMAP_BASE + offset), verified
# from ff7tk FF7Save_Types.h / Type_FF7CHOCOBO.h.
_CHOCO_SLOT0      = 0x0DC4  # FF7CHOCOBO chocobos[0] (16 bytes)
_CHOCO_STABLES    = 0x0CFC  # qty of stables owned
_CHOCO_OCCUPIED   = 0x0CFD  # qty of occupied stables
_CHOCO_MASK       = 0x0CFF  # bitmask of occupied stable slots (bit 0 = slot 1)
_CHOCO_RATING0    = 0x0E3E  # stablechocorating[0] (1=Wonderful .. 8=Worst)
_CHOCO_NAME0      = 0x0EC4  # chocobonames[0][6] (FF7 text, 0xFF-terminated)
_CHOCO_STAMINA0   = 0x0EE8  # chocostaminas[0] (u16)
# FF7CHOCOBO.type byte (record +0x0F): 0=Yellow 1=Green 2=Blue 3=Black 4=Gold.
# Green is confirmed working in-game; the others follow the same enum/record.
# Terrain: Green=mountains, Blue=rivers/shallows, Black=mountains+rivers,
# Gold=all terrain incl. deep ocean.
_CHOCO_TYPES = {
    "Green Chocobo": 1,
    "Blue Chocobo":  2,
    "Black Chocobo": 3,
    "Gold Chocobo":  4,
}
CHOCOBO_ITEM_NAMES = frozenset(_CHOCO_TYPES)


def _deliver_chocobo(pm: "pymem.Pymem", item_name: str) -> bool:
    """Place a bred chocobo of the given colour into Chocobo Farm stable slot 1.

    Each colour overwrites slot 0 with its own type byte; receiving a new colour
    replaces the previous one (one delivered chocobo at a time). Only the higher-
    terrain colour matters for traversal, so this is fine — and AP logic tracks
    the items independently regardless of which is physically stabled.
    """
    type_byte = _CHOCO_TYPES.get(item_name)
    if type_byte is None:
        return False
    try:
        base = SAVEMAP_BASE
        rec  = base + _CHOCO_SLOT0
        # Idempotent: skip if slot 0 already holds this colour and is stabled.
        if pm.read_uchar(rec + 0x0F) == type_byte \
                and (pm.read_uchar(base + _CHOCO_MASK) & 0x01):
            return True
        # FF7CHOCOBO record (16 B).  Only `type` governs terrain; stats are
        # plausible filler so Choco Billy displays/releases it cleanly.
        pm.write_ushort(rec + 0x00, 1000)  # sprintspd
        pm.write_ushort(rec + 0x02, 1000)  # maxsprintspd
        pm.write_ushort(rec + 0x04, 1000)  # speed
        pm.write_ushort(rec + 0x06, 1000)  # maxspeed
        pm.write_uchar (rec + 0x08, 20)    # accel
        pm.write_uchar (rec + 0x09, 20)    # coop
        pm.write_uchar (rec + 0x0A, 20)    # intelligence
        pm.write_uchar (rec + 0x0B, 0)     # personality (range unknown; 0 = safe default)
        pm.write_uchar (rec + 0x0C, 0)     # pcount
        pm.write_uchar (rec + 0x0D, 0)     # raceswon
        pm.write_uchar (rec + 0x0E, 0)     # sex (0 = male)
        pm.write_uchar (rec + 0x0F, type_byte)
        # Per-chocobo extras (parallel arrays, slot 0).
        pm.write_ushort(base + _CHOCO_STAMINA0, 1000)
        pm.write_uchar (base + _CHOCO_RATING0, 1)            # Wonderful
        for i in range(6):                                  # empty (default) name
            pm.write_uchar(base + _CHOCO_NAME0 + i, 0xFF)
        # Stable bookkeeping: own ≥1 stable, mark slot 1 occupied.
        if pm.read_uchar(base + _CHOCO_STABLES) < 1:
            pm.write_uchar(base + _CHOCO_STABLES, 1)
        mask = pm.read_uchar(base + _CHOCO_MASK) | 0x01
        pm.write_uchar(base + _CHOCO_MASK, mask)
        pm.write_uchar(base + _CHOCO_OCCUPIED, bin(mask).count("1"))
        logger.info(f"Delivered {item_name} to Chocobo Farm stable slot 1")
        return True
    except Exception as exc:
        logger.debug(f"Chocobo delivery failed for {item_name}: {exc}")
        return False


# ── Party member delivery (Free Roam: unlock optional characters) ─────────────
# Savemap char roster order: Cloud,Barret,Tifa,Aerith,RedXIII,Yuffie,CaitSith,
# Vincent,Cid -> ids 0..8. PHS availability is a per-id bitmask.
_CHARACTER_IDS = {"Barret": 1, "Tifa": 2, "Aerith": 3, "Red XIII": 4, "Cait Sith": 6, "Cid": 8}
_PARTY_OFFSET       = 0x04F8   # qint8 party[3] — active party member ids
_PHS_ALLOWED_OFFSET = 0x10A4   # quint16 — who is allowed in the PHS
_PHS_VISIBLE_OFFSET = 0x10A6   # quint16 — who is visible in the PHS
_CHARS_OFFSET       = 0x0054   # FF7CHAR chars[9]
_CHAR_RECORD_SIZE   = 132      # bytes per character record


def _deliver_character(pm: "pymem.Pymem", char_name: str) -> bool:
    """Unlock an optional party member: make them available in the PHS, and drop
    them into an empty active party slot if one is free."""
    cid = _CHARACTER_IDS.get(char_name)
    if cid is None:
        return False
    try:
        bit = 1 << cid
        # PHS availability (allowed + visible).
        for off in (_PHS_ALLOWED_OFFSET, _PHS_VISIBLE_OFFSET):
            addr = SAVEMAP_BASE + off
            val = pm.read_ushort(addr)
            if not (val & bit):
                pm.write_ushort(addr, val | bit)
        # Auto-fill an empty active party slot (0xFF empty / 0xFE locked).
        base = SAVEMAP_BASE + _PARTY_OFFSET
        slots = [pm.read_uchar(base + i) for i in range(3)]
        if cid not in slots:
            for i in range(3):
                if slots[i] in (0xFF, 0xFE):
                    pm.write_uchar(base + i, cid)
                    break
        # Sanity check: a level-0 record means the savemap char wasn't seeded.
        if pm.read_uchar(SAVEMAP_BASE + _CHARS_OFFSET + cid * _CHAR_RECORD_SIZE + 1) == 0:
            logger.warning(
                f"{char_name}: savemap record reads level 0 — they may appear "
                f"with no stats; record initialization may be needed."
            )
        logger.info(f"Delivered party member: {char_name}")
        return True
    except Exception as exc:
        logger.debug(f"Character delivery failed for {char_name}: {exc}")
        return False


def _deliver_items_to_game(pm: "pymem.Pymem", ctx: FF7Context) -> None:
    """Drain ctx._pending_items and write each one to FF7 memory."""
    if not ctx._pending_items:
        return

    still_pending: List[Tuple[int, object]] = []
    code_map = _get_code_to_item_name()
    for item_index, net_item in ctx._pending_items:
        item_code = getattr(net_item, "item", None)
        if isinstance(item_code, int):
            item_name = code_map.get(item_code)
        else:
            item_name = item_code if isinstance(item_code, str) else None

        if item_name is None:
            logger.debug(f"Unknown item code {item_code!r} — skipping delivery")
            ctx._delivered_item_indices.add(item_index)
            continue

        ctx._received_item_names.add(item_name)  # for the Northern Crater gate

        if item_name in CHOCOBO_ITEM_NAMES:
            if _deliver_chocobo(pm, item_name):
                ctx._delivered_item_indices.add(item_index)
            else:
                still_pending.append((item_index, net_item))
            continue

        if item_name in VEHICLE_ITEM_FLAGS:
            if _deliver_vehicle_item(pm, item_name):
                ctx._delivered_item_indices.add(item_index)
                mid = _VEHICLE_MODEL_IDS.get(item_name)
                if mid is not None:
                    # Set the savemap parked coord so the vehicle (re)spawns at its
                    # target with its model (the Submarine spawns parked, not at
                    # (0,0)), and queue live relocation for any stranded at (0,0).
                    _write_vehicle_savemap_coord(pm, mid)
                    ctx._pending_vehicle_models.add(mid)
            else:
                still_pending.append((item_index, net_item))
            continue

        if item_name in _CHARACTER_IDS:
            if _deliver_character(pm, item_name):
                ctx._delivered_item_indices.add(item_index)
            else:
                still_pending.append((item_index, net_item))
            continue

        if item_name in KEY_ITEM_FLAGS:
            if _deliver_key_item_flag(pm, item_name):
                ctx._delivered_item_indices.add(item_index)
                # Prevent the game_watcher from re-firing these same biton
                # addresses as location checks (the client wrote them, not the
                # game — firing would create an item-delivery feedback loop).
                for ki_addr, ki_bit in KEY_ITEM_FLAGS[item_name]:
                    for loc_code, (bk, a, b) in ctx.biton_map.items():
                        if bk == 1 and a == ki_addr and b == ki_bit:
                            ctx._checked_this_session.add(loc_code)
            else:
                still_pending.append((item_index, net_item))
            continue

        result = _item_name_to_ff7_id(item_name)
        if result is None:
            logger.debug(f"No FF7 ID for item '{item_name}' — skipping delivery")
            ctx._delivered_item_indices.add(item_index)
            continue

        category, ff7_id = result
        try:
            if category == "materia":
                _write_materia(pm, ff7_id)
            else:
                # items / weapons / armors / accessories all go in the item list
                _write_item(pm, ff7_id)
            ctx._delivered_item_indices.add(item_index)
            logger.info(f"Delivered item: {item_name} (ff7_id={ff7_id})")
        except Exception as exc:
            logger.debug(f"Item delivery failed for '{item_name}': {exc}")
            still_pending.append((item_index, net_item))

    ctx._pending_items = still_pending


def _write_item(pm: "pymem.Pymem", ff7_id: int, qty: int = 1) -> None:
    """Add qty of ff7_id to the item inventory (stacks if already present)."""
    base = SAVEMAP_BASE + ITEM_LIST_OFFSET
    for slot in range(ITEM_SLOT_COUNT):
        word = pm.read_ushort(base + slot * 2)
        slot_id  = word & 0x1FF          # lower 9 bits
        slot_qty = (word >> 9) & 0x7F    # upper 7 bits
        if slot_id == ff7_id and slot_qty > 0:
            new_qty = min(99, slot_qty + qty)
            pm.write_ushort(base + slot * 2, ff7_id | (new_qty << 9))
            return
    # Find empty slot (id == 0x1FF / word == 0xFFFF is the FF7 empty sentinel)
    for slot in range(ITEM_SLOT_COUNT):
        word = pm.read_ushort(base + slot * 2)
        if (word & 0x1FF) == 0x1FF or word == EMPTY_ITEM_WORD:
            pm.write_ushort(base + slot * 2, ff7_id | (qty << 9))
            return
    raise RuntimeError(f"Item inventory full — could not deliver ff7_id={ff7_id}")


# ── Shop-purchase detection ───────────────────────────────────────────────────
# Native-grid Tier-3 AP shops: Gold Saucer's shop Hext sells "token" item ids,
# shophook.dll displays the AP name on them, and the player buys normally. Here
# the client notices the token entering inventory, fires the AP location, and
# swaps the token for a Potion so the player always walks away with a Potion.

# Item handed back after an AP shop purchase. None = give nothing (ideal: the
# player spends gil and receives no item). Set to 0 to hand back a Potion instead
# (fallback if "no item" ever proves problematic).
SHOP_REFUND_ITEM_ID: Optional[int] = None
POTION_FF7_ID = 0


def _load_shop_overrides(exe_dir: Path) -> Dict[int, str]:
    """Parse shop_ap.txt (the same file shophook.dll reads) -> {ff7_id: name}."""
    result: Dict[int, str] = {}
    try:
        path = exe_dir / "shop_ap.txt"
        if not path.exists():
            return result
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            id_str, name = line.split("=", 1)
            try:
                result[int(id_str.strip(), 0)] = name.strip()
            except ValueError:
                continue
    except Exception as exc:
        logger.debug(f"shop_ap.txt parse failed: {exc}")
    return result


def _read_all_item_counts(pm: "pymem.Pymem") -> Dict[int, int]:
    """One block read of the item inventory -> {ff7_id: total_qty}."""
    raw = pm.read_bytes(SAVEMAP_BASE + ITEM_LIST_OFFSET, ITEM_SLOT_COUNT * 2)
    counts: Dict[int, int] = {}
    for i in range(ITEM_SLOT_COUNT):
        word = raw[i * 2] | (raw[i * 2 + 1] << 8)
        if word != EMPTY_ITEM_WORD:
            counts[word & 0x1FF] = counts.get(word & 0x1FF, 0) + ((word >> 9) & 0x7F)
    return counts


def _remove_item(pm: "pymem.Pymem", ff7_id: int, qty: int) -> None:
    """Remove up to qty of ff7_id from the item inventory."""
    base = SAVEMAP_BASE + ITEM_LIST_OFFSET
    remaining = qty
    for slot in range(ITEM_SLOT_COUNT):
        if remaining <= 0:
            return
        addr = base + slot * 2
        word = pm.read_ushort(addr)
        if word == EMPTY_ITEM_WORD or (word & 0x1FF) != ff7_id:
            continue
        slot_qty = (word >> 9) & 0x7F
        take = min(slot_qty, remaining)
        remaining -= take
        new_qty = slot_qty - take
        pm.write_ushort(addr, (ff7_id | (new_qty << 9)) if new_qty > 0 else EMPTY_ITEM_WORD)


def _read_all_materia_counts(pm: "pymem.Pymem") -> Dict[int, int]:
    """One block read of the materia inventory -> {materia_id: count}."""
    raw = pm.read_bytes(SAVEMAP_BASE + MATERIA_LIST_OFFSET, MATERIA_SLOT_COUNT * 4)
    counts: Dict[int, int] = {}
    for i in range(MATERIA_SLOT_COUNT):
        mid = raw[i * 4]
        if mid != EMPTY_MATERIA_BYTE:
            counts[mid] = counts.get(mid, 0) + 1
    return counts


def _remove_materia(pm: "pymem.Pymem", mid: int, qty: int) -> None:
    """Remove up to qty copies of materia id `mid` (mark slots empty)."""
    base = SAVEMAP_BASE + MATERIA_LIST_OFFSET
    removed = 0
    for slot in range(MATERIA_SLOT_COUNT):
        if removed >= qty:
            return
        addr = base + slot * 4
        if pm.read_uchar(addr) == mid:
            for b in range(4):                       # 0xFFFFFFFF = empty slot
                pm.write_uchar(addr + b, EMPTY_MATERIA_BYTE)
            removed += 1


def _process_shop_purchases(pm: "pymem.Pymem", ctx: "FF7Context") -> List[int]:
    """Detect AP shop-token purchases (item + materia space), take the token back
    (no item given), and return the location codes to check. A token purchase =
    inventory count above baseline while gil also dropped."""
    if not ctx._shop_token_names and not ctx._shop_materia_names:
        return []
    try:
        counts = _read_all_item_counts(pm)
        gil = pm.read_uint(SAVEMAP_BASE + GIL_OFFSET)
    except Exception:
        return []
    # A purchase costs gil; a battle drop/steal does not. Only honour token
    # increases when gil also fell since the last poll.
    gil_dropped = (ctx._prev_gil >= 0 and gil < ctx._prev_gil)
    ctx._prev_gil = gil

    newly: List[int] = []
    changed = False
    for ff7_id, name in ctx._shop_token_names.items():
        current = counts.get(ff7_id, 0)
        baseline = ctx._shop_token_baseline.get(ff7_id, 0)
        delta = current - baseline
        if delta <= 0:
            if current < baseline:
                ctx._shop_token_baseline[ff7_id] = current  # consumed in-game
            continue
        if not gil_dropped:
            # Token id rose without spending gil → a battle drop, not a purchase.
            # Absorb into baseline (keep the real item) without firing a check.
            ctx._shop_token_baseline[ff7_id] = current
            continue
        loc = ctx.shop_token_to_location.get(ff7_id)
        if (loc is not None and loc not in ctx.checked_locations
                and loc not in ctx._checked_this_session):
            newly.append(loc)
            ctx._checked_this_session.add(loc)
        try:
            _remove_item(pm, ff7_id, delta)                 # take the purchased token back
            if SHOP_REFUND_ITEM_ID is not None and ff7_id != SHOP_REFUND_ITEM_ID:
                _write_item(pm, SHOP_REFUND_ITEM_ID, delta)  # optional refund item
        except Exception as exc:
            logger.debug(f"Shop swap failed for id {ff7_id}: {exc}")
        logger.info(
            f"AP shop purchase ×{delta}: {name}"
            + ("" if loc is not None else "  (no location mapped — display test)")
        )
        changed = True
    if changed:
        # Re-baseline so the returned Potions / removed tokens aren't recounted.
        try:
            after = _read_all_item_counts(pm)
            for ff7_id in ctx._shop_token_names:
                ctx._shop_token_baseline[ff7_id] = after.get(ff7_id, 0)
        except Exception:
            pass

    # ── Materia-space tokens (materia inventory) ──────────────────────────────
    if ctx._shop_materia_names:
        try:
            mcounts = _read_all_materia_counts(pm)
        except Exception:
            mcounts = {}
        mat_changed = False
        for mid, name in ctx._shop_materia_names.items():
            current = mcounts.get(mid, 0)
            baseline = ctx._shop_materia_baseline.get(mid, 0)
            delta = current - baseline
            if delta <= 0:
                if current < baseline:
                    ctx._shop_materia_baseline[mid] = current  # consumed in-game
                continue
            if not gil_dropped:
                ctx._shop_materia_baseline[mid] = current  # battle drop, not a buy
                continue
            loc = ctx.shop_materia_to_location.get(mid)
            if (loc is not None and loc not in ctx.checked_locations
                    and loc not in ctx._checked_this_session):
                newly.append(loc)
                ctx._checked_this_session.add(loc)
            try:
                _remove_materia(pm, mid, delta)            # take the token back
            except Exception as exc:
                logger.debug(f"Shop materia swap failed for id {mid}: {exc}")
            logger.info(
                f"AP shop purchase (materia) ×{delta}: {name}"
                + ("" if loc is not None else "  (no location mapped — display test)")
            )
            mat_changed = True
        if mat_changed:
            try:
                after_m = _read_all_materia_counts(pm)
                for mid in ctx._shop_materia_names:
                    ctx._shop_materia_baseline[mid] = after_m.get(mid, 0)
            except Exception:
                pass
    return newly


def _write_materia(pm: "pymem.Pymem", ff7_id: int, ap: int = 0) -> None:
    """Add a materia to the materia inventory."""
    base = SAVEMAP_BASE + MATERIA_LIST_OFFSET
    for slot in range(MATERIA_SLOT_COUNT):
        slot_id = pm.read_uchar(base + slot * 4)
        if slot_id == EMPTY_MATERIA_BYTE:
            pm.write_uchar(base + slot * 4, ff7_id)
            # Write AP as 3-byte little-endian
            pm.write_uchar(base + slot * 4 + 1, ap & 0xFF)
            pm.write_uchar(base + slot * 4 + 2, (ap >> 8) & 0xFF)
            pm.write_uchar(base + slot * 4 + 3, (ap >> 16) & 0xFF)
            return
    raise RuntimeError(f"Materia inventory full — could not deliver ff7_id={ff7_id}")


# ── Game watcher ──────────────────────────────────────────────────────────────

async def game_watcher(ctx: FF7Context) -> None:
    """Poll FF7's in-memory Savemap; send LocationChecks when BITON flags flip."""
    if not _PYMEM_AVAILABLE:
        logger.warning(
            "pymem is not installed — FF7 memory reading is disabled.\n"
            "Run: pip install pymem"
        )
        await ctx.exit_event.wait()
        return

    pm: Optional[pymem.Pymem] = None
    last_log = ""

    def log_once(msg: str) -> None:
        nonlocal last_log
        if msg != last_log:
            last_log = msg
            logger.info(msg)

    while not ctx.exit_event.is_set():
        # ── Warn if no BITON map yet ──────────────────────────────────────
        if not ctx.biton_map:
            log_once(
                "No BITON map loaded.  Connect to server, then run "
                "/setjson <path_to_FF7_seed.json> to enable location tracking."
            )
            await asyncio.sleep(3)
            continue

        # ── Attach to game process ────────────────────────────────────────
        if pm is None:
            for name in PROCESS_NAMES:
                try:
                    pm = pymem.Pymem(name)
                    log_once(f"FF7 process attached: {name}")
                    ctx.game_connected = True
                    ctx.pm = pm
                    
                    # ── Enable materia menu from start ─────────────────────
                    # Set bit 3 of savemap byte 0x0BC0 (bank 1, address 0x1C)
                    # This unlocks the Materia menu option in the main menu
                    try:
                        materia_menu_addr = SAVEMAP_BASE + 0x0BC0
                        current_val = pm.read_uchar(materia_menu_addr)
                        if not (current_val & 0x08):  # Bit 3 not set
                            pm.write_uchar(materia_menu_addr, current_val | 0x08)
                            logger.info("Materia menu enabled from game start")
                    except Exception as e:
                        logger.debug(f"Could not enable materia menu: {e}")

                    # ── Inject the shop hook DLL (Tier-3 shops) ────────────
                    # Opt-in: only if shophook.dll sits next to FF7_EN.exe.
                    # This is what makes a MinHook DLL work under 7th Heaven —
                    # FFNx never loads it as a mod, so we inject post-launch.
                    if not ctx._hook_injected:
                        try:
                            from worlds.ff7.dll_inject import inject_dll
                            from pathlib import Path
                            exe_dir = Path(pm.process_base.filename).parent \
                                if getattr(pm, "process_base", None) and pm.process_base.filename \
                                else (ctx.ff7_dir or Path("."))
                            exe_dir_p = Path(exe_dir)
                            # If the seed defined shop slots, (re)write shop_ap.txt
                            # with the correct cross-player names BEFORE injecting
                            # (the DLL reads the file once at load).
                            if ctx._shop_apff7_names or ctx._shop_apff7_materia_names:
                                _write_shop_ap_txt(exe_dir_p, ctx._shop_apff7_names,
                                                   ctx._shop_apff7_materia_names)
                            dll = exe_dir_p / "shophook.dll"
                            if inject_dll(pm, dll):
                                ctx._hook_injected = True
                            # Token ids to watch for purchases (from the same file
                            # the DLL displays; seed names if present, else manual).
                            ctx._shop_token_names = (
                                dict(ctx._shop_apff7_names) if ctx._shop_apff7_names
                                else _load_shop_overrides(exe_dir_p)
                            )
                            ctx._shop_materia_names = dict(ctx._shop_apff7_materia_names)
                            if ctx._shop_token_names or ctx._shop_materia_names:
                                logger.info(
                                    "Shop detection: tracking "
                                    f"{len(ctx._shop_token_names)} item + "
                                    f"{len(ctx._shop_materia_names)} materia token id(s)."
                                )
                        except Exception as e:
                            logger.debug(f"Shop hook injection skipped: {e}")

                    break
                except Exception:
                    pass
            if pm is None:
                log_once("Waiting for FF7_EN.exe … launch the game via 7th Heaven.")
                ctx.game_connected = False
                await asyncio.sleep(3)
                continue

        # ── Deliver queued items ──────────────────────────────────────────
        if ctx._pending_items:
            _deliver_items_to_game(pm, ctx)

        # ── Read savemap and check flags ──────────────────────────────────
        try:
            game_moment = pm.read_ushort(SAVEMAP_BASE + GAME_MOMENT_OFFSET)

            # ── Establish baseline once per game connection ───────────────
            # Free Roam starts at game moment 1603, so the savemap already looks
            # "late": some location detection bits are pre-set, and every boss
            # game-moment threshold is already met. Snapshot those as
            # pre-satisfied so we never report them as fresh checks (which would
            # wrongly hand items to other players the instant we connect). Only
            # 0->1 transitions AFTER this baseline count as real checks.
            if not ctx._baseline_established and ctx.biton_map:
                for code, (bank, address, bit) in ctx.biton_map.items():
                    if code in ctx.checked_locations:
                        continue
                    try:
                        if pm.read_uchar(_biton_byte_addr(bank, address)) & (1 << bit):
                            ctx._baseline_locations.add(code)
                    except Exception:
                        pass
                ctx._baseline_established = True
                if ctx._baseline_locations:
                    logger.info(
                        f"Baseline: suppressing {len(ctx._baseline_locations)} "
                        f"pre-set location flag(s) + already-passed boss checks "
                        f"(game moment {game_moment})."
                    )

            # ── Establish shop-token inventory baseline once ──────────────
            if not ctx._shop_baseline_established and (
                    ctx._shop_token_names or ctx._shop_materia_names):
                try:
                    counts = _read_all_item_counts(pm)
                except Exception:
                    counts = {}
                for ff7_id in ctx._shop_token_names:
                    ctx._shop_token_baseline[ff7_id] = counts.get(ff7_id, 0)
                try:
                    mcounts = _read_all_materia_counts(pm)
                except Exception:
                    mcounts = {}
                for mid in ctx._shop_materia_names:
                    ctx._shop_materia_baseline[mid] = mcounts.get(mid, 0)
                try:
                    ctx._prev_gil = pm.read_uint(SAVEMAP_BASE + GIL_OFFSET)
                except Exception:
                    ctx._prev_gil = -1
                ctx._shop_baseline_established = True

            newly_checked = []
            for code, (bank, address, bit) in ctx.biton_map.items():
                if (code in ctx.checked_locations
                        or code in ctx._checked_this_session
                        or code in ctx._baseline_locations):
                    continue
                byte_addr = _biton_byte_addr(bank, address)
                byte_val  = pm.read_uchar(byte_addr)
                if byte_val & (1 << bit):
                    newly_checked.append(code)
                    ctx._checked_this_session.add(code)

            if newly_checked and ctx.server and ctx.slot:
                await ctx.send_msgs([{"cmd": "LocationChecks", "locations": newly_checked}])
                for code in newly_checked:
                    logger.info(f"Checked location: {ctx.location_names.lookup_in_game(code)}")

            # ── Relocate any AP-delivered vehicle stranded at the (0,0) sea tile ─
            _place_stranded_vehicles(pm, ctx)

            # ── Drive the Northern Crater gate flag from received goal items ───
            _enforce_crater_lock(pm, ctx)

            # ── Free Roam: finish Ultimate Weapon once the player has engaged him ─
            if ctx.free_roam:
                _resolve_ultimate_weapon(pm)

            # ── Shop purchases: detect token buys, swap to Potion, fire checks ─
            shop_checks = _process_shop_purchases(pm, ctx)
            if shop_checks and ctx.server and ctx.slot:
                await ctx.send_msgs([{"cmd": "LocationChecks", "locations": shop_checks}])
                for code in shop_checks:
                    logger.info(f"Checked location: {ctx.location_names.lookup_in_game(code)}")

            # ── Check win condition ───────────────────────────────────────
            if not ctx.finished_game and ctx.server and ctx.slot:
                # Determine goal threshold based on victory condition
                if ctx.victory_condition == 1:  # escape_midgar
                    goal_threshold = MIDGAR_ESCAPE_MOMENT
                    goal_message = "Goal complete — Escaped from Midgar!"
                else:  # defeat_sephiroth (default)
                    goal_threshold = GAME_MOMENT_GOAL
                    goal_message = "Goal complete — Sephiroth defeated!"
                if game_moment >= goal_threshold:
                    ctx.finished_game = True
                    await ctx.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
                    logger.info(goal_message)

        except pymem.exception.ProcessError:
            logger.info("FF7 process lost — will reconnect.")
            pm = None
            ctx.game_connected = False
            ctx._checked_this_session.clear()
            ctx._boss_checks_sent.clear()
            ctx._baseline_established = False
            ctx._baseline_locations.clear()
            ctx._hook_injected = False
            ctx._shop_baseline_established = False
            ctx.pm = None
            await asyncio.sleep(3)
            continue
        except Exception as exc:
            logger.debug(f"FF7 memory read error: {exc}")
            pm = None
            ctx.game_connected = False
            ctx._boss_checks_sent.clear()
            ctx._baseline_established = False
            ctx._baseline_locations.clear()
            ctx._hook_injected = False
            ctx._shop_baseline_established = False
            ctx.pm = None
            await asyncio.sleep(3)
            continue

        await asyncio.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main(args: argparse.Namespace) -> None:
    ctx = FF7Context(args.connect, args.password)
    ctx.auth = args.name

    ctx.server_task = asyncio.ensure_future(server_loop(ctx))

    if gui_enabled:
        ctx.run_gui()
    ctx.run_cli()

    watcher_task = asyncio.create_task(game_watcher(ctx), name="FF7GameWatcher")

    await ctx.exit_event.wait()
    ctx.server_address = None

    await ctx.shutdown()
    watcher_task.cancel()


if __name__ == "__main__":
    import colorama
    colorama.init()

    parser = argparse.ArgumentParser(description="Final Fantasy VII Archipelago Client")
    parser.add_argument("connect",  nargs="?", help="Archipelago server address (host:port)")
    parser.add_argument("password", nargs="?", help="Server password")
    parser.add_argument("--name",   default=None, help="Slot / player name")
    parsed = parser.parse_args()

    asyncio.run(main(parsed))
