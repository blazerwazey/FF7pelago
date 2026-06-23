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
# Live savemap length (ff7-ultima reads 0x10F4). Every field-pickup BITON flag
# lives inside this region (max offset ~0x1057), so the detection scan reads ONE
# snapshot per poll and indexes it instead of doing a ReadProcessMemory syscall
# per location (~381/poll → 1/poll).
SAVEMAP_LEN        = 0x10F4

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
GAME_MOMENT_OFFSET = 0x0BA4   # uint16 LE "mprogress" Main Progress var
GAME_MOMENT_GOAL   = 3000     # (legacy; mprogress actually caps at 1999, see below)
# Defeat-Sephiroth goal detection. mprogress (game_moment) is NOT usable: it maxes
# at 1999 in the crater descent and never reaches a clean post-Sephiroth value, and
# the ending/credits are engine-driven (no ending field/flag to poll). Instead read
# the LIVE game-module global: FF7 switches it to Ending(25) then Credits(28) only
# after the final battle is won (a loss is GameOver=26, the intro is 27). Address +
# enum from ff7-ultima (maciej-trebacz) addresses.rs / types.ts; the map matches
# this build — its game_moment 0xDC08DC == our SAVEMAP_BASE(0xDBFD38)+0x0BA4.
GAME_MODULE_ADDR    = 0xCBF9DC  # live "current_module" byte
GAME_MODULE_FIELD   = 1
GAME_MODULE_BATTLE  = 2
GAME_MODULE_WORLD   = 3
GAME_MODULE_ENDING  = 25        # post-final-battle ending sequence
GAME_MODULE_GAMEOVER = 26
GAME_MODULE_CREDITS = 28        # staff roll

# Live battle formation index (u16). ff7-ultima "battle_id". Used to detect a
# won Ruby/Emerald battle and register the kill (their weapons_killed flags are
# set by post-battle world-script logic the Free Roam endgame skips, so a won
# fight leaves them un-flagged -> the AP check never fires and they respawn).
BATTLE_FORMATION_ADDR = 0x9AAD3C
# Weapon battle formation id -> weapons_killed bit mask (ff7-ultima ff7Battles.ts:
# 982/983 Ruby[Desert]=bit3 0x08, 984/985/986 Emerald[Underwater]=bit4 0x10).
# Diamond Weapon is NOT here — he is fully hidden in Free Roam (his world-map model
# never renders, so his ambient spawn is neutralized) and has no AP check.
# Ultimate Weapon is NOT here either: he flees rather than dying, so a battle win
# never happens — he is handled by _resolve_ultimate_weapon (engagement-based).
_WEAPON_BATTLE_FORMATIONS = {982: 0x08, 983: 0x08, 984: 0x10, 985: 0x10, 986: 0x10}
# Ultimate Weapon (Free Roam): his kill flag weapons_killed.bit[0] is set by his
# FINAL BATTLE (no wm0 model-11 function writes it). The chase that whittles his HP
# down to make that battle lethal is set up by the disc-2 intro, which Free Roam
# skips (jumps to moment 1603) — so he never goes down. Once the player has engaged
# him (submarine_flags.bit[3], set on the first ram), we finish him by setting
# weapons_killed.bit[0] (death + the "Defeat Ultimate Weapon" check). The Ancient
# Forest is separate — its entrance only needs the player on foot/chocobo.
WEAPONS_KILLED_OFFSET  = 0x0C1F  # byte: bit0 = killed, bit2 = HP < 20,000
SUBMARINE_FLAGS_OFFSET = 0x0F2A  # byte: bit3 = Ultimate Weapon chase started/engaged
# Current disc (ff7tk FF7SLOT.disc; live 0xDBFD38+0x0EA4 = 0xDC0BDC = ff7-ultima
# disc_id). Free Roam is endgame, so force disc 3. Not field-settable (fields use
# the DSKCG opcode, engine-handled), so the client writes it directly each poll.
DISC_OFFSET    = 0x0EA4
FREE_ROAM_DISC = 3

# Field "door"/gate story flags that would softlock Free Roam if left unset: at the
# Free Roam game moment the field shows a blocking model UNLESS the flag is ON (it's
# normally set by the story sequence the player skips). Forced ON each poll. Each
# entry is (savemap byte offset, bit). Field Var[bank][addr] -> savemap: banks map
# 1/2→0xBA4, 3/4→0xCA4, 11/12→0xDA4, 13/14→0xEA4, 7/15→0xFA4 (ff7-lib/ff7-ultima).
_FREE_ROAM_FORCE_FLAGS = [
    (0x1034, 0),   # mtcrl_2 DOOR — Var[15][144].0 (Mt. Corel gate; 0xFA4+0x90)
    # Icicle Inn (snow) "Snow area story flags" — Var[1][130] (0xBA4+0x82 = 0xC26).
    # Mark the one-time snow-area events done so the field skips the Shinra blockade
    # cutscene chain on Free Roam entry (complements the convil/snow field patches).
    (0x0C26, 0),   # #0 Man1: "It's dangerous!" handled
    (0x0C26, 3),   # #3 Elena punched Cloud
    (0x0C26, 4),   # #4 Cloud woke in Gast home
    (0x0C26, 7),   # #7 First time snowboarding
    # Junon "Junon area story flags" — Var[1][129] (0xBA4+0x81 = 0xC25). Force the
    # whole byte (all 8 bits = 0xFF) so the one-time Junon arrival sequence (Priscilla
    # CPR, climb-the-tower, top-of-pole, etc.) is marked done and won't re-trigger on
    # Free Roam entry.
    (0x0C25, 0),   # #0 Priscilla warnings given
    (0x0C25, 1),   # #1 Oldman: "Do CPR!"
    (0x0C25, 2),   # #2 Free rest offer made
    (0x0C25, 3),   # #3 Talk about black cape man
    (0x0C25, 4),   # #4 Priscilla: "Gets deeper..."
    (0x0C25, 5),   # #5 Tifa: "5 years ago"
    (0x0C25, 6),   # #6 Cloud: "Hey!" (climb tower)
    (0x0C25, 7),   # #7 Reached top of pole
    # Yuffie forest encounter flag — Var[1][133] (0xBA4+0x85 = 0xC29), LSB only.
    # Bit 0 ON = Yuffie can be encountered in forests (so she's recruitable in
    # Free Roam, where the story flag that normally enables it is skipped).
    (0x0C29, 0),   # bit 0 — "can Yuffie be found in forests"
    # Var[3][189] bit 4 (the "& 16" field-script gate) — bank 3 base 0xCA4 + 0xBD
    # = 0xD61. Forced ON so the gated branch (else goto label 1) is taken.
    (0x0D61, 4),   # Var[3][189] & 16
    # NOTE: Ruby Weapon's spawn (0xF2B.4) is NOT forced here anymore. Ruby's model
    # geometry only renders at world_progress 4, which the overworld init reaches
    # only after Ultimate is dead — so forcing his spawn early just produced an
    # invisible, collidable boss. His spawn + the wp-4 flags are now set together
    # in _resolve_ultimate_weapon once Ultimate is defeated (he then appears, drawn).
]
# Item-conditional field gates: set savemap <offset>.<bit> ONLY once <item> has
# been received (the field gate softlocks otherwise, but opening it without the
# item would break the AP logic). Read on field load, so re-asserted each poll.
_FREE_ROAM_ITEM_GATE_FLAGS = [
    ("Basement Key", 0x0C8C, 1),       # Shinra Mansion basement — Var[1][232].1 (0xBA4+0xE8)
    ("Leviathan Scales", 0x1031, 0),   # "has Leviathan Scales" prerequisite — Var[15][141].0
                                       # (0xFA4+0x8D). Field scripts gate their reward branches
                                       # on this being ON. NOT setting Var[15][137].* — those are
                                       # per-NPC "reward already given" bits, checked OFF, so
                                       # setting them would block the rewards.
    ("Glacier Map", 0x0C26, 6),        # Snow story flag #6 "Glacier Map key item" — Var[1][130].6
                                       # (0xBA4+0x82 = 0xC26). Set only once the AP Glacier Map is
                                       # received, so the Great Glacier map/navigation works.
    ("Submarine", 0x0EF4, 3),          # "Gray submarine" OWNED flag — bank-13 byte (0xEA4+0x50 =
                                       # 0xEF4), bit 3 (0x08). This is what makes the world map spawn
                                       # the parked submarine model (next to Junon, via the sub_world
                                       # coord). VEHICLE_ITEM_FLAGS only sets tut_sub (0xC1E.2 = skips
                                       # the tutorial / grants access) — without the owned flag the
                                       # submarine never appears. Re-asserted each poll so it spawns on
                                       # the next world-map entry after receipt. ("Red submarine" 0xEF6.2
                                       # is the enemy sub, not set here.)
]
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
) -> Tuple[Dict[int, int], Dict[int, int], Dict[int, str], Dict[int, str],
           Dict[int, str], Dict[int, str]]:
    """From the .apff7 ``shops`` array build, split by token space:
        (item_token->loc, materia_token->loc, item_token->name, materia_token->name,
         item_token->desc, materia_token->desc)
    Item-space tokens are composite item ids (consumable/weapon/armor/accessory,
    all detected in the item inventory); materia tokens live in the materia
    inventory. Display name format ``A <Item> @ <Owner>`` (the leading "A " marks
    it as an Archipelago slot), trimmed to fit the shop grid. The description is
    ``An Archipelago Item for <Owner>`` (shown in the shop info pane)."""
    item_loc: Dict[int, int] = {}
    mat_loc: Dict[int, int] = {}
    item_names: Dict[int, str] = {}
    mat_names: Dict[int, str] = {}
    item_descs: Dict[int, str] = {}
    mat_descs: Dict[int, str] = {}
    for s in shops:
        token = s.get("token_id")
        loc   = s.get("location_id")
        if token is None or loc is None:
            continue
        item  = (s.get("item") or "AP Item").strip()
        owner = (s.get("item_owner") or "").strip()
        name  = (f"A {item} @ {owner}" if owner else f"A {item}")[:30]
        desc  = (f"An Archipelago Item for {owner}" if owner else "An Archipelago Item")
        if s.get("token_type", "item") == "materia":
            mat_loc[int(token)] = int(loc)
            mat_names[int(token)] = name
            mat_descs[int(token)] = desc
        else:
            item_loc[int(token)] = int(loc)
            item_names[int(token)] = name
            item_descs[int(token)] = desc
    return item_loc, mat_loc, item_names, mat_names, item_descs, mat_descs


def _write_shop_ap_txt(
    exe_dir: Path, item_names: Dict[int, str], materia_names: Dict[int, str],
    item_descs: Optional[Dict[int, str]] = None,
    materia_descs: Optional[Dict[int, str]] = None,
) -> None:
    """Write shop_ap.txt (read by shophook.dll). Format:
    ``<section>:<index>=<name>[|<description>]`` so the hook can override
    item/weapon/materia names (a3=8) and descriptions (a3=0) alike. (Legacy
    ``<id>=name`` lines are still accepted by the hook and treated as section 4.)"""
    item_descs = item_descs or {}
    materia_descs = materia_descs or {}
    try:
        lines = ["# Auto-generated from the .apff7 shop placements.\n"]
        for tok, name in sorted(item_names.items()):
            sec, idx = _token_section_index("item", tok)
            desc = item_descs.get(tok)
            lines.append(f"{sec}:{idx}={name}|{desc}\n" if desc else f"{sec}:{idx}={name}\n")
        for tok, name in sorted(materia_names.items()):
            sec, idx = _token_section_index("materia", tok)
            desc = materia_descs.get(tok)
            lines.append(f"{sec}:{idx}={name}|{desc}\n" if desc else f"{sec}:{idx}={name}\n")
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

    def _cmd_debug(self, value: str = "") -> bool:
        """Enable/disable the FF7 debug commands (off by default). With no arg it
        toggles; or pass on/off. The debug commands (/wdump /setwp /weapons
        /rewards /mapbitons) are diagnostics — some write game memory — so they
        do nothing until enabled here."""
        v = value.strip().lower()
        if v in ("on", "1", "true", "yes"):
            self.ctx.debug = True
        elif v in ("off", "0", "false", "no"):
            self.ctx.debug = False
        else:
            self.ctx.debug = not self.ctx.debug
        logger.info(f"FF7 debug commands {'ENABLED' if self.ctx.debug else 'disabled'}.")
        return True

    def _require_debug(self) -> bool:
        """Gate for debug commands. Returns True if enabled; else prints a hint."""
        if not self.ctx.debug:
            logger.warning("Debug commands are off. Run /debug to enable them.")
            return False
        return True

    def _cmd_setjson(self, path: str = "") -> bool:
        """Point the client at the Archipelago FF7 file for this seed.
        Usage: /setjson <path_to_AP_seed_Pslot_player.apff7>
        The file is produced by Archipelago at generation time and contains
        pre-computed BITON coordinates for every location.
        """
        if not path.strip():
            logger.warning(
                "Usage: /setjson <path>  "
                "(e.g. /setjson C:/AP/AP_MySeed_P1_Cloud.apff7)"
            )
            return False

        json_path = Path(path.strip())
        if not json_path.exists():
            logger.warning(f"JSON file not found: {json_path}")
            return False

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            biton_map = _biton_map_from_placements(data.get("placements", []))
            item_loc, mat_loc, item_names, mat_names, item_descs, mat_descs = \
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
        self.ctx._shop_apff7_descs = item_descs
        self.ctx._shop_apff7_materia_descs = mat_descs
        if item_loc or mat_loc:
            logger.debug(f"Shop slots loaded: {len(item_loc) + len(mat_loc)} AP shop check(s).")

        settings = _load_settings()
        settings["json_path"] = str(json_path)
        _save_settings(settings)

        logger.debug(
            f"JSON loaded: {json_path.name}  "
            f"({len(biton_map)} locations tracked)"
        )
        return True

    def _cmd_wdump(self, model: str = "") -> bool:
        """[Debug] Dump live world-map state. No arg: player pos + entity list.
        With a model id (e.g. /wdump 5): hex-dump that entity's raw bytes so a
        broken (invisible) vehicle can be diffed against a known-good save.
        """
        if not self._require_debug():
            return True
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
        if not self._require_debug():
            return True
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

    def _cmd_weapons(self) -> bool:
        """[Debug] Dump weapon-boss state: weapons_killed bits, submarine_flags,
        the Ruby spawn flag, the live game module, and (while in a battle) the
        formation id. Fight Ruby/Emerald and run this to confirm the kill flag is
        set and to read the real formation id if a kill isn't registering."""
        if not self._require_debug():
            return True
        pm = getattr(self.ctx, "pm", None)
        if pm is None:
            logger.warning("Not attached to FF7 — open the game first.")
            return False
        try:
            wk = pm.read_uchar(SAVEMAP_BASE + WEAPONS_KILLED_OFFSET)
            sf = pm.read_uchar(SAVEMAP_BASE + SUBMARINE_FLAGS_OFFSET)
            ruby_spawn = pm.read_uchar(SAVEMAP_BASE + 0x0F2B)
            module = pm.read_uchar(GAME_MODULE_ADDR)
            logger.info(
                f"[weapons] weapons_killed=0x{wk:02x}  "
                f"Ultimate={'Y' if wk & 0x01 else 'N'} "
                f"Ruby={'Y' if wk & 0x08 else 'N'} "
                f"Emerald={'Y' if wk & 0x10 else 'N'}"
            )
            logger.info(
                f"[weapons] submarine_flags=0x{sf:02x} (Ultimate-engaged bit3="
                f"{'Y' if sf & 0x08 else 'N'}); Ruby-spawn 0xF2B.4="
                f"{'Y' if ruby_spawn & 0x10 else 'N'}; game_module={module}; "
                f"pending_kill=0x{self.ctx._weapon_kill_pending:02x}"
            )
            if module == GAME_MODULE_BATTLE:
                formation = pm.read_ushort(BATTLE_FORMATION_ADDR)
                logger.info(
                    f"[weapons] IN BATTLE — formation id = {formation} "
                    f"(expect Ruby=982/983, Emerald=984/985/986)"
                )
        except Exception as exc:
            logger.warning(f"[weapons] failed: {exc}")
        return True

    def _cmd_rewards(self) -> bool:
        """[Debug] Diagnose the EXP/Gil/AP battle multipliers: the values from
        slot_data, whether the exe patch sites match the expected build, and the
        live bytes there. Run this if the multipliers seem to do nothing. It
        re-applies the patch when the site matches."""
        if not self._require_debug():
            return True
        ctx = self.ctx
        pm = getattr(ctx, "pm", None)
        if pm is None:
            logger.warning("Not attached to FF7 — open the game first.")
            return False
        try:
            logger.info(
                f"[rewards] slot_data multipliers: EXP x{ctx.exp_multiplier}, "
                f"Gil x{ctx.gil_multiplier}, AP x{ctx.ap_multiplier} "
                f"(applied={ctx._reward_mult_applied})"
            )
            if ctx.exp_multiplier <= 1 and ctx.gil_multiplier <= 1 and ctx.ap_multiplier <= 1:
                logger.warning(
                    "[rewards] All multipliers are 1 — nothing to apply. Set "
                    "exp/gil/ap_multiplier in your YAML and REGENERATE the seed "
                    "(the values travel in slot_data, so an old seed keeps the old values)."
                )
            exp = bytes(pm.read_bytes(_REWARD_EXP_ADDR, 8))
            gil = bytes(pm.read_bytes(_REWARD_GIL_ADDR, 8))
            ap = bytes(pm.read_bytes(_REWARD_AP_ADDR, 8))
            exp_full = bytes(pm.read_bytes(_REWARD_EXP_ADDR, len(_REWARD_EXP_ORIG_2013)))
            match = exp[:6] == _REWARD_EXP_ANCHOR or exp_full == _REWARD_EXP_ORIG_2013
            logger.info(f"[rewards] EXP @0x{_REWARD_EXP_ADDR:X}: {exp.hex(' ')}  "
                        f"anchor={'MATCH' if match else 'MISMATCH'}")
            logger.info(f"[rewards] Gil @0x{_REWARD_GIL_ADDR:X}: {gil.hex(' ')}")
            logger.info(f"[rewards] AP  @0x{_REWARD_AP_ADDR:X}: {ap.hex(' ')}")
            if match:
                ctx._reward_mult_applied = False
                _apply_reward_multipliers(pm, ctx)
                logger.info("[rewards] re-applied. After a '6b c9'/'6b c0'/'6b d2' the next "
                            "byte is the multiplier (hex) — confirm it matches your YAML.")
            else:
                logger.warning(
                    "[rewards] EXP patch site does NOT match this exe build, so the "
                    "multipliers can't be applied (classic Steam ff7_en.exe is expected; "
                    "a 2026 re-release / 7th-Heaven / Hext-modded exe shifts or rewrites "
                    "this code). Paste the window below + your FF7 version to get a patch:"
                )
                base = 0x431500
                window = bytes(pm.read_bytes(base, 0xA0))
                for i in range(0, len(window), 16):
                    logger.warning(f"  0x{base + i:X}: {window[i:i + 16].hex(' ')}")
        except Exception as exc:
            logger.warning(f"[rewards] failed: {exc}")
        return True

    def _cmd_mapbitons(self, path: str = "") -> bool:
        """[Debug] Scan flevel.lgp and rebuild the BITON map.
        Usage: /mapbitons [ff7_install_dir]
        Prefer /setjson in normal use — this is a fallback for debugging.
        """
        if not self._require_debug():
            return True
        from worlds.ff7.biton_mapper import build_biton_map, find_ff7_dir

        ff7_dir: Optional[Path] = None
        if path.strip():
            ff7_dir = Path(path.strip())
        elif self.ctx.ff7_dir:
            ff7_dir = self.ctx.ff7_dir
        else:
            ff7_dir = find_ff7_dir()
            if ff7_dir:
                logger.debug(f"Auto-detected FF7 dir: {ff7_dir}")

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
        # Debug/diagnostic commands are gated behind this (default off) so players
        # don't trip them by accident — some write game memory. Toggle with /debug.
        self.debug: bool = False
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
        # Weapon-boss kill latched from a battle formation, applied to
        # weapons_killed once the player exits the battle to gameplay.
        self._weapon_kill_pending: int = 0
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
        # Battle reward multipliers (from slot_data) + whether the exe patch ran.
        self.exp_multiplier: int = 1
        self.gil_multiplier: int = 1
        self.ap_multiplier: int = 1
        self._reward_mult_applied: bool = False
        # Free Roam mode (from slot_data) — gates Free-Roam-only savemap fixups.
        self.free_roam: bool = False
        # ── Shop-purchase detection (Tier-3 native-grid AP shops) ────────────
        # {ff7_item_id: location_code} for shop-slot "token" items. Buying the
        # token (sold by Gold Saucer's shop Hext, displayed with the AP name by
        # shophook.dll) fires that location; the DLL suppresses the inventory
        # grant and signals the purchase via shop_buys.txt.
        self.shop_token_to_location: Dict[int, int] = {}
        # Materia-space tokens (slot type 1) signal with section 13.
        self.shop_materia_to_location: Dict[int, int] = {}
        # token_id -> display name, parsed from the .apff7 shops array (used to
        # (re)write shop_ap.txt with the correct cross-player names at attach).
        self._shop_apff7_names: Dict[int, str] = {}
        self._shop_apff7_materia_names: Dict[int, str] = {}
        # token_id -> per-owner description ("An Archipelago Item for <Owner>"),
        # written into shop_ap.txt so the DLL overrides the shop info-pane text.
        self._shop_apff7_descs: Dict[int, str] = {}
        self._shop_apff7_materia_descs: Dict[int, str] = {}
        # Path to shophook.dll's shop_buys.txt purchase-signal file (set at the
        # exe dir when the hook is injected); polled + consumed each game tick.
        self._shop_buys_path: Optional[Path] = None

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
            sd = args.get("slot_data", {})
            self.exp_multiplier = max(1, int(sd.get("exp_multiplier", 1)))
            self.gil_multiplier = max(1, int(sd.get("gil_multiplier", 1)))
            self.ap_multiplier  = max(1, int(sd.get("ap_multiplier", 1)))
            self._reward_mult_applied = False
            raw = args.get("slot_data", {}).get("biton_map", {})
            if raw:
                self.biton_map = {int(k): tuple(v) for k, v in raw.items()}
                logger.debug(
                    f"BITON map received from server: {len(self.biton_map)} locations tracked."
                )
            else:
                self._load_biton_map_from_json()
            # Shop slots: prefer the server (slot_data) so no .apff7/`/setjson`
            # is needed; fall back to the .apff7 file only if not present.
            raw_shops = args.get("slot_data", {}).get("shops", [])
            if raw_shops:
                self.shop_token_to_location, self.shop_materia_to_location, \
                    self._shop_apff7_names, self._shop_apff7_materia_names, \
                    self._shop_apff7_descs, self._shop_apff7_materia_descs = \
                    _shops_from_apff7(raw_shops)
                if self.shop_token_to_location or self.shop_materia_to_location:
                    logger.debug(
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
            logger.debug(
                "No Archipelago JSON path configured — BITON tracking disabled.  "
                "Run /setjson <path_to_FF7_seed_P1.json> to enable it."
            )
            return
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.biton_map = _biton_map_from_placements(data.get("placements", []))
            self.shop_token_to_location, self.shop_materia_to_location, \
                self._shop_apff7_names, self._shop_apff7_materia_names, \
                self._shop_apff7_descs, self._shop_apff7_materia_descs = \
                _shops_from_apff7(data.get("shops", []))
            logger.debug(
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
                self._shop_apff7_names, self._shop_apff7_materia_names, \
                self._shop_apff7_descs, self._shop_apff7_materia_descs = \
                _shops_from_apff7(data.get("shops", []))
            if self.shop_token_to_location:
                logger.debug(
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
                logger.debug(
                    "Wall Market side-effect: delivered Pharmacy Coupon "
                    "(mktpb old man bypassed by remote dress delivery)"
                )
            pm.write_uchar(var_addr, current | (1 << _MKTPB_OLD_MAN_DONE_BIT))
            logger.debug("Wall Market side-effect: mktpb old man marked as processed")
    except Exception as exc:
        logger.debug(f"Wall Market side-effect failed: {exc}")


# Sector 5 walkmesh gate side-effect.
# Entry to Midgar runs through field mds5_5, whose script keeps the passage open
# only while the "owns Key to Sector 5" flag is ON:
#     If Var[15][38] bitOFF 3  ->  Deactivate the triangle #4   (blocks the way)
# So that flag — not the AP-internal Key-to-Sector-5 inventory bit (bank-1 0x43.5)
# that delivery also sets — is what must be ON. Var[15][38] is a savemap bit.
# The field-script savemap banks (authoritative ff7-lib/ff7-ultima map: bank pairs
# 1/2,3/4 -> 0xBA4,0xCA4; 11/12 -> 0xDA4; 13/14 -> 0xEA4; 7/15 -> 0xFA4; 5/6 = temp)
# put bank 15 at region 0xFA4, so Var[15][38] = 0xFA4 + 0x26 = 0x0FCA, bit 3.
# (Earlier 0x10CA was wrong — there is no 0x10A4 savemap region; bank 6 is temp.)
_SECTOR5_GATE_OFFSET = 0x0FCA   # FF7SLOT offset (live addr = SAVEMAP_BASE + this)
_SECTOR5_GATE_BIT    = 3        # bit 3 (mask 0x08)


def _ensure_sector5_walkmesh_gate(pm: "pymem.Pymem") -> None:
    """Open the mds5_5 walkmesh passage into Midgar (Free Roam) by setting the
    Key-to-Sector-5 possession flag Var[15][38].3 the field script gates on."""
    try:
        addr = SAVEMAP_BASE + _SECTOR5_GATE_OFFSET
        current = pm.read_uchar(addr)
        if not (current & (1 << _SECTOR5_GATE_BIT)):
            pm.write_uchar(addr, current | (1 << _SECTOR5_GATE_BIT))
            logger.debug(
                "Sector 5 side-effect: set mds5_5 walkmesh gate flag "
                f"(0x{_SECTOR5_GATE_OFFSET:04X} bit {_SECTOR5_GATE_BIT})"
            )
    except Exception as exc:
        logger.debug(f"Sector 5 walkmesh-gate side-effect failed: {exc}")


# Snow-area "Snowboard key item obtained" story flag. Ultima Bank 1 (field bank
# pair 1/2 = savemap region 0 @0xBA4), address #130 (0x82), bit 1 → savemap
# 0xBA4 + 0x82 = 0xC26, bit 1. The AP-internal Snowboard inventory bit (bank-1
# 0x46.2) isn't this story flag, so set it explicitly on Snowboard delivery.
_SNOWBOARD_FLAG_OFFSET = 0x0C26
_SNOWBOARD_FLAG_BIT    = 1


def _ensure_snowboard_flag(pm: "pymem.Pymem") -> None:
    """Set the 'Snowboard key item obtained' story flag (Bank 1 #130 bit 1)."""
    try:
        addr = SAVEMAP_BASE + _SNOWBOARD_FLAG_OFFSET
        current = pm.read_uchar(addr)
        if not (current & (1 << _SNOWBOARD_FLAG_BIT)):
            pm.write_uchar(addr, current | (1 << _SNOWBOARD_FLAG_BIT))
            logger.debug(
                "Snowboard side-effect: set 'Snowboard key item obtained' flag "
                f"(0x{_SNOWBOARD_FLAG_OFFSET:04X} bit {_SNOWBOARD_FLAG_BIT})"
            )
    except Exception as exc:
        logger.debug(f"Snowboard flag side-effect failed: {exc}")


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
        logger.debug(f"Delivered key item: {item_name}")
        if item_name in _DRESS_ITEMS:
            _ensure_mktpb_old_man_processed(pm)
        if item_name == "Key to Sector 5":
            _ensure_sector5_walkmesh_gate(pm)
        if item_name == "Snowboard":
            _ensure_snowboard_flag(pm)
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
        logger.debug(f"Relocated vehicle(s) model_id={sorted(placed)} to spawn coords")


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
                logger.debug("Northern Crater unlocked — all goal items received.")
    except Exception as exc:
        logger.debug(f"crater lock write failed: {exc}")


def _resolve_ultimate_weapon(pm: "pymem.Pymem") -> None:
    """Finish Ultimate Weapon in Free Roam AND advance to the post-Ultimate world
    state so Ruby Weapon actually RENDERS.

    His kill flag (weapons_killed.bit[0]) is set by his final battle, which never
    becomes lethal because the disc-2 chase is skipped — so once the player has
    engaged him (submarine_flags.bit[3], set on the first ram) we set bit[0] (death
    + the AP check). Ruby is gated by the overworld's world_progress: it
    only reaches 4 ("after Ultimate killed") when weapons_killed.bit0 AND 0xF2B.0
    AND submarine_flags.bit4 are all set, and the boss model GEOMETRY for Ruby only
    loads at world_progress 4 (at 3 he's an invisible-but-collidable entity). A real
    Ultimate kill sets all three; the engagement shortcut set only bit0, leaving wp
    at 3 and Ruby invisible. So on resolving Ultimate we also set 0xF2A.4, 0xF2B.0,
    and 0xF2B.4 (Ruby's spawn bit) — the full post-Ultimate state. No-op until
    engaged."""
    try:
        wk_addr = SAVEMAP_BASE + WEAPONS_KILLED_OFFSET
        sf_addr = SAVEMAP_BASE + SUBMARINE_FLAGS_OFFSET
        wk = pm.read_uchar(wk_addr)
        sf = pm.read_uchar(sf_addr)
        if not (wk & 0x01):                       # not yet defeated
            if not (sf & 0x08):                   # bit3 — not engaged yet
                return
            pm.write_uchar(wk_addr, wk | 0x01)    # engaged → mark defeated
            logger.debug("Ultimate Weapon defeated (Free Roam) — weapons_killed.bit[0] set.")
        # Ultimate down: assert the post-Ultimate state so world_progress hits 4 and
        # Ruby's model is drawn (he's invisible at wp3). Re-checked each poll so it
        # self-heals across overworld reloads.
        if not (sf & 0x10):                       # submarine_flags.bit4
            pm.write_uchar(sf_addr, sf | 0x10)
        f2b_addr = SAVEMAP_BASE + 0x0F2B
        f2b = pm.read_uchar(f2b_addr)
        if (f2b & 0x11) != 0x11:                  # 0xF2B.0 (wp4 cond) + 0xF2B.4 (Ruby spawn)
            pm.write_uchar(f2b_addr, f2b | 0x11)
            logger.debug("Post-Ultimate world state set — Ruby Weapon should now render.")
        # NOTE: Diamond Weapon is fully hidden in Free Roam (his world-map model
        # never renders even at wp4), so his ambient spawn is neutralized in
        # wm0.ev and nothing here touches his 0xEF6.3 flag.
    except Exception as exc:
        logger.debug(f"resolve ultimate weapon failed: {exc}")


def _resolve_weapon_battles(ctx, pm: "pymem.Pymem") -> None:
    """Register Ruby/Emerald Weapon kills in Free Roam by watching battles.

    Their defeat flags (weapons_killed bit3=Ruby, bit4=Emerald) are set by
    post-battle world-script logic that the Free Roam endgame state skips, so a
    WON fight leaves the flag clear: the AP check never fires and the weapon
    keeps respawning. We watch the live game module + battle formation id; while
    the player is in a Ruby/Emerald battle we latch the kill, and once they
    return to gameplay (World/Field, i.e. they won — not a Game Over) we set the
    bit. Acts ONLY on the exact weapon formation ids, so a wrong/garbage
    formation read can never false-trigger a kill."""
    try:
        module = pm.read_uchar(GAME_MODULE_ADDR)
        if module == GAME_MODULE_BATTLE:
            formation = pm.read_ushort(BATTLE_FORMATION_ADDR)
            mask = _WEAPON_BATTLE_FORMATIONS.get(formation)
            if mask:
                ctx._weapon_kill_pending |= mask
            return
        if not ctx._weapon_kill_pending:
            return
        if module == GAME_MODULE_GAMEOVER:
            ctx._weapon_kill_pending = 0          # player lost — not a kill
            return
        if module in (GAME_MODULE_WORLD, GAME_MODULE_FIELD):
            wk_addr = SAVEMAP_BASE + WEAPONS_KILLED_OFFSET
            wk = pm.read_uchar(wk_addr)
            new = wk | ctx._weapon_kill_pending
            if new != wk:
                names = []
                if ctx._weapon_kill_pending & 0x08:
                    names.append("Ruby")
                if ctx._weapon_kill_pending & 0x10:
                    names.append("Emerald")
                pm.write_uchar(wk_addr, new)
                logger.debug(
                    f"{'/'.join(names)} Weapon defeat registered (Free Roam) — "
                    f"weapons_killed 0x{wk:02x} -> 0x{new:02x}."
                )
            ctx._weapon_kill_pending = 0
    except Exception as exc:
        logger.debug(f"resolve weapon battles failed: {exc}")


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
        logger.debug(f"Vehicle unlocked: {item_name} (addr=0x{bank1_addr:02X} mask=0x{mask:02X})")
        return True
    except Exception as exc:
        logger.debug(f"Vehicle flag write failed for '{item_name}': {exc}")
        return False


# ── Green Chocobo delivery (Free Roam: cross the Junon-area mountain crater) ───
# In Free Roam the only land route to Junon is blocked by the "Junon Area crater"
# world-map alternative (mountain terrain).  A mountain-capable (green) chocobo
# crosses it.  Only a *stabled, bred* chocobo carries the green colour, so we
# write a coloured FF7CHOCOBO record into the next free Chocobo Farm stable slot
# (foot-reachable from Kalm) and set the stable bookkeeping so Choco Billy will
# release it.  Each AP chocobo colour gets its own slot (no overwrite).
# All offsets are FF7SLOT offsets (live addr = SAVEMAP_BASE + offset), verified
# from ff7tk FF7Save_Types.h / Type_FF7CHOCOBO.h.
_CHOCO_SLOT0      = 0x0DC4  # FF7CHOCOBO chocobos[0] (16 bytes each, 6 slots)
_CHOCO_STABLES    = 0x0CFC  # qty of stables owned
_CHOCO_OCCUPIED   = 0x0CFD  # qty of occupied stables
_CHOCO_MASK       = 0x0CFF  # bitmask of occupied stable slots (bit 0 = slot 1)
_CHOCO_RATING0    = 0x0E3E  # stablechocorating[0] (1=Wonderful .. 8=Worst)
_CHOCO_NAME0      = 0x0EC4  # chocobonames[0][6] (FF7 text, 0xFF-terminated)
_CHOCO_STAMINA0   = 0x0EE8  # chocostaminas[0] (u16)
_CHOCO_MAX_SLOTS  = 6       # FF7 stable holds up to 6 chocobos
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
    """Place a bred chocobo of the given colour into the next free Chocobo Farm
    stable slot.

    Each AP chocobo colour is a distinct, one-time item, so we add it to the next
    empty stable slot rather than overwriting slot 0. Idempotent per colour: if a
    chocobo of this colour is already stabled, do nothing (so re-delivery on
    reconnect can't stack duplicates).
    """
    type_byte = _CHOCO_TYPES.get(item_name)
    if type_byte is None:
        return False
    try:
        base = SAVEMAP_BASE
        mask = pm.read_uchar(base + _CHOCO_MASK)
        # Idempotent: skip if any occupied slot already holds this colour.
        free_slot = -1
        for n in range(_CHOCO_MAX_SLOTS):
            if (mask >> n) & 1:
                if pm.read_uchar(base + _CHOCO_SLOT0 + n * 16 + 0x0F) == type_byte:
                    return True
            elif free_slot < 0:
                free_slot = n
        if free_slot < 0:
            logger.warning(
                f"Chocobo Farm stable full ({_CHOCO_MAX_SLOTS} slots) — "
                f"cannot deliver {item_name}"
            )
            return False

        rec = base + _CHOCO_SLOT0 + free_slot * 16
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
        # Per-chocobo extras (parallel arrays, indexed by slot).
        pm.write_ushort(base + _CHOCO_STAMINA0 + free_slot * 2, 1000)
        pm.write_uchar (base + _CHOCO_RATING0 + free_slot, 1)        # Wonderful
        for i in range(6):                                          # empty (default) name
            pm.write_uchar(base + _CHOCO_NAME0 + free_slot * 6 + i, 0xFF)
        # Stable bookkeeping: mark this slot occupied; own ≥ occupied stables.
        mask |= (1 << free_slot)
        pm.write_uchar(base + _CHOCO_MASK, mask)
        occupied = bin(mask).count("1")
        pm.write_uchar(base + _CHOCO_OCCUPIED, occupied)
        if pm.read_uchar(base + _CHOCO_STABLES) < occupied:
            pm.write_uchar(base + _CHOCO_STABLES, occupied)
        logger.debug(f"Delivered {item_name} to Chocobo Farm stable slot {free_slot + 1}")
        return True
    except Exception as exc:
        logger.debug(f"Chocobo delivery failed for {item_name}: {exc}")
        return False


# ── Party member delivery (Free Roam: unlock optional characters) ─────────────
# Savemap char roster order: Cloud,Barret,Tifa,Aerith,RedXIII,Yuffie,CaitSith,
# Vincent,Cid -> ids 0..8. PHS availability is a per-id bitmask.
_CHARACTER_IDS = {"Barret": 1, "Tifa": 2, "Aerith": 3, "Red XIII": 4, "Cait Sith": 6, "Cid": 8}
_PARTY_OFFSET       = 0x04F8   # qint8 party[3] — active party member ids
# PHS bitmasks (per character id). 0x10A4 is the LOCK mask (ff7-ultima
# party_locking_mask): a SET bit forces the member in place / blocks swapping.
# 0x10A6 is the visibility/availability mask. A swappable member needs its
# visibility bit SET and its lock bit CLEAR.
_PHS_LOCK_OFFSET    = 0x10A4   # quint16 — who is LOCKED (un-swappable) in the PHS
_PHS_VISIBLE_OFFSET = 0x10A6   # quint16 — who is visible/available in the PHS
_CHARS_OFFSET       = 0x0054   # FF7CHAR chars[9]
_CHAR_RECORD_SIZE   = 132      # bytes per character record (FF7CHAR)

# Default in-game names per character id (FF7's initial-data names).
_CHAR_DEFAULT_NAMES = {
    1: "Barret", 2: "Tifa", 3: "Aeris", 4: "Red XIII", 6: "Cait Sith", 8: "Cid",
}
# First (default) weapon index per character id — the byte stored in FF7CHAR
# +0x1C. Each character may only equip weapons in their own range, so a delivered
# character must hold one of theirs (from FF7-exe-Editor GameData.cs WeaponData).
_CHAR_DEFAULT_WEAPONS = {
    1: 0x20,  # Barret  — Gatling Gun
    2: 0x10,  # Tifa    — Leather Glove
    3: 0x3E,  # Aeris   — Guard Stick
    4: 0x30,  # Red XIII— Mythril Clip
    6: 0x65,  # Cait Sith — Yellow M-phone
    8: 0x49,  # Cid     — Spear
}
# FF7CHAR field offsets used during record initialisation.
_CHR_ID = 0x00; _CHR_LEVEL = 0x01; _CHR_NAME = 0x10
_CHR_WEAPON = 0x1C; _CHR_ARMOR = 0x1D; _CHR_ACCESSORY = 0x1E
_CHR_STATUS = 0x1F; _CHR_ROW = 0x20
_CHR_CURHP = 0x2C; _CHR_BASEHP = 0x2E; _CHR_CURMP = 0x30; _CHR_BASEMP = 0x32
_CHR_MAXHP = 0x38; _CHR_MAXMP = 0x3A; _CHR_MATERIA = 0x40  # 16 × 4 bytes


def _encode_ff7_name(name: str, width: int = 12) -> bytes:
    """Encode an ASCII name into FF7's menu/kernel charmap (ASCII - 0x20),
    0xFF-terminated and 0xFF-padded to `width` bytes."""
    out = bytearray()
    for c in name[:width - 1]:
        b = ord(c)
        out.append((b - 0x20) & 0xFF if 0x20 <= b <= 0x7E else 0x00)
    out.append(0xFF)                       # terminator
    out += b"\xFF" * (width - len(out))    # pad
    return bytes(out[:width])


def _init_character_record(pm: "pymem.Pymem", cid: int) -> None:
    """Seed an uninitialised character record so a delivered party member is
    playable. Optional characters (Cait Sith, Cid …) never get their join-event
    record in Free Roam, so their savemap slot reads all-zero — which the engine
    treats as id 0 ("Cloud") with 0 max HP (instant death). We clone Cloud's
    record (guaranteed valid, level/stat-consistent) and retarget it: own id,
    name and first weapon, no armor/accessory, empty materia, and HP/MP collapsed
    to the unequipped base so the values stay self-consistent."""
    chars = SAVEMAP_BASE + _CHARS_OFFSET
    rec = bytearray(pm.read_bytes(chars, _CHAR_RECORD_SIZE))  # Cloud (slot 0)
    rec[_CHR_ID] = cid
    rec[_CHR_NAME:_CHR_NAME + 12] = _encode_ff7_name(
        _CHAR_DEFAULT_NAMES.get(cid, "AP Char"))
    rec[_CHR_WEAPON] = _CHAR_DEFAULT_WEAPONS.get(cid, 0x00)
    rec[_CHR_ARMOR] = 0xFF          # no armor
    rec[_CHR_ACCESSORY] = 0xFF      # no accessory
    rec[_CHR_STATUS] = 0x00         # normal (clear sadness/fury)
    rec[_CHR_ROW] = 0x01            # front row
    rec[_CHR_MATERIA:_CHR_MATERIA + 16 * 4] = b"\xFF" * (16 * 4)  # empty slots
    # With equipment/materia stripped, max == base; keep HP/MP consistent & alive.
    base_hp = int.from_bytes(rec[_CHR_BASEHP:_CHR_BASEHP + 2], "little") or 1
    base_mp = int.from_bytes(rec[_CHR_BASEMP:_CHR_BASEMP + 2], "little")
    for off in (_CHR_CURHP, _CHR_MAXHP):
        rec[off:off + 2] = base_hp.to_bytes(2, "little")
    for off in (_CHR_CURMP, _CHR_MAXMP):
        rec[off:off + 2] = base_mp.to_bytes(2, "little")
    pm.write_bytes(chars + cid * _CHAR_RECORD_SIZE, bytes(rec), _CHAR_RECORD_SIZE)


def _ensure_character_record(pm: "pymem.Pymem", cid: int) -> bool:
    """(Re)seed an optional character's record if it is uninitialised/invalid.
    A bad slot reads as id 0 ("Cloud") and/or max HP 0 (instant death). We re-init
    when ANY of: max HP == 0, level == 0, or the id byte != cid — the earlier
    level-only check missed stubs that have a nonzero level but zero HP/wrong id.
    A validly-progressed record (id==cid, level>0, maxHP>0) is left untouched.
    Returns True if it (re)initialised."""
    rec_base = SAVEMAP_BASE + _CHARS_OFFSET + cid * _CHAR_RECORD_SIZE
    try:
        level = pm.read_uchar(rec_base + _CHR_LEVEL)
        maxhp = pm.read_ushort(rec_base + _CHR_MAXHP)
        id_byte = pm.read_uchar(rec_base + _CHR_ID)
        if maxhp == 0 or level == 0 or id_byte != cid:
            _init_character_record(pm, cid)
            return True
    except Exception as exc:
        logger.debug(f"character record check failed for cid {cid}: {exc}")
    return False


def _deliver_character(pm: "pymem.Pymem", char_name: str) -> bool:
    """Unlock an optional party member: make them available in the PHS, and drop
    them into an empty active party slot if one is free."""
    cid = _CHARACTER_IDS.get(char_name)
    if cid is None:
        return False
    try:
        bit = 1 << cid
        # Seed the savemap record if it was never initialised / is invalid (reads
        # as id 0 "Cloud" with 0 max HP → instant death). Clone-and-retarget Cloud.
        if _ensure_character_record(pm, cid):
            logger.debug(f"Initialized {char_name} character record (was uninitialised/invalid)")
        # Make the member available AND swappable in the PHS: SET the visibility
        # bit and CLEAR the lock bit. (Previously we set BOTH masks, but 0x10A4 is
        # the LOCK mask — setting it made delivered members appear but be un-
        # swappable.)
        vis_addr = SAVEMAP_BASE + _PHS_VISIBLE_OFFSET
        vis = pm.read_ushort(vis_addr)
        if not (vis & bit):
            pm.write_ushort(vis_addr, vis | bit)
        lock_addr = SAVEMAP_BASE + _PHS_LOCK_OFFSET
        lock = pm.read_ushort(lock_addr)
        if lock & bit:
            pm.write_ushort(lock_addr, lock & ~bit)
        # Auto-fill an empty active party slot (0xFF empty / 0xFE locked).
        base = SAVEMAP_BASE + _PARTY_OFFSET
        slots = [pm.read_uchar(base + i) for i in range(3)]
        if cid not in slots:
            for i in range(3):
                if slots[i] in (0xFF, 0xFE):
                    pm.write_uchar(base + i, cid)
                    break
        logger.debug(f"Delivered party member: {char_name}")
        return True
    except Exception as exc:
        logger.debug(f"Character delivery failed for {char_name}: {exc}")
        return False


# ── Battle reward multipliers (EXP / Gil / AP) ────────────────────────────────
# Instruction patches into ff7_en.exe's battle reward calc (addresses + bytes from
# ff7-ultima / ff7-lib; same exe map our savemap/module addresses use). Each
# rewrites the calc site to `imul <reg>, <reg>, <mult>` before the original add, so
# the boosted reward flows through the game's normal level-up / materia-AP handling
# (no post-hoc exp/AP fix-ups needed). <mult> is a signed imm8 (capped to 127).
_REWARD_EXP_ADDR = 0x43153F
_REWARD_GIL_ADDR = 0x43155A   # 0x43153F + 0x1B
_REWARD_AP_ADDR  = 0x431576
# First 6 bytes of the classic EXP site (`mov ecx,[eax+0x9AB138]`) — also the
# first 6 bytes our patch writes, so it doubles as an "already patched" marker.
_REWARD_EXP_ANCHOR = bytes((0x8B, 0x88, 0x38, 0xB1, 0x9A, 0x00))
# The 2013 Steam build (FFNx / 7th Heaven) computes the same reward in a different
# instruction order — it loads the running total first then adds the per-enemy
# value — so the site starts differently and the classic anchor misses it. It uses
# the SAME registers, the SAME per-enemy globals (0x9AB138/0x9AB134) and the SAME
# total globals (0x99E2C0/C8/C4) in blocks of the SAME size (18/16/12), so the
# exact same patch bytes apply at the exact same addresses. We validate the full
# original block of all three sites before patching this build.
_REWARD_EXP_ORIG_2013 = bytes((0x8B, 0x0D, 0xC0, 0xE2, 0x99, 0x00, 0x03, 0x88, 0x38,
                               0xB1, 0x9A, 0x00, 0x89, 0x0D, 0xC0, 0xE2, 0x99, 0x00))
_REWARD_GIL_ORIG_2013 = bytes((0xA1, 0xC8, 0xE2, 0x99, 0x00, 0x03, 0x82, 0x34,
                               0xB1, 0x9A, 0x00, 0xA3, 0xC8, 0xE2, 0x99, 0x00))
_REWARD_AP_ORIG_2013 = bytes((0xA1, 0xC4, 0xE2, 0x99, 0x00, 0x03, 0xC2,
                              0xA3, 0xC4, 0xE2, 0x99, 0x00))


def _reward_patch_exp(v: int) -> bytes:
    return bytes((0x8B, 0x88, 0x38, 0xB1, 0x9A, 0x00, 0x6B, 0xC9, v,
                  0x01, 0x0D, 0xC0, 0xE2, 0x99, 0x00, 0x90, 0x90, 0x90))


def _reward_patch_gil(v: int) -> bytes:
    return bytes((0x8B, 0x82, 0x34, 0xB1, 0x9A, 0x00, 0x6B, 0xC0, v,
                  0x01, 0x05, 0xC8, 0xE2, 0x99, 0x00, 0x90))


def _reward_patch_ap(v: int) -> bytes:
    return bytes((0x6B, 0xD2, v, 0x01, 0x15, 0xC4, 0xE2, 0x99, 0x00, 0x90, 0x90, 0x90))


def _apply_reward_multipliers(pm: "pymem.Pymem", ctx: FF7Context) -> None:
    """Patch the battle EXP/Gil/AP calc instructions once, per slot_data multipliers."""
    if ctx._reward_mult_applied:
        return
    if ctx.exp_multiplier <= 1 and ctx.gil_multiplier <= 1 and ctx.ap_multiplier <= 1:
        ctx._reward_mult_applied = True
        return
    try:
        if bytes(pm.read_bytes(_REWARD_EXP_ADDR, 6)) == _REWARD_EXP_ANCHOR:
            build = "classic"          # classic Steam build, or already patched
        elif (bytes(pm.read_bytes(_REWARD_EXP_ADDR, len(_REWARD_EXP_ORIG_2013))) == _REWARD_EXP_ORIG_2013
              and bytes(pm.read_bytes(_REWARD_GIL_ADDR, len(_REWARD_GIL_ORIG_2013))) == _REWARD_GIL_ORIG_2013
              and bytes(pm.read_bytes(_REWARD_AP_ADDR, len(_REWARD_AP_ORIG_2013))) == _REWARD_AP_ORIG_2013):
            build = "2013/FFNx"         # 2013 Steam build (FFNx / 7th Heaven)
        else:
            logger.warning("Reward multipliers: battle reward calc doesn't match a known "
                           "FF7 build — skipping (run /rewards to dump the code for support).")
            ctx._reward_mult_applied = True
            return
        # Same patch bytes + addresses for both builds (verified: identical
        # registers, per-enemy globals and total globals, identical block sizes).
        if ctx.exp_multiplier > 1:
            p = _reward_patch_exp(min(ctx.exp_multiplier, 127))
            pm.write_bytes(_REWARD_EXP_ADDR, p, len(p))
        if ctx.gil_multiplier > 1:
            p = _reward_patch_gil(min(ctx.gil_multiplier, 127))
            pm.write_bytes(_REWARD_GIL_ADDR, p, len(p))
        if ctx.ap_multiplier > 1:
            p = _reward_patch_ap(min(ctx.ap_multiplier, 127))
            pm.write_bytes(_REWARD_AP_ADDR, p, len(p))
        logger.debug(f"Battle reward multipliers applied ({build} build): "
                    f"EXP x{ctx.exp_multiplier}, Gil x{ctx.gil_multiplier}, AP x{ctx.ap_multiplier}")
        ctx._reward_mult_applied = True
    except Exception as exc:
        logger.debug(f"Reward multiplier patch failed: {exc}")


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
            logger.debug(f"Delivered item: {item_name} (ff7_id={ff7_id})")
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
# Native-grid Tier-3 AP shops: Gold Saucer's shop Hext sells reserved "token" item
# ids, shophook.dll displays the AP name/description on them, and the player buys
# normally. shophook.dll SUPPRESSES the inventory grant (the token never enters
# inventory) and appends "<section>:<index>" to shop_buys.txt. Here the client
# consumes that file and fires the matching AP location. Gil is still deducted by
# the game's separate DecreaseGil call, so the player pays for the slot.
SHOP_BUYS_FILENAME = "shop_buys.txt"


def _process_shop_purchases(pm: "pymem.Pymem", ctx: "FF7Context") -> List[int]:
    """Consume shophook.dll's shop_buys.txt and return the AP location codes to
    check. Each line is "<section>:<index>" (section 4 = item-space token, 13 =
    materia-space token). The DLL already suppressed the inventory grant, so the
    client only has to map the token to its location and fire the check."""
    path = ctx._shop_buys_path
    if path is None or not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if not text.strip():
            return []
        # Consume: truncate so each purchase fires exactly once.
        path.write_text("", encoding="utf-8")
    except Exception as exc:
        logger.debug(f"shop_buys.txt read/consume failed: {exc}")
        return []

    newly: List[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        sec_str, idx_str = line.split(":", 1)
        try:
            section = int(sec_str.strip(), 0)
            index = int(idx_str.strip(), 0)
        except ValueError:
            continue
        if section == KTEXT_MATERIA:
            loc = ctx.shop_materia_to_location.get(index)
            space = "materia"
        else:
            loc = ctx.shop_token_to_location.get(index)
            space = "item"
        if loc is None:
            logger.debug(f"AP shop buy {section}:{index} ({space}) has no mapped location.")
            continue
        if loc in ctx.checked_locations or loc in ctx._checked_this_session:
            continue
        newly.append(loc)
        ctx._checked_this_session.add(loc)
        logger.debug(f"AP shop purchase ({space} token {index}) → firing location {loc}")
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


def _strip_token_materia(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Delete AP shop-token materia from the materia inventory.

    Materia tokens use unused/"gap" materia ids that are never legitimately owned.
    The shophook fires the purchase check from the gil drop, but the shop's materia
    grant isn't fully suppressed, so the token materia still lands in inventory
    (shown with a broken name/AP). Since these ids can never be a real materia, we
    strip any of them out each poll and compact the list. Runs in all modes."""
    token_ids = set(ctx.shop_materia_to_location.keys())
    if not token_ids:
        return
    try:
        base = SAVEMAP_BASE + MATERIA_LIST_OFFSET
        raw = bytes(pm.read_bytes(base, MATERIA_SLOT_COUNT * 4))
        kept = bytearray()
        removed = 0
        for i in range(MATERIA_SLOT_COUNT):
            slot = raw[i * 4:i * 4 + 4]
            if slot[0] in token_ids:          # token id byte ⇒ drop this slot
                removed += 1
            else:
                kept += slot
        if removed == 0:
            return
        kept += b"\xff\xff\xff\xff" * removed  # refill the freed slots as empty (compacted)
        pm.write_bytes(base, bytes(kept), len(kept))
        logger.debug(f"Stripped {removed} AP shop-token materia from inventory")
    except Exception as exc:
        logger.debug(f"strip token materia failed: {exc}")


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
        # ── Wait quietly for a BITON map (logged to file only, not the client) ─
        if not ctx.biton_map:
            logger.debug(
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
                            logger.debug("Materia menu enabled from game start")
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
                            # with the correct cross-player names + descriptions
                            # BEFORE injecting (the DLL reads the file once at load).
                            if ctx._shop_apff7_names or ctx._shop_apff7_materia_names:
                                _write_shop_ap_txt(exe_dir_p, ctx._shop_apff7_names,
                                                   ctx._shop_apff7_materia_names,
                                                   ctx._shop_apff7_descs,
                                                   ctx._shop_apff7_materia_descs)
                            # Purchase-signal file the DLL appends to; clear any
                            # stale entries from a previous session before injecting.
                            ctx._shop_buys_path = exe_dir_p / SHOP_BUYS_FILENAME
                            try:
                                ctx._shop_buys_path.write_text("", encoding="utf-8")
                            except Exception:
                                pass
                            dll = exe_dir_p / "shophook.dll"
                            if inject_dll(pm, dll):
                                ctx._hook_injected = True
                            n_item = len(ctx.shop_token_to_location)
                            n_mat = len(ctx.shop_materia_to_location)
                            if n_item or n_mat:
                                logger.debug(
                                    "Shop detection: watching shop_buys.txt for "
                                    f"{n_item} item + {n_mat} materia token slot(s)."
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

        # ── Battle reward multipliers (one-time exe patch once connected) ──
        _apply_reward_multipliers(pm, ctx)

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
            # One savemap snapshot per poll backs both the baseline and the
            # detection scan (all BITON flags live inside it), replacing ~381
            # per-location reads with a single read. Falls back to per-location
            # reads if the snapshot read fails.
            try:
                _sm = bytes(pm.read_bytes(SAVEMAP_BASE, SAVEMAP_LEN))
                if len(_sm) < SAVEMAP_LEN:
                    _sm = None
            except Exception:
                _sm = None

            def _biton_is_set(bank: int, address: int, bit: int) -> bool:
                idx = _biton_byte_addr(bank, address) - SAVEMAP_BASE
                if _sm is not None and 0 <= idx < len(_sm):
                    return bool(_sm[idx] & (1 << bit))
                return bool(pm.read_uchar(_biton_byte_addr(bank, address)) & (1 << bit))

            if not ctx._baseline_established and ctx.biton_map:
                for code, (bank, address, bit) in ctx.biton_map.items():
                    if code in ctx.checked_locations:
                        continue
                    try:
                        if _biton_is_set(bank, address, bit):
                            ctx._baseline_locations.add(code)
                    except Exception:
                        pass
                ctx._baseline_established = True
                if ctx._baseline_locations:
                    logger.debug(
                        f"Baseline: suppressing {len(ctx._baseline_locations)} "
                        f"pre-set location flag(s) + already-passed boss checks "
                        f"(game moment {game_moment})."
                    )

            newly_checked = []
            for code, (bank, address, bit) in ctx.biton_map.items():
                if (code in ctx.checked_locations
                        or code in ctx._checked_this_session
                        or code in ctx._baseline_locations):
                    continue
                try:
                    hit = _biton_is_set(bank, address, bit)
                except Exception:
                    continue                          # bad single read — skip, don't kill the pass
                if hit:
                    newly_checked.append(code)
                    ctx._checked_this_session.add(code)

            if newly_checked and ctx.server and ctx.slot:
                await ctx.send_msgs([{"cmd": "LocationChecks", "locations": newly_checked}])
                for code in newly_checked:
                    try:
                        logger.debug(f"Checked location: {ctx.location_names.lookup_in_game(code)}")
                    except Exception:
                        logger.debug(f"Checked location: {code}")

            # ── Relocate any AP-delivered vehicle stranded at the (0,0) sea tile ─
            _place_stranded_vehicles(pm, ctx)

            # ── Drive the Northern Crater gate flag from received goal items ───
            _enforce_crater_lock(pm, ctx)

            # ── Keep the mds5_5 walkmesh open once Key to Sector 5 is owned ─────
            # The gate flag (Var[15][38].3) is read on each field load, so re-set
            # it every poll: this self-heals re-entering mds5_5 and keys received
            # before this gate flag was wired up (one-time delivery already past).
            if "Key to Sector 5" in ctx._received_item_names:
                _ensure_sector5_walkmesh_gate(pm)

            # ── Free Roam: finish Ultimate Weapon once the player has engaged him ─
            if ctx.free_roam:
                _resolve_ultimate_weapon(pm)
                # Register Ruby/Emerald kills from a won battle (their flags are
                # otherwise never set in Free Roam → no check + endless respawn).
                _resolve_weapon_battles(ctx, pm)
                # Force disc 3 (Free Roam = endgame). New games default to disc 1;
                # re-assert each poll in case the engine resets it.
                try:
                    if pm.read_uchar(SAVEMAP_BASE + DISC_OFFSET) != FREE_ROAM_DISC:
                        pm.write_uchar(SAVEMAP_BASE + DISC_OFFSET, FREE_ROAM_DISC)
                except Exception:
                    pass
                # Own all 6 Chocobo Farm stables from the start (vanilla buys them
                # one at a time from Choco Billy). Each AP chocobo can then be
                # stabled immediately instead of the owned-count creeping up per
                # delivery. Re-asserted each poll; occupancy (0x0CFD/0x0CFF) is still
                # managed per chocobo by _deliver_chocobo.
                try:
                    if pm.read_uchar(SAVEMAP_BASE + _CHOCO_STABLES) != _CHOCO_MAX_SLOTS:
                        pm.write_uchar(SAVEMAP_BASE + _CHOCO_STABLES, _CHOCO_MAX_SLOTS)
                except Exception:
                    pass
                # Keep delivered optional characters playable: re-seed their record
                # if it's been left/overwritten invalid (id 0 "Cloud" / 0 HP → dies).
                for _cname, _cid in _CHARACTER_IDS.items():
                    if _cname in ctx._received_item_names:
                        if _ensure_character_record(pm, _cid):
                            logger.debug(f"Re-seeded {_cname} record (was invalid)")
                # Force open field gates that would otherwise softlock (e.g. the
                # Mt. Corel mtcrl_2 door, read on field load).
                for _off, _bit in _FREE_ROAM_FORCE_FLAGS:
                    try:
                        _a = SAVEMAP_BASE + _off
                        _v = pm.read_uchar(_a)
                        if not (_v & (1 << _bit)):
                            pm.write_uchar(_a, _v | (1 << _bit))
                    except Exception:
                        pass
                # Item-conditional gates (open only once the key item is received).
                for _gitem, _goff, _gbit in _FREE_ROAM_ITEM_GATE_FLAGS:
                    if _gitem in ctx._received_item_names:
                        try:
                            _a = SAVEMAP_BASE + _goff
                            _v = pm.read_uchar(_a)
                            if not (_v & (1 << _gbit)):
                                pm.write_uchar(_a, _v | (1 << _gbit))
                        except Exception:
                            pass

            # ── Shop purchases: detect token buys, swap to Potion, fire checks ─
            shop_checks = _process_shop_purchases(pm, ctx)
            if shop_checks and ctx.server and ctx.slot:
                await ctx.send_msgs([{"cmd": "LocationChecks", "locations": shop_checks}])
                for code in shop_checks:
                    logger.debug(f"Checked location: {ctx.location_names.lookup_in_game(code)}")

            # ── Strip AP materia tokens the shop grant leaves in inventory ─────
            _strip_token_materia(pm, ctx)

            # ── Check win condition ───────────────────────────────────────
            if not ctx.finished_game and ctx.server and ctx.slot:
                if ctx.victory_condition == 1:  # escape_midgar
                    reached_goal = game_moment >= MIDGAR_ESCAPE_MOMENT
                    goal_message = "Goal complete — Escaped from Midgar!"
                else:  # defeat_sephiroth (default)
                    # The game switches the live module to Ending/Credits only after
                    # Sephiroth is beaten — the reliable "on the kill" signal.
                    module = pm.read_uchar(GAME_MODULE_ADDR)
                    reached_goal = module in (GAME_MODULE_ENDING, GAME_MODULE_CREDITS)
                    goal_message = "Goal complete — Sephiroth defeated!"
                if reached_goal:
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
            ctx._shop_buys_path = None
            ctx.pm = None
            await asyncio.sleep(3)
            continue
        except Exception as exc:
            # A transient read/state error (e.g. a read during a load screen, or a
            # bug in one sub-step) must NOT tear down the whole session: wiping the
            # baseline here re-snapshots it on the next poll, which would mark any
            # since-flipped location flag as "pre-existing" and silently drop the
            # check. Surface the real error once, probe that the process is still
            # alive, and only reconnect if it has actually gone. Baseline + checked
            # state are preserved across the hiccup.
            log_once(f"FF7 poll error (continuing): {exc!r}")
            try:
                pm.read_uchar(SAVEMAP_BASE)            # cheap liveness probe
            except Exception:
                logger.info("FF7 process lost — will reconnect.")
                pm = None
                ctx.game_connected = False
                ctx._checked_this_session.clear()
                ctx._boss_checks_sent.clear()
                ctx._baseline_established = False
                ctx._baseline_locations.clear()
                ctx._hook_injected = False
                ctx._shop_buys_path = None
                ctx.pm = None
            await asyncio.sleep(POLL_INTERVAL)
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
