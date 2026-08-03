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
import hashlib
import json
import random
import struct
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

if TYPE_CHECKING:
    from worlds.ff7.TrapLink import TrapSpec

from CommonClient import CommonContext, ClientCommandProcessor, logger, server_loop
from NetUtils import ClientStatus
from Utils import async_start, user_path

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
# How long after Connected an index==0 ReceivedItems packet is still assumed to be
# Archipelago replaying the item history rather than a genuine first receipt. The
# replay is part of the handshake and arrives near-instantly; 10s is generous.
# Worst case if a player somehow checks a location inside the window: that one item
# is treated as history, i.e. exactly the old behaviour.
_HISTORY_SYNC_WINDOW_S = 10.0
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
# Module ids seen DURING a battle->gameplay transition, measured in a vanilla
# trace: World(3) -> 23 -> Battle(2) -> 17 -> World(3). 23 is pre-battle, 17 is
# post-battle/load. Anything OUTSIDE this set means we are not on a normal
# battle-exit path, and a pending weapon kill must be discarded.
_BATTLE_EXIT_MODULES = frozenset({
    GAME_MODULE_BATTLE, GAME_MODULE_FIELD, GAME_MODULE_WORLD, 17, 23,
})
# How long a latched weapon kill stays valid after the battle ends. A real
# battle->world return measured ~3.8s; a game over plus save reload is far longer
# (game-over music, Continue menu, slot select, load), so this window separates
# them. 60 polls x POLL_INTERVAL 0.2s = 12s.
_WEAPON_KILL_WINDOW_TICKS = 60

# Field-model object array (2013 Steam build; ff7-lib addresses.rs, maciej-trebacz).
# Each on-field model is a 0x88-byte struct at FIELD_MODELS_OBJS + index*0x88, where
# index = the entity's CHAR-opcode operand. Relevant bytes: +0x5f collision
# (0 = solid), +0x61 interaction (0 = talkable), +0x62 visible (1 = shown). These
# are what FF7 Ultima's field-model toggle writes. FIELD_NAME_ADDR = current field's
# name string (e.g. "crcin_1").
FIELD_NAME_ADDR          = 0xCC1EF0
FIELD_MODELS_OBJS        = 0xCC1670
_FIELD_MODEL_STRUCT_SIZE = 0x88

# in-battle actor memory (party status/hp) lives in worlds/ff7/TrapLink.py,
# shared by the traps and deathlink.

# Free Roam game-over recovery: number of consecutive in-game (Field/World) polls
# required after a Game Over before we re-deliver all received items. Gives the
# new-game md1stin savemap seeding time to finish so our re-writes aren't clobbered.
_RESUME_REDELIVER_TICKS = 3

# Live battle formation index (u16). ff7-ultima "battle_id". Used to detect a
# won Ruby/Emerald battle and register the kill (their weapons_killed flags are
# set by post-battle world-script logic the Free Roam endgame skips, so a won
# fight leaves them un-flagged -> the AP check never fires and they respawn).
BATTLE_FORMATION_ADDR = 0x9AAD3C
# Weapon battle formation id -> weapons_killed bit mask (ff7-ultima ff7Battles.ts /
# FF7 enemy-formation table):
#   982/983 Ruby[Desert]        = bit3 0x08
#   984/985/986 Emerald[Water]  = bit4 0x10
# ULTIMATE IS NOT HERE, and needs nothing from the client (2026-07-27).
# The vanilla chain works once Gold Saucer stops corrupting it: the BATTLE ENGINE
# sets weapons_killed.bit[0] itself mid-fight (measured in both vanilla AND Free
# Roam), then `highwind_init` runs the crater crash on the world-map return and
# `ultima_weapon_27` sets submarine_flags.bit[4] when it completes. The AP check
# reads bit[0] like any other flag.
#
# The 2026-07-27 "kill him at battle 281 and retire him" workaround is REVERTED:
# it existed only because the crash appeared unfixable, and its `_ULTIMATE_RETIRE`
# write (submarine_flags.bit[4]) would now SUPPRESS the very cinematic that was
# just repaired. Root cause was ours — `patchDiamondBoardingScene` part (e) wrote
# RETURN over ultima_weapon_27's first instruction. See [[freeroam-weapon-bosses]].
#
# Ruby and Emerald DO still need the client write: their flags come from
# post-battle world scripting the Free Roam endgame skips, so a won fight would
# otherwise leave them un-flagged and they would respawn.
_WEAPON_BATTLE_FORMATIONS = {
    982: 0x08, 983: 0x08,
    984: 0x10, 985: 0x10, 986: 0x10,
}
# Ultimate's final battle. CURRENTLY UNUSED — kept as documentation and for the
# `/weapons` dump. The client's kill registration was removed 2026-07-27 so the
# vanilla script can be observed unassisted; if the game turns out never to set
# weapons_killed.bit[0] itself, restore the latch in `_resolve_weapon_battles`.
_ULTIMATE_FINAL_FORMATION = 287
# ── Ultimate Weapon ──────────────────────────────────────────────────────────
# Deliberately NOT in the table above. Setting weapons_killed.bit0 the moment 287
# is won tells the world script he is already dead, so `ultima_weapon_update`
# stops running and never executes the death sequence that flies him off — he
# stays loaded exactly where he is and the player collides straight into a second
# fight (playtester 2026-07-23; the /weapons trace showed bit0 set on the 287 win
# with him still present at Cosmo Canyon).
#
# Instead we ARM his own death and let the game perform it: on winning 287, write
# the chase hit counter to 5, which is the precondition `ultima_weapon_update`
# tests before calling `ultima_weapon_25` (the death sequence). The game then sets
# bit0 itself, exactly as it would in a vanilla chase.
# Polls to wait for that death sequence before falling back to setting bit0
# ourselves. POLL_INTERVAL is 0.2s, so ~24s — generous for the fade/fly/animation.
# The fallback exists so a failure here can never regress to the infinite-fight
# run-breaker: worst case we land back on the old behaviour, never worse.
WEAPONS_KILLED_OFFSET  = 0x0C1F  # byte: bit0 = killed, bit2 = HP < 20,000
SUBMARINE_FLAGS_OFFSET = 0x0F2A  # byte: bit3 = Ultimate Weapon chase started/engaged
# Ultimate Weapon's CHASE HIT COUNTER (decompiled from wm0.ev, 2026-07-23).
# `ultima_weapon_26` — his post-battle hit reaction — ends with, gated on < 5:
#     Savemap[0xF33].byte = Savemap[0xF33].byte + 1
# and `ultima_weapon_update` (model 11 fn 2) tests `Savemap[0xF33].byte == 5` at
# wm0 0x3692; that TRUE branch is what flies him off and calls `ultima_weapon_25`,
# the death sequence. So FIVE HITS is the game's own precondition for him dying.
# (0xF38 is the chase STAGE, picking the formation per stop: 9->282, 10->294,
# 11->283, 12->284, 14->285, else 286. 0xF33 is Ultimate-only — nothing else in
# wm0.ev reads or writes it, so priming it cannot disturb anything else.)
ULTIMATE_HITS_OFFSET = 0x0F33   # byte: chase hits landed, 0-5; 5 = he dies
# ULTIMATE WEAPON'S REMAINING HP — savemap 0x0BFF, **3 bytes** (bank 1 field 91).
# THIS WAS THE ROOT CAUSE of the whole Ultimate saga. A fresh Free Roam save leaves
# it ZERO, so the engine's "Ultimate HP < 20,000" test is true from the very first
# frame and it sets weapons_killed.bit[2] the moment the player engages him. Per the
# decompiled `ultima_weapon_update`, bit2 both (a) re-triggers battle 287 on every
# approach and (b) blocks the death branch — hence the endless 287 loop, the forced
# second fight, and the death sequence never playing. Every earlier workaround was
# treating that symptom.
#
# Seeding his real HP lets the vanilla chase run: he flies between stops, each
# battle whittles the pool, and when it genuinely drops below 20,000 the game sets
# bit2 itself, sends him to the crater, and the final 287 is lethal.
ULTIMATE_HP_OFFSET = 0x0BFF     # 3 bytes LE, "Ultimate Weapon's remaining HP"
# World-script "Special[]" registers — the runtime values wm0.ev reads with
# PUSH_SPECIAL (opcode 0x11b). NOT savemap, so nothing we write can reach them.
# Base derived from ff7-lib addresses.rs: world_mode = 0xE045E4 and
# world_map_type = 0xE045E8 are adjacent u32s, and Landscaper decompiles the
# corresponding reads as Special[4] and Special[5] — so the array starts at
# 0xE045E8 - 5*4. That also puts Special[6]/Special[7] where the decompiles say
# last_field_id and map_options live, which is the cross-check.
#
# Special[5] ("unknown_5") is the last unexplained gate on Ultimate's crater-crash
# cinematic: both wm0 0x3818 (which PLACES him at the crater) and highwind_init
# (which calls ultima_weapon_27) require it to be 1, and Free Roam appears to never
# reach that value.
WORLD_SPECIAL_BASE = 0xE045D4   # Special[0]; u32 stride
# UNUSED since the client stopped seeding his HP (2026-07-27). Kept for reference:
# note that live readings during a chase reach ~12,000,000, far above this, so this
# was only ever a seed for the "reads exactly 0" case and is NOT a maximum.
ULTIMATE_HP_FULL   = 100000     # his nominal full HP (0x0186A0); field is u24
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
    (0x0C26, 5),   # #5 moves the snmin1 cat off the snowboard. The cat's init
                   #    gates its position on Var[1][130].5: OFF -> (-66,634) on top
                   #    of the snowboard (blocks it); ON -> (-37,433), clearing it.
                   #    The vanilla event that sets this is skipped in Free Roam. No
                   #    location detects on bit 5 (snowboard=bit1, glacier map=bit6).
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
    # NOTE: Cave of the Gi (cosin2) story is handled by Gold Saucer neutering the
    # BUGEN cutscene script directly (GI_CAVE_STORY), not a force-flag. The earlier
    # Var[3][173].7 attempt did not gate it and was removed.
    # NOTE: Var[3][189] bit 4 (0xD61.4, the "& 16" field-script gate) is no longer
    # forced here — removed at request.
    # NOTE: Ruby Weapon's spawn (0xF2B.4) is NOT forced here anymore. Ruby's model
    # geometry only renders at world_progress 4, which the overworld init reaches
    # only after Ultimate is dead — so forcing his spawn early just produced an
    # invisible, collidable boss. Those flags USED to be set together once Ultimate
    # was defeated, but all client Ultimate writes were removed 2026-07-27 — so
    # nothing sets them now and Ruby may not render at all. See the note above
    # _resolve_weapon_battles.
]
# Item-conditional field gates: set savemap <offset>.<bit> ONLY once <item> has
# been received (the field gate softlocks otherwise, but opening it without the
# item would break the AP logic). Read on field load, so re-asserted each poll.
_FREE_ROAM_ITEM_GATE_FLAGS = [
    # NOTE: Basement Key intentionally has NO entry here. Its gate flag
    # (Var[1][232].1 = 0x0C8C.1) doubles as the "Key To Basement" pickup's
    # detection bit, so setting it on receipt made that AP check uncheckable.
    # Gold Saucer now re-gates the sininb2 basement on the possession bit
    # (Var[1][0x43].4, set by KEY_ITEM_FLAGS), so the door opens from holding
    # the key while 0x0C8C.1 is freed for the (re-introduced) check. See the
    # FieldPickup patch "BASEMENT_GATE".
    # NOTE: Leviathan Scales location 200336 now uses bit 2 (not bit 0) for
    # pickup detection to avoid conflict with this possession flag. The chest
    # script checks bit 2, while game scripts check bit 0 for possession.
    ("Leviathan Scales", 0x1031, 0),   # "has Leviathan Scales" prerequisite — Var[15][141].0
                                       # (0xFA4+0x8D). Field scripts gate their reward branches
                                       # on this being ON. NOT setting Var[15][137].* — those are
                                       # per-NPC "reward already given" bits, checked OFF, so
                                       # setting them would block the rewards.
    # NOTE: Glacier Map deliberately has NO gate-flag entry. Its "Glacier Map key
    # item obtained" story flag (Var[1][130].6 = 0xC26.6) is ALSO the in-game
    # pickup's detection flag for location 310019 ("Icicle Inn - Glacier Map"):
    # setting it on receipt made the field hide the pickup AND auto-suppressed the
    # AP check, leaving it permanently unobtainable. No snow/glacier field reads
    # Var[1][130] for navigation (verified by full-game field scan) — the in-game
    # map feature is driven by the key-item POSSESSION bit (KEY_ITEM_FLAGS, 0x45.4),
    # which is still set on receipt. So this location now behaves like every other
    # working key-item pickup.
    ("Submarine", 0x0EF4, 3),          # "Gray submarine" OWNED flag — bank-13 byte (0xEA4+0x50 =
                                       # 0xEF4), bit 3 (0x08). The system overworld init checks this to
                                       # LOAD the submarine model, so this is what makes it APPEAR next
                                       # to Junon. VEHICLE_ITEM_FLAGS also sets tut_sub (0xC1E.2 = skips
                                       # the acquisition tutorial). Re-asserted each poll so it spawns on
                                       # the next world-map entry after receipt.
    ("Submarine", 0x0EF6, 2),          # Persistent "submarine parked & drivable on the world map" flag —
                                       # bank-13 byte 0xEF6, bit 2. Despite the "Red submarine" label in
                                       # some savemap docs, the model-13 init in wm0.ev REQUIRES this for
                                       # its steady-state spawn path (the one vanilla takes on every
                                       # world-map entry once tut_sub is set): that path is gated on
                                       # owned(0xEF4.3) AND tut_sub(0xC1E.2) AND 0xEF6.2 AND being in the
                                       # Junon mesh AND NOT(0xF2A.1). The Junon-raid field script normally
                                       # sets 0xEF6.2; Free Roam skips that script, so without this the
                                       # model loaded (owned flag) and APPEARED but no init path ran to
                                       # POSITION/REGISTER it as a drivable vehicle — the sub showed up at
                                       # Junon but could not be driven out to sea. Checked in 6 places in
                                       # wm0.ev, set in none, so it must come from the savemap. NOT(0xF2A.1)
                                       # holds because the client only ever writes 0xF2A bits 3/4 (weapon
                                       # pre-arm), never bit 1.
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


# Which trap item indices have ACTUALLY fired, persisted per (seed, slot).
#
# Traps must fire exactly once each, and that has to survive a client restart —
# Archipelago replays the whole item history on every reconnect, so without a
# record of what already fired the client can only GUESS from the packet shape.
# Guessing produced two bugs: every trap re-firing on reconnect (the reason the
# index==0 heuristic was added), and then a brand-new player's FIRST trap never
# firing at all, because a first receipt and a history replay are identical on the
# wire. Persisting the answer removes the guess.
#
# It also closes the gap the heuristic explicitly gave up on: a trap granted while
# the client was fully closed now fires on the next connect instead of being
# written off as history.
_FIRED_TRAPS_FILE = Path(user_path("ff7_fired_traps.json"))


def _fired_traps_key(ctx: "FF7Context") -> str:
    """Per-multiworld, per-slot identity. Two slots in one seed, or the same slot
    across two seeds, must not share a fired-trap record."""
    return f"{_ap_seed(ctx)}|{getattr(ctx, 'auth', '') or ''}"


def _load_fired_traps(ctx: "FF7Context") -> Set[int]:
    try:
        if _FIRED_TRAPS_FILE.exists():
            data = json.loads(_FIRED_TRAPS_FILE.read_text(encoding="utf-8"))
            return {int(i) for i in data.get(_fired_traps_key(ctx), [])}
    except Exception as exc:
        logger.debug(f"fired-trap load failed: {exc}")
    return set()


def _save_fired_traps(ctx: "FF7Context") -> None:
    """Rewrite this slot's entry, preserving every other slot's."""
    try:
        data = {}
        if _FIRED_TRAPS_FILE.exists():
            try:
                data = json.loads(_FIRED_TRAPS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}                      # corrupt file: start it over
        if not isinstance(data, dict):
            data = {}
        data[_fired_traps_key(ctx)] = sorted(ctx._seen_trap_indices)
        _FIRED_TRAPS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug(f"fired-trap save failed: {exc}")


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


def _read_apff7_json(path: "Path") -> dict:
    """Read the .apff7 seed: either the APPlayerContainer zip the apworld emits
    (payload member ff7_seed.json alongside the archipelago.json manifest) or
    the legacy bare-JSON format. Sniffed by the PK zip magic."""
    raw = path.read_bytes()
    if raw[:2] == b"PK":
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            member = ("ff7_seed.json" if "ff7_seed.json" in names else
                      next(n for n in names if not n.endswith("archipelago.json")))
            return json.loads(zf.read(member).decode("utf-8"))
    return json.loads(raw.decode("utf-8"))


# Shop AP-slot key: (shop_id, kernel-text section, token id). Keying on the SHOP
# id (not the bare token id) makes detection unambiguous — the same real FF7
# item/materia id can be an AP slot in one shop AND vanilla stock in another (or
# an equippable weapon you own): only the exact (shop, section, id) the seed
# reserved is treated as an AP slot. This is what stops unequipping a weapon or
# buying a shared-id materia in an unrelated shop from firing a false check.
ShopKey = Tuple[int, int, int]


# Free Roam story-shop variants. Fort Condor, Junon, Costa del Sol and Rocket Town
# pick their shop id by story progress: a field opens an EARLY id before the event
# and a LATE id after it. Free Roam pins game_moment to 1997 (disc 3), so those
# fields ALWAYS take the late branch — and Gold Saucer therefore clones each
# tokenized early record over its late id (`mirrorFreeRoamStoryShops` in
# ShopRandomizer.cpp). The .apff7 only ever names the EARLY id, so every AP slot
# must also be registered under the late id(s): shophook.dll keys names and buys on
# the LIVE shop id, so without this the token renders as a nameless materia/item
# and buying it fires nothing (playtester report: blank 100 gil materia in the Fort
# Condor materia shop — shop 17 mirrored to 52).
#
# Aliasing cannot cause double-checks: the token id is unchanged and each maps to a
# single AP location, so both ids resolve to the same location, which fires once.
# Upper Junon's item shop (20) is SPLIT across 55 and 59 rather than cloned, so both
# are listed and each slot only actually exists in one of them.
_SHOP_ID_MIRRORS: Dict[int, Tuple[int, ...]] = {
    16: (51,), 17: (52,),               # Fort Condor    item / materia
    19: (54,),                          # Upper Junon    weapon #1
    20: (55,),                          # Upper Junon    item
    22: (57,), 23: (58,),               # Upper Junon    weapon #2 / accessory
    24: (59,),                          # Upper Junon    materia #2
    26: (60,), 27: (61,), 28: (62,),    # Costa del Sol  weapon / materia / item
    41: (63,), 42: (64,),               # Rocket Town    weapon / item
}
# NOTE: 59 is the D2 variant of shop 24 (Upper Junon Materia #2), NOT a second late
# id for the item shop 20. An earlier build mapped 20 -> (55, 59) and had GS SPLIT
# shop 20 across both, which overwrote shop 24 entirely — so its AP token ("Upper
# Junon Materia 2 - AP Slot") lived in no reachable shop (reported 2026-07-23).
# Shop 21 (Upper Junon Materia #1) has no D1/D2 split and needs no mirror.


def _shops_from_apff7(
    shops: List[dict],
) -> Tuple[Dict[ShopKey, int], Dict[ShopKey, str], Dict[ShopKey, str]]:
    """From the .apff7 ``shops`` array build (loc, name, desc) maps keyed by
    (shop_id, section, token_id). Section 4 = item space (composite id), 13 =
    materia space. Display name ``A <Item>`` (leading "A " marks an AP slot);
    the owner sits in the description shown in the shop info pane."""
    locs:  Dict[ShopKey, int] = {}
    names: Dict[ShopKey, str] = {}
    descs: Dict[ShopKey, str] = {}
    for s in shops:
        token   = s.get("token_id")
        loc     = s.get("location_id")
        shop_id = s.get("shop_id")
        if token is None or loc is None or shop_id is None:
            continue
        section = KTEXT_MATERIA if s.get("token_type", "item") == "materia" else KTEXT_ITEM
        item  = (s.get("item") or "AP Item").strip()
        owner = (s.get("item_owner") or "").strip()
        cls   = (s.get("item_classification") or "").strip()
        name  = f"A {item}"[:30]
        desc  = (f"An Archipelago Item for {owner}" if owner else "An Archipelago Item")
        if cls:
            desc += f" - {cls} Item"
        # Register under the declared shop id AND any Free Roam late variant GS
        # mirrored it into, so the slot is named/detected whichever id opens.
        for sid in (int(shop_id), *_SHOP_ID_MIRRORS.get(int(shop_id), ())):
            key: ShopKey = (sid, section, int(token))
            locs[key]  = int(loc)
            names[key] = name
            descs[key] = desc
    return locs, names, descs


def _shop_sold_keys(ctx: "FF7Context") -> "frozenset[ShopKey]":
    """The AP shop cells whose location is already checked (server-confirmed or
    fired this session) — shophook.dll removes these from shop stock so an
    obtained AP item can't be re-bought."""
    done = set(getattr(ctx, "checked_locations", set())) | ctx._checked_this_session
    return frozenset(key for key, loc in ctx.shop_ap_locations.items() if loc in done)


def _write_shop_sold_txt(exe_dir: Path, sold: "frozenset[ShopKey]") -> None:
    """Write shop_sold.txt (read by shophook.dll on each shop open). One
    ``<shop_id>:<section>:<index>`` line per already-obtained AP cell."""
    try:
        lines = ["# Sold AP shop cells (removed from stock). shop:section:index\n"]
        lines += [f"{shop}:{sec}:{idx}\n" for (shop, sec, idx) in sorted(sold)]
        (exe_dir / "shop_sold.txt").write_text("".join(lines), encoding="utf-8")
    except Exception as exc:
        logger.debug(f"shop_sold.txt write failed: {exc}")


def _write_shop_ap_txt(
    exe_dir: Path, names: Dict[ShopKey, str],
    descs: Optional[Dict[ShopKey, str]] = None,
) -> None:
    """Write shop_ap.txt (read by shophook.dll). Format:
    ``<shop_id>:<section>:<index>=<name>[|<description>]``. The shop id scopes the
    name override to the exact shop cell, so a real item id that is AP stock in
    one shop but vanilla stock elsewhere is only renamed where it belongs."""
    descs = descs or {}
    try:
        lines = ["# Auto-generated from the .apff7 shop placements. shop:section:index=name|desc\n"]
        for (shop, sec, idx), name in sorted(names.items()):
            desc = descs.get((shop, sec, idx))
            lines.append(f"{shop}:{sec}:{idx}={name}|{desc}\n" if desc
                         else f"{shop}:{sec}:{idx}={name}\n")
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
        /rewards /mapbitons /parkcoord) are diagnostics — some write game memory —
        so they do nothing until enabled here."""
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
            data = _read_apff7_json(json_path)
            biton_map = _biton_map_from_placements(data.get("placements", []))
            shop_loc, shop_names, shop_descs = \
                _shops_from_apff7(data.get("shops", []))
        except Exception as exc:
            logger.warning(f"Failed to read JSON: {exc}")
            return False

        self.ctx.json_path = json_path
        self.ctx.biton_map = biton_map
        self.ctx.shop_ap_locations = shop_loc
        self.ctx._shop_ap_names = shop_names
        self.ctx._shop_ap_descs = shop_descs
        if shop_loc:
            logger.debug(f"Shop slots loaded: {len(shop_loc)} AP shop check(s).")

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
            # Ultimate's CHASE state. His REMAINING HP is the value that governs
            # everything: at 0 the engine reads "HP < 20,000" as true, sets
            # weapons_killed.bit[2] on first contact and locks him into the crater
            # standoff (endless 287, death branch unreachable). 0xF33 counts chase
            # hits; 0xF38 is the stage that picks the formation.
            hp = int.from_bytes(pm.read_bytes(SAVEMAP_BASE + ULTIMATE_HP_OFFSET, 3),
                                "little")
            hits = pm.read_uchar(SAVEMAP_BASE + ULTIMATE_HITS_OFFSET)
            stage = pm.read_uchar(SAVEMAP_BASE + 0x0F38)
            _stage_form = {9: 282, 10: 294, 11: 283, 12: 284, 14: 285}
            logger.info(
                f"[weapons] Ultimate HP 0xBFF={hp}"
                f"{'  ** 0 = uninitialised, chase will not work **' if hp == 0 else ''}"
                f"{'  (below 20,000 -> final-battle state)' if 0 < hp < 20000 else ''}"
            )
            logger.info(
                f"[weapons] CHASE: hits 0xF33={hits}/5"
                f"  stage 0xF38={stage}"
                f" -> formation {_stage_form.get(stage, 286)};"
                f" 0xF2B=0x{ruby_spawn:02x} (.1 death-started="
                f"{'Y' if ruby_spawn & 0x02 else 'N'} .2 death-seq-done="
                f"{'Y' if ruby_spawn & 0x04 else 'N'})"
            )
            # World Special[] registers — Special[5] is the crash cinematic's last
            # gate. Labelled where the decompiles name them; the rest are shown raw
            # so the array alignment can be sanity-checked (6 should look like a
            # field id, 7 like map options).
            _sp_names = {4: "world_mode", 5: "world_map_type / unknown_5",
                         6: "last_field_id", 7: "map_options"}
            try:
                _sp = [pm.read_uint(WORLD_SPECIAL_BASE + i * 4) for i in range(9)]
                logger.info("[weapons] Special[]: " + "  ".join(
                    f"[{i}]={v}" for i, v in enumerate(_sp)))
                logger.info(f"[weapons] Special[5] = {_sp[5]} "
                            f"({_sp_names[5]}) — crater crash needs 1"
                            f"{'  ** MATCHES **' if _sp[5] == 1 else ''}")
                logger.info(f"[weapons]   cross-check: [6]={_sp[6]} ({_sp_names[6]}), "
                            f"[4]={_sp[4]} ({_sp_names[4]})")
            except Exception as exc:
                logger.info(f"[weapons] Special[] read failed: {exc}")
            # IS ULTIMATE'S MODEL ACTUALLY LOADED?
            # This is the diagnostic the whole crash investigation was missing.
            # wm0's overworld model-loader has two arms, chosen by `Special[5] == 0`
            # at 0x058E. Free Roam always runs arm 1, which loads model 11 only under
            # `!weapons_killed.bit[0]` — so a DEAD Ultimate has no entity, and
            # highwind_init's `call_function(ultima_weapon, 27)` lands on nothing:
            # the Highwind hides and the music cuts (that part is highwind_init's own
            # code) and then nothing happens. The arm that loads him while dead is
            # arm 2's crash-scene block at 0x14AA, which Free Roam never reaches.
            # Gold Saucer's `patchUltimateModelLoad` re-keys the arm-1 gate to
            # `!submarine_flags.bit[4]` so he stays loaded until the crash is DONE.
            #
            # Read-only — no writes, so a trace taken with this is never contaminated.
            try:
                _ptr = pm.read_uint(_WORLD_ENTITY_PTR)
                _found, _seen, _models = None, set(), []
                for _ in range(48):
                    if _ptr == 0 or _ptr < 0x400000 or _ptr in _seen:
                        break
                    _seen.add(_ptr)
                    _m = pm.read_uchar(_ptr + _WE_MODEL)
                    _models.append(_m)
                    if _m == _ULTIMATE_MODEL_ID and _found is None:
                        _found = (_ptr,
                                  pm.read_int(_ptr + _WE_POS + 0),
                                  pm.read_int(_ptr + _WE_POS + 4),
                                  pm.read_int(_ptr + _WE_POS + 8))
                    _ptr = pm.read_uint(_ptr + _WE_NEXT)
                if _found:
                    logger.info(f"[weapons] Ultimate MODEL LOADED at 0x{_found[0]:08X} "
                                f"pos(X,Z,Y)={_found[1]},{_found[2]},{_found[3]}")
                else:
                    logger.info(f"[weapons] Ultimate MODEL NOT LOADED "
                                f"** the crash call has no entity to run on **"
                                f"  (loaded models: {sorted(set(_models))})")
            except Exception as exc:
                logger.info(f"[weapons] entity scan failed: {exc}")
            if module == GAME_MODULE_BATTLE:
                formation = pm.read_ushort(BATTLE_FORMATION_ADDR)
                logger.info(
                    f"[weapons] IN BATTLE — formation id = {formation} "
                    f"(Ultimate final=287, chase=282-286/294; "
                    f"Ruby=982/983, Emerald=984/985/986)"
                )
        except Exception as exc:
            logger.warning(f"[weapons] failed: {exc}")
        return True

    def _cmd_resync(self) -> bool:
        """Re-deliver every AP item you've received. Use this after a game over
        in Free Roam: a game over reloads the baseline and wipes your delivered
        items, and /resync restores your key items, vehicles, party members,
        chocobos and inventory. (The client also does this automatically when it
        detects you've returned to play after a Game Over.) Safe to run anytime —
        stackable items and materia are reconciled to their AP-granted total, so
        anything still in your inventory is left alone (only the missing amount is
        added)."""
        n = _requeue_all_received_items(self.ctx)
        if n:
            logger.info(f"Re-delivering {n} received AP item(s) on the next tick…")
        else:
            logger.info("No received AP items to re-deliver yet.")
        return True

    def _summon_vehicle(self, label: str, model_id: int,
                        slot: Tuple[int, int]) -> bool:
        """Shared body for /highwind and /chocobo: drop the given world-map model
        on the player's feet. `slot` is the savemap parked-coord slot to rewrite so
        the move survives a world-map reload — pass None for something that only
        exists as live world state (a dismounted chocobo)."""
        pm = getattr(self.ctx, "pm", None)
        if pm is None:
            logger.info(f"[{label}] Not attached to FF7 — start the game first.")
            return True
        try:
            if pm.read_uchar(GAME_MODULE_ADDR) != GAME_MODULE_WORLD:
                logger.info(f"[{label}] You need to be on the world map "
                            "(not in a field, battle or menu).")
                return True
        except Exception as exc:
            logger.info(f"[{label}] Could not read the game state: {exc}")
            return True
        pos = _read_world_player_pos(pm)
        if pos is None:
            logger.info(f"[{label}] Could not read your world-map position — "
                        "move a step and try again.")
            return True
        moved = _move_world_entity(pm, model_id, pos)
        if slot is not None:
            _repark_savemap_coord(pm, slot, model_id, pos)
        if moved:
            logger.info(f"[{label}] Moved to your location (X={pos[0]}, Y={pos[2]}).")
            return True

        # Couldn't find the entity. Report the position we used and list what the
        # walk actually reached — that separates the two ways this fails:
        #   * the position printed here is NOT where you are  -> 0xE04918 went stale
        #     (it stops tracking you while you're riding something).
        #   * the position is right but model {model_id} is missing from the list
        #     -> the entity is no longer reachable, because 0xE39AD8 is the CURRENT
        #     entity and mounting re-points it past the vehicle in the chain.
        if slot is not None:
            # Not an error: FF7 keeps only a limited pool of world models loaded, so
            # this vehicle's entity can be evicted when something else needs the slot
            # — a chocobo left standing on the map is the common case (confirmed
            # 2026-07-22: /highwind works while riding, and the Highwind drops out of
            # the entity array the moment you dismount).
            #
            # Queue the move rather than relying on a world-map reload: the game
            # writes live world state back over these savemap slots on the
            # world->field transition, so re-parking alone does NOT survive (the
            # Highwind stayed put after entering and leaving a location).
            ctx = self.ctx
            if not hasattr(ctx, "_pending_vehicle_moves"):
                ctx._pending_vehicle_moves = {}
            ctx._pending_vehicle_moves[model_id] = pos
            logger.info(f"[{label}] Not loaded right now — queued. It will move to "
                        f"X={pos[0]}, Y={pos[2]} as soon as the game loads it again.")
            logger.info(f"[{label}] A chocobo standing on the map is using the model "
                        "slot: ride it (or enter and leave a location) and the move "
                        "applies straight away.")
            if logger.isEnabledFor(10):     # DEBUG
                self._dump_world_entities(pm, label)
            return True

        logger.info(f"[{label}] Could not find world model id {model_id}. "
                    f"Position used: X={pos[0]}, Z={pos[1]}, Y={pos[2]}")
        logger.info(f"[{label}] Entities loaded right now:")
        self._dump_world_entities(pm, label)
        return True

    def _cmd_highwind(self) -> bool:
        """Move the Highwind to your current world-map location. Requires the
        Highwind (it must have been unlocked/received) and that you're standing
        on the world map."""
        pm = getattr(self.ctx, "pm", None)
        if pm is not None:
            # Ownership = the world_map_vehicles bit the client sets on delivery
            # (bank-1 addr 0x7F mask 0x10 -> savemap 0x0C23). Checking the game
            # rather than the AP item list also covers a vanilla-obtained Highwind.
            try:
                addr, mask, _ = VEHICLE_ITEM_FLAGS["Highwind"]
                if not (pm.read_uchar(_biton_byte_addr(1, addr)) & mask):
                    logger.info("[highwind] You do not have the Highwind yet.")
                    return True
            except Exception as exc:
                logger.debug(f"[highwind] ownership check failed: {exc}")
        return self._summon_vehicle("highwind", _VEHICLE_MODEL_IDS["Highwind"],
                                    _VEHICLE_SAVEMAP_SLOT[_VEHICLE_MODEL_IDS["Highwind"]])

    def _cmd_chocobo(self, model: str = "") -> bool:
        """Move your dismounted chocobo to your world-map location. Run it with no
        argument while a chocobo is out; if the client does not know which world
        model the chocobo is yet, it lists what is on the map so you can pass the
        id once (e.g. `/chocobo 9`), after which plain `/chocobo` works."""
        pm = getattr(self.ctx, "pm", None)
        if pm is None:
            logger.info("[chocobo] Not attached to FF7 — start the game first.")
            return True

        # A dismounted chocobo is LIVE world state only — it has no parked-coord
        # savemap slot (the `wchoco` slot this first tried is something else: it
        # reads empty while a chocobo is standing on the map). So it is found purely
        # by walking the world entity list, and the move is not persisted — ride off
        # or enter a location and the chocobo is gone, exactly as in vanilla.
        model_id: Optional[int] = None
        if model.strip():
            try:
                model_id = int(model.strip(), 0)
            except ValueError:
                logger.warning(f"[chocobo] bad model id: {model!r}")
                return True
        elif _CHOCOBO_MODEL_ID is not None:
            model_id = _CHOCOBO_MODEL_ID
        else:
            model_id = getattr(self.ctx, "_chocobo_model_id", None)

        if model_id is None:
            logger.info("[chocobo] I do not know which world model your chocobo is "
                        "yet. Live entities on the map right now:")
            self._dump_world_entities(pm)
            logger.info("[chocobo] Find the entity that appears when you dismount, "
                        "then run `/chocobo <model id>` once.")
            return True

        # 0x0F6C is the Tiny Bronco / Chocobo parked-coord slot (ff7-flat-wiki
        # Savemap, B[13][200]); the packed model id decides which of the two the
        # game spawns, so writing the chocobo's id there is safe. Free Roam drops
        # the Tiny Bronco anyway (invisible world model), so nothing contends here.
        ok = self._summon_vehicle("chocobo", model_id, _TINYBRONCO_CHOCOBO_SLOT)
        # Remember a working id for the rest of the session so plain /chocobo works.
        if model.strip():
            self.ctx._chocobo_model_id = model_id
        return ok

    def _dump_world_entities(self, pm: "pymem.Pymem", label: str = "chocobo") -> None:
        """List the live world-map entities (model id, position, distance from the
        player). Used to identify an unknown model, and to show why a vehicle move
        found nothing — note this walks forward from 0xE39AD8, which holds the
        CURRENT entity rather than a guaranteed list head, so anything earlier in
        the chain would not appear here."""
        pos = _read_world_player_pos(pm)
        try:
            ptr = pm.read_uint(_WORLD_ENTITY_PTR)
        except Exception as exc:
            logger.info(f"[{label}] could not walk the entity list: {exc}")
            return
        known = {v: k for k, v in _VEHICLE_MODEL_IDS.items()}
        known.setdefault(_CHOCOBO_MODEL_ID, "Chocobo")
        seen: Set[int] = set()
        for _ in range(48):
            if ptr == 0 or ptr < 0x400000 or ptr in seen:
                break
            seen.add(ptr)
            try:
                mid = pm.read_uchar(ptr + _WE_MODEL)
                ex = pm.read_int(ptr + _WE_POS + 0)
                ey = pm.read_int(ptr + _WE_POS + 8)
                dist = ""
                if pos is not None:
                    dist = "  dist=%d" % int(((ex - pos[0]) ** 2
                                              + (ey - pos[2]) ** 2) ** 0.5)
                logger.info(f"[{label}]   0x{ptr:08X} model_id={mid:3d} "
                            f"pos=({ex}, {ey}){dist}  {known.get(mid, '')}")
                ptr = pm.read_uint(ptr + _WE_NEXT)
            except Exception:
                break

    def _cmd_parkcoord(self, vehicle: str = "", mode: str = "") -> bool:
        """[Debug] Inspect or corrupt a vehicle's savemap parked coord, to test the
        self-repair pass. `/parkcoord` shows the slots decoded; `/parkcoord sub zero`
        or `/parkcoord sub legacy` puts the slot into a known-bad state so you can
        watch it heal. Vehicles: sub, highwind."""
        if not self._require_debug():
            return True
        pm = getattr(self.ctx, "pm", None)
        if pm is None:
            logger.warning("[parkcoord] Not attached to FF7.")
            return True
        by_name = {"sub": "Submarine", "submarine": "Submarine",
                   "highwind": "Highwind", "bh": "Highwind"}

        def _show() -> None:
            for nm, mid in _VEHICLE_MODEL_IDS.items():
                slot = _VEHICLE_SAVEMAP_SLOT.get(mid)
                if slot is None:
                    continue
                try:
                    c1 = pm.read_uint(SAVEMAP_BASE + slot[0])
                    c2 = pm.read_uint(SAVEMAP_BASE + slot[1])
                except Exception as exc:
                    logger.info(f"[parkcoord] {nm}: read failed ({exc})")
                    continue
                pid, x, y = (c1 >> 19) & 0x1F, c1 & 0x7FFFF, c2 & 0x3FFFF
                ang, z = (c1 >> 24) & 0xFF, (c2 >> 18)
                good = _VEHICLE_FIXED_POS.get(mid)
                sane = (pid == mid and (x or y)
                        and (x, y) not in _VEHICLE_LEGACY_BAD_SPOTS)
                owned = nm in getattr(self.ctx, "_received_item_names", set())
                if not owned:
                    state = "not received - repair skipped"
                elif sane:
                    state = "OK"
                else:
                    state = "INVALID -> will be repaired"
                # ang/Z included because they identify the WRITER: the client always
                # writes ang=16, Z=0, so anything else came from the game itself.
                logger.info(f"[parkcoord] {nm:<10} id={pid:<3} X={x:<7} Y={y:<7} "
                            f"Z={z:<6} ang={ang:<4} {state}"
                            + (f"   (spawn X={good[0]} Y={good[2]})" if good else ""))

        if not vehicle.strip():
            _show()
            logger.info("[parkcoord] Usage: /parkcoord <sub|highwind> "
                        "<zero|legacy|natural>")
            return True
        name = by_name.get(vehicle.strip().lower())
        mode = mode.strip().lower()
        if name is None or mode not in ("zero", "legacy", "natural"):
            logger.warning("[parkcoord] Usage: /parkcoord <sub|highwind> "
                           "<zero|legacy|natural>")
            return True
        if name not in getattr(self.ctx, "_received_item_names", set()):
            logger.warning(f"[parkcoord] {name} hasn't been received — the repair "
                           "pass only manages vehicles you actually own.")
            return True
        mid = _VEHICLE_MODEL_IDS[name]
        slot = _VEHICLE_SAVEMAP_SLOT[mid]
        if mode == "natural":
            # Clear the slot AND stop the client writing it, so the game's own
            # init decides where the vehicle goes. Session-only; reconnect resets.
            _SUPPRESS_VEHICLE_COORD.add(mid)
            try:
                pm.write_uint(SAVEMAP_BASE + slot[0], 0)
                pm.write_uint(SAVEMAP_BASE + slot[1], 0)
            except Exception as exc:
                logger.warning(f"[parkcoord] write failed: {exc}")
                return True
            logger.info(f"[parkcoord] {name}: slot cleared and client writes "
                        "SUPPRESSED for this session. Reload the world map (enter "
                        "and leave a location), then run /wdump to see where the "
                        "game puts it on its own.")
            return True
        if mode == "zero":
            c1 = c2 = 0
        else:
            bad = next(iter(_VEHICLE_LEGACY_BAD_SPOTS))
            c1 = (bad[0] & 0x7FFFF) | ((mid & 0x1F) << 19) | (16 << 24)
            c2 = bad[1] & 0x3FFFF
        try:
            pm.write_uint(SAVEMAP_BASE + slot[0], c1)
            pm.write_uint(SAVEMAP_BASE + slot[1], c2)
        except Exception as exc:
            logger.warning(f"[parkcoord] write failed: {exc}")
            return True
        logger.info(f"[parkcoord] {name} slot set to '{mode}'. It should repair on "
                    "the next poll (be in a field or on the world map):")
        _show()
        return True

    def _cmd_gomode(self) -> bool:
        """Show what you still need to lower the Northern Crater barrier — the
        Highwind, the full party and all four Huge Materia."""
        received = getattr(self.ctx, "_received_item_names", set())
        # Grouped so the output reads like a checklist rather than one long line.
        groups = (
            ("Highwind",     ["Highwind"]),
            ("Party",        ["Barret", "Tifa", "Aerith", "Red XIII",
                              "Cait Sith", "Cid"]),
            ("Huge Materia", ["Huge Materia (Fort Condor)", "Huge Materia (Corel)",
                              "Huge Materia (Underwater)", "Huge Materia (Rocket)"]),
        )

        def _short(n: str) -> str:
            if n.startswith("Huge Materia ("):
                return n[len("Huge Materia ("):-1]
            return n

        have_all = CRATER_REQUIRED_ITEMS.issubset(received)
        total   = len(CRATER_REQUIRED_ITEMS)
        got     = len(CRATER_REQUIRED_ITEMS & set(received))
        logger.info(f"[gomode] Northern Crater barrier: {got}/{total} goal items"
                    + ("  ** GO MODE — the barrier is down **" if have_all else ""))
        for title, names in groups:
            owned   = [n for n in names if n in received]
            missing = [n for n in names if n not in received]
            line = f"[gomode] {title:<13} {len(owned)}/{len(names)}"
            if missing:
                line += "   missing: " + ", ".join(_short(n) for n in missing)
            else:
                line += "   complete"
            logger.info(line)

        # The gate byte is what the field actually reads, so surface any
        # disagreement rather than letting the player trust the checklist alone.
        pm = getattr(self.ctx, "pm", None)
        if pm is not None:
            try:
                live = pm.read_uchar(SAVEMAP_BASE + CRATER_LOCK_OFFSET)
                if bool(live) != have_all:
                    logger.info(f"[gomode] (in-game gate byte is {live} — it syncs "
                                "on the next delivery tick)")
            except Exception as exc:
                logger.debug(f"[gomode] gate read failed: {exc}")
        else:
            logger.info("[gomode] (game not attached — showing AP-received items only)")
        return True

    def _cmd_keys(self) -> bool:
        """List the town keys you own (town gating). Shows every town key
        received from Archipelago and whether its world-map gate bit is set
        in the running game (they can briefly differ right after receiving a
        key, until the client's next delivery tick)."""
        # Town keys = the KEY_ITEM_FLAGS entries living in the relocated
        # town-gate bytes (rel 0x403/0x404 = savemap 0xFA7/0xFA8).
        town_keys = [(name, flags[0]) for name, flags in KEY_ITEM_FLAGS.items()
                     if flags and flags[0][0] in (0x403, 0x404)]
        received = getattr(self.ctx, "_received_item_names", set())
        pm = getattr(self.ctx, "pm", None)
        live: Dict[str, bool] = {}
        if pm is not None:
            try:
                for name, (rel, bit) in town_keys:
                    live[name] = bool(pm.read_uchar(_biton_byte_addr(1, rel)) & (1 << bit))
            except Exception:
                live = {}
        owned = [n for n, _ in town_keys if n in received or live.get(n)]
        missing = [n for n, _ in town_keys if n not in owned]
        def _short(n: str) -> str:
            return n[:-4] if n.endswith(" Key") else n
        logger.info(f"[keys] Town keys owned ({len(owned)}/{len(town_keys)}): "
                    + (", ".join(_short(n) for n in owned) if owned else "none"))
        if missing:
            logger.info("[keys] Missing: " + ", ".join(_short(n) for n in missing))
        # Flag keys received but not yet applied in-game (delivery pending).
        pending = [n for n in owned if n in received and live and not live.get(n)]
        if pending:
            logger.info("[keys] Received but not yet applied in-game (will apply "
                        "on the next delivery tick): "
                        + ", ".join(_short(n) for n in pending))
        if pm is None:
            logger.info("[keys] (game not attached — showing AP-received keys only)")
        return True

    def _cmd_trap(self, name: str = "") -> bool:
        """Manually fire a trap by name, e.g. `/trap frog` or `/trap poison`. The
        trap goes through the normal queue, so a battle-only trap fires on your
        next battle. Run `/trap` with no name to list the available traps."""
        from worlds.ff7.TrapLink import TRAP_REGISTRY, resolve_trap

        ctx = self.ctx
        available = ", ".join(sorted(s.name for s in TRAP_REGISTRY.values()))
        query = name.strip()
        if not query:
            logger.info(f"Usage: /trap <name>.  Available traps: {available}")
            return True
        spec = resolve_trap(query)
        if spec is None:
            logger.warning(f"Unknown trap '{query}'.  Available traps: {available}")
            return True
        ctx._trap_queue.append(spec)
        when = "on your next battle" if spec.battle_only else "shortly"
        logger.info(f"Queued {spec.name} - it will fire {when}.")
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

    def make_gui(self):
        # Rename the window from the default "Archipelago Text Client".
        ui = super().make_gui()
        ui.base_title = "Archipelago Final Fantasy VII Client"
        return ui

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
        # Fields in _FIELD_FLAG_RESET_ONCE whose entry reset has already been
        # applied this game. Cleared on a fresh Free Roam start so a new game
        # gets its one reset back.
        self._flag_reset_once_done: Set[str] = set()
        # Live pymem handle (set by game_watcher) so debug commands can read memory.
        self.pm = None
        # Model ids of delivered vehicles still needing relocation off the (0,0)
        # sea tile (only specific vehicles, so we never disturb the submarine etc).
        self._pending_vehicle_models: Set[int] = set()
        # model_id -> (X, Z, Y) for /highwind moves awaiting a loaded entity
        self._pending_vehicle_moves: Dict[int, Tuple[int, int, int]] = {}

        settings = _load_settings()
        stored_dir  = settings.get("ff7_dir")
        stored_json = settings.get("json_path")
        self.ff7_dir:  Optional[Path] = Path(stored_dir)  if stored_dir  else None
        self.json_path: Optional[Path] = Path(stored_json) if stored_json else None
        self.biton_map: Dict[int, Tuple[int, int, int]] = {}
        # Item delivery state (persists across poll cycles)
        self._delivered_item_indices: Set[int] = set()
        self._pending_items: List[Tuple[int, object]] = []
        # Set while re-delivering the full item set (/resync or game-over recovery).
        # In this mode, stackable items + materia are RECONCILED to their AP-granted
        # target quantity (only the missing amount is added) instead of blindly
        # stacked, so re-syncing with items still present can't duplicate them.
        # Normal incremental delivery leaves this False (plain additive writes).
        self._resync_reconcile: bool = False
        # ── trap queue (see worlds/ff7/TrapLink.py) ──────────────────────────
        # traps ride the item pipeline into _trap_queue and TrapLink.pump_trap_queue
        # fires one per tick. _seen_trap_indices is never cleared, so a game-over
        # re-deliver can't re-trigger a trap.
        self._trap_queue: "deque[TrapSpec]" = deque()
        self._priority_trap: Optional["TrapSpec"] = None   # inbound traplink (latest wins)
        self._seen_trap_indices: Set[int] = set()
        self._last_trap_activation: float = 0.0
        self._bomb_field_reset: Optional[int] = None   # field obj ptr awaiting post-battle reset
        # ── deathlink (see worlds/ff7/DeathLink.py) ──────────────────────────
        self._pending_kill: Optional[str] = None       # inbound death, applied in battle
        self._deathlink_kill_time: float = 0.0         # when we last applied an inbound kill
        self._death_sent_this_over: bool = False       # one outbound send per game over
        # Free Roam game-over recovery: latched when the live module hits the
        # Game Over screen (26); once gameplay resumes for a few stable ticks we
        # re-deliver every received item, because the game over reloaded the
        # wiped md1stin baseline. _resume_debounce counts the post-resume ticks.
        self._game_over_seen: bool = False
        # True while sitting in md1stin after a fresh Free Roam start; drives the
        # re-delivery once the opening field is left (see _pump_reseed_resync).
        self._reseed_pending: bool = False
        self._resume_debounce: int = 0
        # Delivery queue gate: items are only flushed to game memory while the
        # player is in the FIELD or WORLD module for two consecutive polls.
        # Battle and menu modules keep their own working copies of inventory/
        # party state and write them back over the savemap on exit, silently
        # losing anything the client wrote mid-module. _last_module tracks the
        # module byte across polls; _delivery_held_logged de-spams the hold log.
        self._last_module: Optional[int] = None
        self._delivery_held_logged: bool = False
        # Self-healing party-data rebuild. A client-inserted member's limit
        # TECHNIQUE list is only built by the engine's party-refresh fns, and the
        # call made at delivery no-ops for the FIRST member (party goes 1->2 while
        # the field is still loading). We re-run the rebuild once per stable party
        # composition: _party_sig is the last-seen 3-slot party byte-triple,
        # _party_sig_stable counts consecutive identical polls, and
        # _party_rebuilt_sig is the composition we last rebuilt (so each new one
        # rebuilds exactly once, a few polls after it settles).
        self._party_sig: bytes = b""
        self._party_sig_stable: int = 0
        self._party_rebuilt_sig: bytes = b""
        # Boss checks that have been sent (location_id)
        self._boss_checks_sent: Set[int] = set()
        # Weapon-boss kill latched from a battle formation, applied to
        # weapons_killed once the player exits the battle to gameplay.
        self._weapon_kill_pending: int = 0
        # Polls remaining for _weapon_kill_pending to stay valid. Without this the
        # mask survived a LOST fight and fired later, handing the player a free
        # Ruby/Emerald check "when you load back into the game" (report 2026-07-27).
        self._weapon_kill_ticks: int = 0
        # Latched while in Ultimate's final battle (287); drained on the way
        # out to register the kill the game does not register itself.
        # DORMANT since 2026-07-27 — nothing sets or reads this while the client's
        # Ultimate kill registration is removed. Kept so restoring it is a one-liner.
        self._ultimate_final_pending: bool = False
        # Ultimate: latched while in battle 287, drained on the way out to
        # arm his death; then a grace countdown for the game to perform it.
        # True while Ultimate's kill has been written on entering battle
        # 287 but the player has not yet come out of it alive.
        # Baseline established once per game connection: locations whose detection
        # bit is already set at connect (Free Roam starts at game moment 1603,
        # which leaves savemap progress noise). Suppressed so we never
        # false-report them as fresh checks.
        self._baseline_locations: Set[int] = set()
        self._baseline_established: bool = False
        # Reverse index (savemap-rel offset, bit) -> [location codes] built from
        # biton_map, so a CLIENT-driven flag write (gate/force/key-item flag) can
        # suppress any location that shares that exact bit (else the client setting
        # a gate flag fires the original pickup location as a phantom check).
        self._biton_rev: Dict[Tuple[int, int], List[int]] = {}
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
        # AP shop slots keyed by (shop_id, section, token_id) — see ShopKey. The
        # shop id makes detection unambiguous vs. the same real id elsewhere.
        self.shop_ap_locations: Dict[ShopKey, int] = {}
        # (shop_id, section, token_id) -> display name / description, parsed from
        # the .apff7 shops array; (re)written into shop_ap.txt for the DLL.
        self._shop_ap_names: Dict[ShopKey, str] = {}
        self._shop_ap_descs: Dict[ShopKey, str] = {}
        # Path to shophook.dll's shop_buys.txt purchase-signal file (set at the
        # exe dir when the hook is injected); polled + consumed each game tick.
        self._shop_buys_path: Optional[Path] = None
        # Last sold-cell set written to shop_sold.txt (the DLL removes these from
        # shop stock so obtained AP items can't be re-bought). Rewritten when it
        # changes so mid-session purchases drop out of the shop on the next visit.
        self._shop_sold_written: "frozenset[ShopKey]" = frozenset()

    async def server_auth(self, password_requested: bool = False) -> None:
        await super().server_auth(password_requested)
        if not self.auth:
            await self.get_username()
            await self.send_connect()

    def on_package(self, cmd: str, args: dict) -> None:
        super().on_package(cmd, args)
        if cmd == "Connected":
            # When the handshake happened. Archipelago replays the whole item
            # history immediately after Connected, so an index==0 packet arriving
            # inside this window is that replay; one arriving later is a genuine
            # first receipt. See _HISTORY_SYNC_WINDOW_S.
            self._connected_at = time.monotonic()
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
                self.shop_ap_locations, self._shop_ap_names, self._shop_ap_descs = \
                    _shops_from_apff7(raw_shops)
                if self.shop_ap_locations:
                    logger.debug(
                        f"Shop slots from server: {len(self.shop_ap_locations)}"
                        " AP shop check(s)."
                    )
            else:
                self._load_shops_from_json()
            # deathlink / traplink: enable via connection tags. the flags come from
            # slot_data["options"], and on_package is sync so the ConnectUpdate is
            # scheduled as a task.
            opts = sd.get("options", {})
            if opts.get("death_link"):
                self.tags.add("DeathLink")
            if opts.get("trap_link"):
                self.tags.add("TrapLink")
            if "DeathLink" in self.tags or "TrapLink" in self.tags:
                async_start(self.send_msgs([{"cmd": "ConnectUpdate", "tags": list(self.tags)}]),
                            name="ff7-link-tags")
            # Restore which traps have already fired for THIS seed+slot. Done here
            # (not at __init__) because the key needs seed_name and auth, which only
            # exist once Connected has been handled.
            _fired = _load_fired_traps(self)
            if _fired:
                self._seen_trap_indices |= _fired
                logger.debug(f"Restored {len(_fired)} already-fired trap(s) for "
                             f"this slot — they will not re-fire.")
        elif cmd == "Bounced":
            from worlds.ff7.TrapLink import handle_bounced
            handle_bounced(self, args)
        elif cmd == "ReceivedItems":
            # Queue items for delivery on the next in-field game_watcher tick.
            # Register names immediately (not at delivery) so gates that read
            # _received_item_names — crater lock, character counts — don't lag
            # behind while deliveries are held for the field module.
            index = args.get("index", 0)
            # An index==0 packet is Archipelago RE-SENDING the full item history
            # (every (re)connect and resync). Two things must not treat it as
            # "fresh receipts": (1) stackables/materia must RECONCILE to the AP
            # target instead of stacking on top of a save that survived (a full
            # game+client restart re-sends everything → duplicated materia); and
            # (2) TRAPS in the history must not re-fire — the player already
            # experienced them. We pre-mark those trap indices as seen so the
            # delivery pass skips them. (Trade-off: a trap granted while the
            # client was fully closed won't fire on reconnect — acceptable versus
            # re-triggering every trap on every restart.)
            code_map = _get_code_to_item_name()
            # index==0 means EITHER "Archipelago is replaying the full history"
            # (every reconnect/resync) OR "this is your very first item ever" —
            # the wire format is identical, because there is nothing before
            # index 0 in either case. Treating both as a replay meant a new
            # player's FIRST item was pre-marked as already-seen: a first TRAP
            # never fired (reported 2026-08-01, Frog Trap), and a first stackable
            # took the reconcile path instead of the grant path.
            #
            # Timing separates them: the replay is part of the connection
            # handshake and lands within a second or so of Connected, while a real
            # receipt needs the player to go and check a location. Anything
            # outside the window is a fresh receipt.
            #
            # Deliberately NOT keyed on `self._delivered_item_indices` being
            # non-empty: a RESTARTED client has delivered nothing yet, so that
            # test would classify the reconnect replay as fresh and re-fire every
            # trap plus duplicate every stackable — worse than the bug it fixes.
            _since_connect = time.monotonic() - getattr(self, "_connected_at", 0.0)
            _is_full_resync = (index == 0 and args.get("items")
                               and _since_connect <= _HISTORY_SYNC_WINDOW_S)
            if _is_full_resync:
                self._resync_reconcile = True
            from worlds.ff7.TrapLink import TRAP_REGISTRY
            for offset, net_item in enumerate(args.get("items", [])):
                item_index = index + offset
                if item_index not in self._delivered_item_indices:
                    self._pending_items.append((item_index, net_item))
                item_code = getattr(net_item, "item", None)
                item_name = (code_map.get(item_code) if isinstance(item_code, int)
                             else item_code if isinstance(item_code, str) else None)
                if item_name:
                    self._received_item_names.add(item_name)
            # Traps are NO LONGER pre-marked from the packet shape — whether one has
            # already fired is read from the persisted per-slot record instead
            # (_load_fired_traps, on Connected). `_is_full_resync` now only governs
            # stackable/materia RECONCILIATION, which genuinely does depend on
            # whether this is a replay.

    def on_deathlink(self, data: dict) -> None:
        """inbound deathlink: kill a random party member on the next battle tick."""
        super().on_deathlink(data)          # updates last_death_link + logs
        self._pending_kill = data.get("cause") or f"death from {data.get('source', 'another world')}"

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
            data = _read_apff7_json(json_path)
            self.biton_map = _biton_map_from_placements(data.get("placements", []))
            self.shop_ap_locations, self._shop_ap_names, self._shop_ap_descs = \
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
            data = _read_apff7_json(json_path)
            self.shop_ap_locations, self._shop_ap_names, self._shop_ap_descs = \
                _shops_from_apff7(data.get("shops", []))
            if self.shop_ap_locations:
                logger.debug(
                    f"Shop slots loaded: {len(self.shop_ap_locations)} AP shop check(s)."
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


def _suppress_client_flag_locations(ctx: "FF7Context", rel_offset: int, bit: int) -> None:
    """Mark any tracked location whose detection BITON is exactly (rel_offset, bit)
    as already-handled, so a CLIENT-driven savemap write (item-gate flag, force
    flag, key-item flag) can't fire the original pickup location as a phantom
    check. ``rel_offset`` is relative to SAVEMAP_BASE — i.e.
    ``_biton_byte_addr(bank, addr) - SAVEMAP_BASE``. No-op until the reverse index
    (_biton_rev) is built at baseline. Legitimate in-game pickups are unaffected:
    they set the same bit, but detection (run at the top of the poll) fires them
    before the client's write/suppression at the bottom of the poll."""
    for code in ctx._biton_rev.get((rel_offset, bit), ()):
        ctx._checked_this_session.add(code)


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
    # The 4 Huge Materia grant their possession bit (key-items menu) ONLY on AP
    # receipt — the in-game pickups now fire DEDICATED detection flags instead of
    # these possession bits (Underwater/Fort Condor moved to bank13 0x90.0/0.1 in
    # locations.json; Corel=0x80.6, Rocket=bank13 0x91.3 already separate). So
    # setting 0x42.4-7 here no longer collides with any location's detection flag:
    # the in-game pickup fires the check WITHOUT adding the materia to the menu, and
    # AP receipt adds it to the menu WITHOUT pre-firing the check. (Needs a fresh
    # seed so Gold Saucer relocates the pickup BITONs to the new detection flags.)
    "Huge Materia (Fort Condor)": [(0x42, 4)],
    "Huge Materia (Corel)":       [(0x42, 5)],
    "Huge Materia (Underwater)":  [(0x42, 6)],
    "Huge Materia (Rocket)":      [(0x42, 7)],
    # Town gating (town_gating option): each town key sets a FREE savemap bit that the
    # world-map ENTER_FIELD gate checks (Gold Saucer inserts PUSH_SAVEMAP_BIT).
    # RELOCATED 2026-07-18: the old rel bytes 0x184/0x185 (= savemap 0xD28/0xD29)
    # are the field-script story vars Var[3][132]/[133] — LIVE vanilla state! The
    # Forgotten City (lost1 etc.) reads 0xD28 bits 4/5 for its Aerith-death-night
    # variant (Nibelheim + Rocket Town keys turned the city blue/death-prepped),
    # and yougan2 CLEARS 0xD29.0 (would delete the Mideel key). New home: rel
    # 0x403/0x404 (= savemap 0xFA7/0xFA8, pure script-flag bank) — verified free
    # by scanning ALL 702 field scripts + every wm*.ev for savemap references.
    # MUST match GS patchTownGates towns[]: bitIndex = relByte*8 + bit.
    "Fort Condor Key":            [(0x403, 0)],
    "Junon Key":                  [(0x403, 1)],
    "North Corel Key":            [(0x403, 2)],
    "Cosmo Canyon Key":           [(0x403, 3)],
    "Nibelheim Key":              [(0x403, 4)],
    "Rocket Town Key":            [(0x403, 5)],
    "Wutai Key":                  [(0x403, 6)],
    "Icicle Inn Key":             [(0x403, 7)],
    "Mideel Key":                 [(0x404, 0)],
    "Gongaga Key":                [(0x404, 1)],
    "Bone Village Key":           [(0x404, 2)],
    "Costa del Sol Key":          [(0x404, 3)],
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
# NOTE: We no longer set the mds5_5 walkmesh gate flag (Var[15][38].3 = 0x0FCA.3)
# from the client. That flag is ALSO the Bone Village "Key To Sector 5" pickup's
# detection bit, so setting it on receipt broke that AP check (same failure mode
# as Glacier Map / Snowboard). Instead, Gold Saucer re-gates the mds5_5 entry
# triangle on the key-item POSSESSION bit Var[1][0x43].5 (set by KEY_ITEM_FLAGS
# below), so Midgar entry opens from holding the key while 0x0FCA.3 is freed for
# the check. The old gate-flag helper is gone; see the Gold Saucer FieldPickup
# patch "SECTOR5_GATE" (mds5_5 entry triangle repointed to Var[1][0x43].5).
# Historically the client set Var[15][38].3 (0x0FCA.3) here; mds5_5 gated the
# Midgar-entry triangle on it ("If Var[15][38] bitOFF 3 -> deactivate triangle
# #4"). That bit doubles as the Bone Village pickup's detection flag, hence the
# move to gating on possession instead.


# NOTE: We deliberately do NOT set the "Snowboard key item obtained" story flag
# (Var[1][130].1 = 0xC26.1) on Snowboard delivery. That flag is ALSO the in-game
# pickup's detection flag for location 310018 ("Icicle Inn - Snowboard"): setting
# it on receipt made the field hide the pickup, leaving the AP check unobtainable
# (same failure mode as Glacier Map). No snow field reads Var[1][130] for
# progression (verified by full-game field scan); the snowboard itself is granted
# via the key-item POSSESSION bit (KEY_ITEM_FLAGS, 0x46.2), so progression is
# unaffected and the location is once again checkable.


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
        # Key to Sector 5 needs no special flag: Gold Saucer re-gates the mds5_5
        # Midgar-entry walkmesh on the possession bit (0x43.5) set just above.
        # Snowboard intentionally sets no story flag here — see note above.
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

# Ultimate Weapon's world-map model. Read-only, for the `/weapons` presence check:
# his entity must exist for highwind_init's `call_function(ultima_weapon, 27)` —
# the crater crash — to run at all.
_ULTIMATE_MODEL_ID = 11

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
    # Submarine — Junon dock surface spot, captured live from a player-parked
    # sub (2026-07-09). The previous spot (170091, 149648) was ~200 units off
    # and clipped the sub into the dock geometry (stuck on delivery).
    13: (169884, -240, 149694),
}
# Model ids safe to drop on the player's position (flying vehicles only).
_VEHICLE_PLAYER_OK: frozenset = frozenset()

# (X, Y) spots earlier client builds wrongly spawned vehicles at; a queued
# vehicle found here is migrated to its proper target.
_VEHICLE_LEGACY_BAD_SPOTS: frozenset = frozenset({
    (170091, 149648),   # old Submarine spawn — clipped into the Junon dock
})

# Savemap parked-vehicle coord slots (FF7SLOT offsets) by model id. The game
# spawns the parked vehicle's MODEL from the id packed into this coord.
# Packing (ff7tk): chunk1 = X(&0x7FFFF) | id<<19 | angle<<24; chunk2 = Y(&0x3FFFF) | Z<<18.
# Offsets confirmed against the ff7-flat-wiki Savemap table: each 8-byte slot is
# SHARED by two vehicles and the packed model id selects which one spawns.
#   0x0F6C = Tiny Bronco / Chocobo   0x0F74 = Buggy / Highwind
_VEHICLE_SAVEMAP_SLOT: Dict[int, Tuple[int, int]] = {
    3:  (0x0F74, 0x0F78),   # Highwind  -> Buggy/Highwind slot (wiki B[13][208])
    13: (0x0F7C, 0x0F80),   # Submarine -> sub_world / sub_world2
    19: (0x0F6C, 0x0F70),   # Chocobo   -> Tiny Bronco/Chocobo slot (wiki B[13][200])
}


# Debug: model ids the client must NOT write a parked coord for this session
# (`/parkcoord <vehicle> natural`). Lets us see where the game's own wm0.ev init
# puts a vehicle when we stay out of the way — the flags (owned + tut_sub +
# 0xEF6.2 + Junon mesh for the sub) are what drive its spawn, so our coord write
# may be overriding a correct dock placement with a stale constant at Z=0.
_SUPPRESS_VEHICLE_COORD: Set[int] = set()


def _write_vehicle_savemap_coord(pm: "pymem.Pymem", model_id: int,
                                 force: bool = False) -> None:
    """Populate the savemap parked-vehicle coord (with the model id) so the game
    loads the vehicle's model on the next world-map (re)spawn — fixes the
    'usable but invisible' vehicle. Z is left 0; the game derives water height.

    Only writes when the slot does NOT already hold a valid parked coord for this
    model. Item delivery is re-run wholesale after a Free Roam game over and on
    /resync (`_delivered_item_indices.clear()`), and this used to overwrite the
    slot with the FIXED SPAWN every time — silently teleporting a Highwind the
    player had moved with /highwind (or simply flown somewhere and parked) back to
    its spawn on the next world-map load. A genuine game over re-seeds the savemap
    from the md1stin baseline, so the slot reads empty there and the spawn coord is
    still written as intended."""
    slot = _VEHICLE_SAVEMAP_SLOT.get(model_id)
    target = _VEHICLE_FIXED_POS.get(model_id)
    if slot is None or target is None or model_id in _SUPPRESS_VEHICLE_COORD:
        return
    if not force:
        try:
            c1 = pm.read_uint(SAVEMAP_BASE + slot[0])
            c2 = pm.read_uint(SAVEMAP_BASE + slot[1])
            parked_id = (c1 >> 19) & 0x1F
            parked_x, parked_y = c1 & 0x7FFFF, c2 & 0x3FFFF
            if parked_id == model_id and (parked_x or parked_y):
                logger.debug(f"vehicle {model_id} already parked at "
                             f"({parked_x}, {parked_y}) — keeping the player's position")
                return
        except Exception as exc:
            logger.debug(f"parked-coord read failed for {model_id}: {exc}")
    x, _z, y = target
    chunk1 = (x & 0x7FFFF) | ((model_id & 0x1F) << 19) | ((16 & 0xFF) << 24)
    chunk2 = (y & 0x3FFFF)
    try:
        pm.write_uint(SAVEMAP_BASE + slot[0], chunk1)
        pm.write_uint(SAVEMAP_BASE + slot[1], chunk2)
    except Exception as exc:
        logger.debug(f"savemap vehicle coord write failed: {exc}")


# World-map model id of a dismounted chocobo — 19, confirmed in-game 2026-07-22 by
# walking the live entity list next to a chocobo that had just been ridden out.
# /chocobo still accepts an explicit id (`/chocobo 20`) in case a different chocobo
# type or game state uses another model; a passed id overrides this for the session.
# (The `wchoco` savemap slot at 0x0F64 this first tried is NOT the dismounted
# chocobo — it reads empty while one is standing on the map. A dismounted chocobo
# is live world state only, so the move is not persisted across a world reload.)
_CHOCOBO_MODEL_ID: Optional[int] = 19

# Parked world-map coord slots, per the ff7-flat-wiki Savemap table. Both are 8
# bytes, B[13][200] / B[13][208], and each is SHARED between two vehicles — the
# packed model id in bits 19-23 is what tells the game which one to spawn:
#   0x0F6C = Tiny Bronco / Chocobo      0x0F74 = Buggy / Highwind
# (0x0F64, the `wchoco` label in /wdump, is NOT the chocobo — that guess was wrong.)
_TINYBRONCO_CHOCOBO_SLOT = (0x0F6C, 0x0F70)
_BUGGY_HIGHWIND_SLOT     = (0x0F74, 0x0F78)


def _read_world_player_pos(pm: "pymem.Pymem") -> Optional[Tuple[int, int, int]]:
    """Player's live world-map position as (X, Z, Y), or None if unavailable /
    not actually on the world map yet."""
    try:
        x = pm.read_int(_WORLD_PLAYER_POS + 0)
        z = pm.read_int(_WORLD_PLAYER_POS + 4)
        y = pm.read_int(_WORLD_PLAYER_POS + 8)
    except Exception:
        return None
    if x == 0 and y == 0:
        return None
    return (x, z, y)


def _move_world_entity(pm: "pymem.Pymem", model_id: int,
                       pos: Tuple[int, int, int]) -> bool:
    """Move the live world-map entity with `model_id` to (X, Z, Y). Returns False
    if no such entity is currently spawned."""
    try:
        ptr = pm.read_uint(_WORLD_ENTITY_PTR)
    except Exception:
        return False
    seen: Set[int] = set()
    for _ in range(48):
        if ptr == 0 or ptr < 0x400000 or ptr in seen:
            break
        seen.add(ptr)
        try:
            if pm.read_uchar(ptr + _WE_MODEL) == model_id:
                pm.write_int(ptr + _WE_POS + 0, pos[0])
                pm.write_int(ptr + _WE_POS + 4, pos[1])
                pm.write_int(ptr + _WE_POS + 8, pos[2])
                return True
            ptr = pm.read_uint(ptr + _WE_NEXT)
        except Exception:
            break
    return False


def _repark_savemap_coord(pm: "pymem.Pymem", slot: Tuple[int, int],
                          model_id: int, pos: Tuple[int, int, int]) -> None:
    """Rewrite a parked-vehicle savemap coord so the move survives a world-map
    reload. Packing (ff7tk): chunk1 = X | id<<19 | angle<<24, chunk2 = Y | Z<<18.
    The existing facing angle is preserved, and Z is left 0 exactly as
    `_write_vehicle_savemap_coord` does — the game derives the ground/water
    height itself, and Z is unsigned in this packing so a negative player Z
    (below sea level) would corrupt the Y field."""
    x, _z, y = pos
    try:
        angle = (pm.read_uint(SAVEMAP_BASE + slot[0]) >> 24) & 0xFF
        pm.write_uint(SAVEMAP_BASE + slot[0],
                      (x & 0x7FFFF) | ((model_id & 0x1F) << 19) | (angle << 24))
        pm.write_uint(SAVEMAP_BASE + slot[1], (y & 0x3FFFF))
    except Exception as exc:
        logger.debug(f"savemap re-park write failed: {exc}")


def _pump_vehicle_moves(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Apply queued `/highwind`-style moves that could not be done immediately.

    Writing the savemap parked coord and waiting for a world-map reload does NOT
    work on its own: the game writes the LIVE world state back into those slots on
    the world->field transition, so our value is overwritten before the field->world
    restore ever reads it (observed 2026-07-22 — the Highwind stayed put after
    entering and leaving a location).

    So a queued move is retried against the LIVE entity every tick instead. The
    entity comes back as soon as whatever evicted it releases the model slot (e.g.
    riding the chocobo that displaced it), and the move then applies instantly. The
    savemap slot is also refreshed while in a FIELD — that is past the point where
    the game clobbers it, so it survives into the next world load as a backstop.
    """
    pending: Dict[int, Tuple[int, int, int]] = getattr(ctx, "_pending_vehicle_moves", {})
    if not pending:
        return
    try:
        module = pm.read_uchar(GAME_MODULE_ADDR)
    except Exception:
        return

    if module == GAME_MODULE_WORLD:
        done = []
        for model_id, pos in list(pending.items()):
            try:
                if _move_world_entity(pm, model_id, pos):
                    slot = _VEHICLE_SAVEMAP_SLOT.get(model_id)
                    if slot is not None:
                        _repark_savemap_coord(pm, slot, model_id, pos)
                    done.append(model_id)
                    logger.info(f"[vehicle] model {model_id} is loaded again — "
                                f"moved to X={pos[0]}, Y={pos[2]}.")
            except Exception as exc:
                logger.debug(f"pending vehicle move failed for {model_id}: {exc}")
        for model_id in done:
            pending.pop(model_id, None)
    elif module == GAME_MODULE_FIELD:
        # Past the world->field save, so this write is not clobbered.
        for model_id, pos in pending.items():
            slot = _VEHICLE_SAVEMAP_SLOT.get(model_id)
            if slot is not None:
                _repark_savemap_coord(pm, slot, model_id, pos)


def _repair_vehicle_parked_coords(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Self-heal a delivered vehicle's PARKED COORD when it has been lost.

    `_place_stranded_vehicles` can only repair a vehicle whose LIVE ENTITY is
    currently resident — it writes the savemap coord from inside the
    entity-found branch. The world model pool is capacity-limited and evicts
    entities (see the Highwind/chocobo work), so a vehicle that isn't loaded
    right now is never repaired, and the bad coord persists across reloads.

    Three ways the coord goes bad, none of which the live-entity path catches:
      * never written — delivered while not on the world map, or the savemap was
        re-seeded from the md1stin baseline afterwards (a fresh Free Roam start
        right after an offline batch of items lands);
      * clobbered to (0,0) — the game writes LIVE world state back over these
        slots on the world->field transition, so a vehicle sitting unpositioned
        at (0,0) when the player enters a location persists that (0,0);
      * a legacy bad spot — the pre-fix Submarine coord that clipped inside the
        Junon dock. The keep-the-player's-position guard treats that as a valid
        parked coord, so it needs `force` to migrate.

    Runs in field AND world: the field case matters most, being past the point
    where the game clobbers these slots."""
    if not ctx.free_roam:
        return
    try:
        if pm.read_uchar(GAME_MODULE_ADDR) not in (GAME_MODULE_FIELD, GAME_MODULE_WORLD):
            return
    except Exception:
        return
    for name, mid in _VEHICLE_MODEL_IDS.items():
        if name not in ctx._received_item_names:
            continue
        slot = _VEHICLE_SAVEMAP_SLOT.get(mid)
        if slot is None or mid not in _VEHICLE_FIXED_POS:
            continue
        try:
            c1 = pm.read_uint(SAVEMAP_BASE + slot[0])
            c2 = pm.read_uint(SAVEMAP_BASE + slot[1])
        except Exception:
            continue
        parked_id = (c1 >> 19) & 0x1F
        x, y = c1 & 0x7FFFF, c2 & 0x3FFFF
        # Sane = right model, a real position, and not a known-bad spot. A player
        # who has driven the vehicle somewhere valid is preserved.
        if parked_id == mid and (x or y) and (x, y) not in _VEHICLE_LEGACY_BAD_SPOTS:
            continue
        _write_vehicle_savemap_coord(pm, mid, force=True)
        logger.debug(f"Repaired parked coord for {name} (was id={parked_id} "
                     f"({x}, {y})) -> spawn")


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


# Great Glacier "you woke up after the snowboard run" latch: Var[1][185] bit 0
# (savemap 0xBA4 + 185 = 0xC5D). hyou1/2/3/7's `init` gates the wake-up cutscene on
# it being CLEAR, and the cutscene's `event` script sets it (OR 0x01) once played.
# The snowboard minigame (`snow`/playgam) ZEROES the whole byte as you set off,
# which is what re-arms the cutscene for a legitimate descent.
#
# Free Roam lets the player walk into the Great Glacier from the world map with no
# snowboard at all. The latch is clear, so the arrival cutscene — written for the
# snowboard landing — plays anyway and repositions Cloud to the wake-up spot, which
# is nowhere near the on-foot entrance: he ends up off the walkmesh at the bottom
# of the screen, unable to move (menu still opens, because the script's MENU2 0
# runs). Playtester report 2026-07-22: entered on foot with a gold chocobo, had the
# Glacier Map but not the Snowboard.
#
# BIT 1 MUST NEVER BE SET. Decompiled hyou1 S0-Main (Makou Reactor, user 2026-07-31):
#     15  if Var[1][185] & 1  -> fade in           (bit 0 = wake-up already seen)
#     23  if Var[1][185] & 2  -> Var[1][185] &= 253
#     25       execute cloud script 3              (= Place field Model at
#                                                    Var[2][188/190/192/194])
# and lines 30-35 show what bit 1 is FOR: pressing SQUARE stores Cloud's position
# into Var[2][188](X)/190(Y)/192(Z)/194(triangle) and jumps to hyoumap (#669, the
# glacier map screen). **Bit 1 means "I am coming back from the map screen —
# restore my saved position."**
#
# On 2026-07-27 this constant was widened to 0x03 because hyou1 tests both bits.
# That was exactly backwards: setting bit 1 makes the field RESTORE Cloud to those
# saved coordinates, which are 0/0/0 triangle 0 for any player who never opened the
# map — i.e. off the walkmesh, unable to move. Captured live from a stuck player:
# 0x0C60..0x0C66 all read 0. Line 24 self-clears bit 1, so re-setting it every
# world-map poll stranded them on EVERY entry. Reverted the same session.
#
# Bit 0 is the only bit this client may touch, and it does the one thing we want:
# skip the snowboard-landing cutscene when walking in on foot.
_GLACIER_WAKEUP_ADDR = 0x0C5D
_GLACIER_WAKEUP_BIT  = 0x01      # bit 0 ONLY — see above, bit 1 strands the player


def _seed_glacier_wakeup(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Mark the Great Glacier wake-up as already seen WHILE ON THE WORLD MAP.

    Gating on the world map is what makes this safe: reaching the glacier on foot
    always goes through the world map, so the latch is set before entry and the
    cutscene is skipped. The snowboard route never touches the world map between
    `snow` zeroing the byte and arriving at hyou1, so a real descent still plays
    its wake-up exactly as designed. Idempotent — only writes when the bit is clear.
    """
    if not ctx.free_roam:
        return
    try:
        if pm.read_uchar(GAME_MODULE_ADDR) != GAME_MODULE_WORLD:
            return
        addr = SAVEMAP_BASE + _GLACIER_WAKEUP_ADDR
        v = pm.read_uchar(addr)
        # `!= mask` rather than `not (v & mask)` — equivalent for a single bit,
        # and it stays correct if the mask is ever revisited. Bit 1 is deliberately
        # NOT in the mask; see the comment on _GLACIER_WAKEUP_BIT.
        if (v & _GLACIER_WAKEUP_BIT) != _GLACIER_WAKEUP_BIT:
            pm.write_uchar(addr, v | _GLACIER_WAKEUP_BIT)
            logger.debug("Great Glacier: marked the snowboard wake-up as seen "
                         "(entering on foot from the world map)")
    except Exception as exc:
        logger.debug(f"glacier wake-up seed failed: {exc}")


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


def _suppress_diamond_scene(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Keep savemap 0xEF6 bit 3 ("Diamond Weapon is marching on Midgar") clear in
    Free Roam. Visiting Bone Village + the Forgotten City sets it, and the ENGINE
    (not the world script) then arms the disc-2 Diamond scene on the next Highwind
    touch: camera rise cinematic + forced ENTER_FIELD — resuming System fn 30's
    body PAST its entry, so no wm0.ev head patch can stop it. Clearing the arming
    flag kills the whole sequence at the source (verified live 2026-07-07).
    Bit-surgical: 0xEF6 bit 0 is the Underwater Huge Materia location flag —
    never touch the rest of the byte."""
    if not ctx.free_roam:
        return
    try:
        addr = SAVEMAP_BASE + 0x0EF6
        val = pm.read_uchar(addr)
        if val & 0x08:
            pm.write_uchar(addr, val & ~0x08)
            logger.debug("Cleared Diamond-scene arming flag (savemap 0xEF6 bit 3)")
    except Exception as exc:
        logger.debug(f"diamond scene suppress failed: {exc}")


def _seed_ultimate_hp(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Initialise Ultimate Weapon's remaining-HP field so his chase can start.

    A fresh Free Roam save leaves savemap 0x0BFF at ZERO. The engine reads that as
    "HP < 20,000", sets weapons_killed.bit[2] the moment the player engages, and
    `ultima_weapon_update` then routes every approach straight to battle 287 —
    the multi-stage chase never runs at all.

    Written ONLY while the field reads EXACTLY 0 and he is not yet dead.

    DO NOT ADD AN UPPER BOUND. On 2026-07-27 this guard was widened to
    `== 0 or > ULTIMATE_HP_FULL`, because live readings of 9,527,041 and
    12,390,656 "obviously" could not be HP. They were real: every vanilla chase
    save reads this field in the millions (13,513,217 at Junon, 1,507,841 at
    Mideel, 6,809,344 at Nibelheim...). The upper bound made the client clamp the
    engine's own writes on every 0.2 s poll, and the player watched Ultimate's HP
    freeze at exactly 100,000 mid-chase. ULTIMATE_HP_FULL is a SEED for the zero
    case, NOT a maximum.

    (The field's exact semantics are still unresolved — no offset in the save slot
    decreases monotonically across the vanilla chase saves, so the "u24 remaining
    HP" label may be imprecise. What IS established empirically is that seeding
    100,000 into a zeroed field makes the chase run.)"""
    try:
        wk = pm.read_uchar(SAVEMAP_BASE + WEAPONS_KILLED_OFFSET)
        if wk & 0x01:                                  # already defeated
            return
        hp_addr = SAVEMAP_BASE + ULTIMATE_HP_OFFSET
        if int.from_bytes(pm.read_bytes(hp_addr, 3), "little") == 0:
            pm.write_bytes(hp_addr, ULTIMATE_HP_FULL.to_bytes(3, "little"), 3)
            logger.debug(f"Ultimate Weapon HP seeded: 0 -> {ULTIMATE_HP_FULL} "
                         f"(0x0BFF, 3 bytes) — chase can now start.")
    except Exception as exc:
        logger.debug(f"ultimate HP seed failed: {exc}")


def _resolve_weapon_battles(ctx, pm: "pymem.Pymem") -> None:
    """Register Ruby/Emerald Weapon kills in Free Roam by watching battles.

    Their defeat flags (weapons_killed bit3=Ruby, bit4=Emerald) are set by
    post-battle world-script logic that the Free Roam endgame state skips, so a WON
    fight leaves the flag clear: the AP check never fires and the weapon keeps
    respawning. We watch the live game module + battle formation id; while the
    player is in a weapon battle we latch the kill, and once they return to
    gameplay (World/Field, i.e. they won — not a Game Over) we set the bit. Acts
    ONLY on the exact weapon formation ids, so a wrong/garbage formation read can
    never false-trigger a kill.

    ULTIMATE (2026-07-27): he dies at **battle 281**, the Junon-crater encounter,
    and is then RETIRED via  — the crater-crash cinematic is
    deliberately suppressed rather than fixed. See the table comment for why."""
    try:
        module = pm.read_uchar(GAME_MODULE_ADDR)
        if module == GAME_MODULE_BATTLE:
            formation = pm.read_ushort(BATTLE_FORMATION_ADDR)
            mask = _WEAPON_BATTLE_FORMATIONS.get(formation)
            if mask:
                ctx._weapon_kill_pending |= mask
                ctx._weapon_kill_ticks = _WEAPON_KILL_WINDOW_TICKS
            return
        if module == GAME_MODULE_GAMEOVER:
            ctx._weapon_kill_pending = 0          # player lost — not a kill
            ctx._weapon_kill_ticks = 0
            return
        # A LOSS MUST NEVER REGISTER AS A KILL. Clearing on GAME_MODULE_GAMEOVER
        # alone was not enough: if that module was missed, or the loss took some
        # other path, the mask simply sat there until the player reloaded and the
        # first Field/World poll cashed it in as a win. So also discard it when the
        # module leaves the known battle-exit path, and expire it on a timer.
        if ctx._weapon_kill_pending:
            if module not in _BATTLE_EXIT_MODULES:
                logger.debug(f"weapon kill 0x{ctx._weapon_kill_pending:02x} discarded "
                             f"— module {module} is off the battle-exit path")
                ctx._weapon_kill_pending = 0
                ctx._weapon_kill_ticks = 0
                return
            ctx._weapon_kill_ticks -= 1
            if ctx._weapon_kill_ticks <= 0:
                logger.debug(f"weapon kill 0x{ctx._weapon_kill_pending:02x} expired "
                             f"— no return to gameplay within "
                             f"{_WEAPON_KILL_WINDOW_TICKS * POLL_INTERVAL:.0f}s")
                ctx._weapon_kill_pending = 0
                return
        if not ctx._weapon_kill_pending:
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
            ctx._weapon_kill_ticks = 0
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

# Racing stats per breed, as (speed, maxspeed, sprint, maxsprint, stamina,
# accel, coop, intelligence).
#
# The old values were a flat 1000/1000/1000/1000 + 20/20/20 "plausible filler",
# written when chocobos were purely a TRAVERSAL item and nothing raced them. Two
# problems once racing became a check:
#   * they are 3-5x below a real racer. The ff7-flat-wiki Savemap page gives a
#     reference racing chocobo as sprint 4500 / speed 3500 / stamina 6000 /
#     accel 70 / coop 100 / int 100;
#   * worse, **maxspeed/maxsprint are the CAPS a chocobo improves toward while
#     racing** — pinning them equal to the current stats meant an AP chocobo could
#     never get faster no matter how much it raced. That is what made
#     "Chocobo Racing Rank S" (9 wins, climbing C->B->A->S) unreachable.
#
# So: start each breed at a raceable value and leave real headroom above it, and
# scale the whole ladder by breed so Gold is genuinely the best racer (it already
# is the best traversal chocobo). Gold starts at exactly the wiki's reference
# racer with room to grow; Green is the weakest but still competitive in the
# lower classes and improves from there.
#
# Deliberately NOT touched: `raceswon` stays 0. Pre-setting wins would advance the
# class for free, and the Rank S AP check reads the game's own progression bit
# (0x10 at savemap 0xE2E) — it would fire without a single race, turning a fix
# into a bug.
_CHOCO_RACE_STATS = {
    #                 speed maxspd sprint maxspr stam accel coop int
    "Green Chocobo": (2600,  4200,  3400,  5000, 4500,  55,  80,  80),
    "Blue Chocobo":  (2800,  4600,  3600,  5400, 5000,  60,  85,  85),
    "Black Chocobo": (3200,  5200,  4100,  6000, 5600,  68,  95,  95),
    "Gold Chocobo":  (3500,  6000,  4500,  6800, 6000,  70, 100, 100),
}


# Chocobo racing rank checks, driven by the per-chocobo WIN COUNTER rather than
# the racing-class byte.
#
# A chocobo's class advances every 3 wins, so 3 = Class B, 6 = Class A, 9 = Class S
# — and 9 is independently corroborated by the savemap doc's flag for
# "win 9 races to enter Rank S" (0x10 at 0xE2E). Wins live at record +0x0D and are
# MONOTONIC, which the class byte at 0x0DBB is not: that one holds only the
# currently SELECTED class, and Class A (value 2) leaves bit 0 clear, so a
# bit-based read of it can miss Rank B entirely. Counting wins avoids that.
#
# Per CHOCOBO, not per save — the doc's "win 19 races with the same chocobo" wording
# confirms the counter is per animal — so take the best chocobo in the stable
# rather than summing across them.
_CHOCO_WINS_OFFSET = 0x0D          # FF7CHOCOBO.raceswon, within the 16-byte record
_CHOCO_RANK_CHECKS = {             # location code -> wins needed
    310101: 3,                     # Chocobo Racing Rank B
    310102: 6,                     # Chocobo Racing Rank A
    310099: 9,                     # Chocobo Racing Rank S (also has a native flag)
}


def _best_chocobo_wins(pm: "pymem.Pymem") -> int:
    """Highest race-win count of any chocobo in the stable, or 0."""
    best = 0
    try:
        base = SAVEMAP_BASE + _CHOCO_SLOT0
        for n in range(_CHOCO_MAX_SLOTS):
            wins = pm.read_uchar(base + n * 16 + _CHOCO_WINS_OFFSET)
            if 0 < wins < 200:                 # ignore junk in an unused slot
                best = max(best, wins)
    except Exception as exc:
        logger.debug(f"chocobo win read failed: {exc}")
    return best


def _chocobo_rank_checks(pm: "pymem.Pymem", ctx: "FF7Context") -> List[int]:
    """Location codes newly earned by chocobo race wins."""
    if not ctx.free_roam:
        return []
    wanted = [c for c, need in _CHOCO_RANK_CHECKS.items()
              if c in ctx.server_locations and c not in ctx.checked_locations
              and c not in ctx._checked_this_session]
    if not wanted:
        return []
    wins = _best_chocobo_wins(pm)
    if wins <= 0:
        return []
    fired = [c for c in wanted if wins >= _CHOCO_RANK_CHECKS[c]]
    for code in fired:
        ctx._checked_this_session.add(code)
        logger.debug(f"Chocobo racing: {wins} win(s) -> location {code}")
    return fired


def _deliver_chocobo(pm: "pymem.Pymem", item_name: str, sender: str = "") -> bool:
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
        # FF7CHOCOBO record (16 B), layout per the ff7-flat-wiki Savemap page.
        # `type` governs terrain; the rest decide whether it can actually race —
        # see _CHOCO_RACE_STATS for why these are no longer flat filler.
        (_spd, _maxspd, _spr, _maxspr,
         _stam, _acc, _coop, _int) = _CHOCO_RACE_STATS.get(
            item_name, _CHOCO_RACE_STATS["Green Chocobo"])
        pm.write_ushort(rec + 0x00, _spr)     # sprintspd
        pm.write_ushort(rec + 0x02, _maxspr)  # maxsprintspd  (CAP - must exceed current)
        pm.write_ushort(rec + 0x04, _spd)     # speed
        pm.write_ushort(rec + 0x06, _maxspd)  # maxspeed      (CAP - must exceed current)
        pm.write_uchar (rec + 0x08, _acc)     # accel
        pm.write_uchar (rec + 0x09, _coop)    # coop
        pm.write_uchar (rec + 0x0A, _int)     # intelligence
        pm.write_uchar (rec + 0x0B, 0)     # personality (range unknown; 0 = safe default)
        pm.write_uchar (rec + 0x0C, 0)     # pcount
        pm.write_uchar (rec + 0x0D, 0)     # raceswon — left 0 on purpose, see above
        pm.write_uchar (rec + 0x0E, 0)     # sex (0 = male)
        pm.write_uchar (rec + 0x0F, type_byte)
        # Per-chocobo extras (parallel arrays, indexed by slot).
        pm.write_ushort(base + _CHOCO_STAMINA0 + free_slot * 2, _stam)
        pm.write_uchar (base + _CHOCO_RATING0 + free_slot, 1)        # Wonderful
        # name it after the ap player who found it (empty sender = blank name)
        for i, b in enumerate(_encode_ff7_name(sender, width=6)):
            pm.write_uchar(base + _CHOCO_NAME0 + free_slot * 6 + i, b)
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
_CHARACTER_IDS = {"Barret": 1, "Tifa": 2, "Aerith": 3, "Red XIII": 4, "Yuffie": 5,
                  "Cait Sith": 6, "Vincent": 7, "Cid": 8}
_PARTY_OFFSET       = 0x04F8   # qint8 party[3] — active party member ids
# PHS bitmasks (per character id). 0x10A4 is the LOCK mask (ff7-ultima
# party_locking_mask): a SET bit forces the member in place / blocks swapping.
# 0x10A6 is the visibility/availability mask. A swappable member needs its
# visibility bit SET and its lock bit CLEAR.
_PHS_LOCK_OFFSET    = 0x10A4   # quint16 — who is LOCKED (un-swappable) in the PHS
_PHS_VISIBLE_OFFSET = 0x10A6   # quint16 — who is visible/available in the PHS
# Main-menu visibility mask (ff7tk savemap: quint16 menu_visible @0x0BC0). A SET
# bit = that menu option is shown; MENUPHS = bit 8. In Free Roam the PHS option
# is hidden until the player has received this many party members via AP, so the
# party-swap menu can't be opened before there's anyone to swap to.
_MENU_VISIBLE_OFFSET = 0x0BC0
_MENU_PHS_BIT        = 8           # MENUPHS (ff7tk FF7Save::MENUITEMS)
_PHS_UNLOCK_CHARACTERS = 3
_CHARACTER_ITEM_NAMES = frozenset(_CHARACTER_IDS)
_CHARS_OFFSET       = 0x0054   # FF7CHAR chars[9]
_CHAR_RECORD_SIZE   = 132      # bytes per character record (FF7CHAR)

# Default in-game names per character id (FF7's initial-data names).
_CHAR_DEFAULT_NAMES = {
    1: "Barret", 2: "Tifa", 3: "Aeris", 4: "Red XIII", 5: "Yuffie",
    6: "Cait Sith", 7: "Vincent", 8: "Cid",
}
# First (default) weapon index per character id — the byte stored in FF7CHAR
# +0x1C. Each character may only equip weapons in their own range, so a delivered
# character must hold one of theirs (from FF7-exe-Editor GameData.cs WeaponData).
# Per-character weapon ranges as (first weapon NUMBER, count). FF7CHAR.weapon
# stores the weapon# (= item id - 0x80) and the ranges are NOT 16 each — this
# mirrors the table in GS StartingEquipmentRandomizer (verified vs ff7tk's weapon
# enum). EVERY recruitable id must appear: a missing one used to fall back to
# weapon 0 = Cloud's Buster Sword, which is how AP-delivered Vincent turned up
# holding a sword.
#
# These matter only for characters the client has to REBUILD from scratch. FF7's
# kernel ships Cait Sith and Vincent with placeholder ids (9 = Young Cloud, 10 =
# Sephiroth), so `_ensure_character_record` sees id != cid, calls them invalid and
# re-seeds them via the clone-Cloud path — discarding the weapon Gold Saucer
# randomized into the kernel. Everyone else has a valid kernel record and simply
# KEEPS the GS-randomized weapon. So we pick one here ourselves, from the seed, to
# keep those two randomized (and reproducible) instead of always identical.
_CHAR_WEAPON_RANGES = {
    0: (0,   16),  # Cloud     — swords
    1: (32,  16),  # Barret    — gun-arms
    2: (16,  16),  # Tifa      — gloves
    3: (62,  11),  # Aerith    — rods/staves
    4: (48,  14),  # Red XIII  — clips/combs
    5: (87,  14),  # Yuffie    — shuriken/rings
    6: (101, 13),  # Cait Sith — megaphones
    7: (114, 13),  # Vincent   — guns
    8: (73,  14),  # Cid       — spears
}


def _ap_seed(ctx: "FF7Context") -> str:
    """Stable per-multiworld seed string for deterministic equipment picks.
    Prefers the server's seed name; falls back to the .apff7 seed so /setjson-only
    setups stay reproducible too."""
    for attr in ("seed_name", "seed"):
        val = getattr(ctx, attr, None)
        if val:
            return str(val)
    return ""


def _seeded_index(seed: str, key: str, count: int) -> int:
    """Stable index in [0, count) derived from the multiworld seed + a key.
    Uses sha256 rather than `random` so the same seed always yields the same
    equipment across machines and Python versions."""
    if count <= 1:
        return 0
    digest = hashlib.sha256(f"{seed}|{key}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % count


# ---------------------------------------------------------------------------
# kernel.bin "Battle and growth data" (section index 2)
# ---------------------------------------------------------------------------
# Cloning Cloud's record gave every AP-delivered character CLOUD's stats. Instead
# we compute each character's own baseline from the growth curves in the PLAYER'S
# OWN kernel.bin — no game data is shipped with the apworld, and a Gold Saucer
# modified kernel is picked up automatically.
#
# Layout (verified against the shipped 2013 kernel, see the validation notes
# below): 9 character records of 0x38 bytes at 0x0000 in char-id order. Record
# bytes 0x00-0x08 are curve INDICES for Str, Vit, Mag, Spr, Dex, Lck, HP, MP, Exp.
# One contiguous 64-entry curve table starts at 0x021C, 16 bytes per entry = 8
# (gradient, base) pairs, one per level band. `base` is a SIGNED byte (the HP
# curves rely on this — e.g. 255 = -1, 240 = -16).
_GROW_RECORD_SIZE  = 0x38
_GROW_CURVE_BASE   = 0x021C
_GROW_CURVE_STRIDE = 16
_GROW_BAND_MAX     = (11, 21, 31, 41, 51, 61, 81, 99)   # upper level of each band
_KERNEL_SECTION_GROWTH = 2

# Validated against the stock kernel's own shipped starting records:
#   primary = base + grad*level//100  — exact for Tifa/Aeris/Red XIII/Cid (every
#       untouched level-1 record) and for Cloud's Str/Vit/Mag/Spr at level 6.
#       Barret (hand-tweaked Str/Lck) and Cloud's Dex/Lck differ by 1-4: those
#       gain probabilistically on level-up, so a shipped record is not a pure
#       curve evaluation. The curve value is the intended baseline.
#   hp = base*40 + level*grad  — exact for ALL six valid shipped records.
#   mp = base*2 + grad*level//10 — exact for all five level-1 records
#       (Cloud, the only pre-levelled one, comes out 50 vs a shipped 54).
# Yuffie's shipped record is all-zero stats with a placeholder 100 HP / 5 MP; in
# vanilla her join event overwrites it, which never happens in Free Roam. That is
# the bug `_growth_stats` repairs.


def _find_kernel_bin(pm: "pymem.Pymem", ctx: Optional["FF7Context"] = None) -> Optional[Path]:
    """Locate the running game's kernel.bin. Tries the attached process's own
    directory first, then the configured FF7 dir, covering both the classic
    layout (<game>/data/lang-*/kernel) and the 2026 re-release's nested
    <game>/ff7/workingdir/data/lang-*/kernel."""
    roots: List[Path] = []
    try:
        roots.append(Path(pm.process_base.filename).parent)
    except Exception:
        pass
    if ctx is not None and getattr(ctx, "ff7_dir", None):
        roots.append(Path(ctx.ff7_dir))
    for root in roots:
        for prefix in ("", "ff7/workingdir"):
            base = root / prefix if prefix else root
            try:
                hits = sorted(base.glob("data/lang-*/kernel/kernel.bin"))
            except Exception:
                continue
            if hits:
                return hits[0]
    return None


def _read_kernel_section(path: Path, index: int) -> Optional[bytes]:
    """Return one decompressed kernel.bin section. Each section is preceded by a
    6-byte header ``[u16 compressed][u16 uncompressed][u16 filetype]`` and stored
    as a gzip stream."""
    import gzip
    try:
        blob = path.read_bytes()
    except Exception as exc:
        logger.debug(f"kernel.bin read failed ({path}): {exc}")
        return None
    off = 0
    for i in range(27):
        if off + 6 > len(blob):
            return None
        clen = int.from_bytes(blob[off:off + 2], "little")
        if i == index:
            try:
                return gzip.decompress(blob[off + 6:off + 6 + clen])
            except Exception as exc:
                logger.debug(f"kernel.bin section {index} inflate failed: {exc}")
                return None
        off += 6 + clen
    return None


def _growth_curve(grow: bytes, curve_index: int, level: int) -> Tuple[int, int]:
    """(gradient, base) for a curve at a level. `base` is returned SIGNED."""
    band = next((i for i, hi in enumerate(_GROW_BAND_MAX) if level <= hi), 7)
    off = _GROW_CURVE_BASE + curve_index * _GROW_CURVE_STRIDE + band * 2
    grad = grow[off]
    base = grow[off + 1]
    return grad, (base - 256 if base > 127 else base)


def _growth_stats(grow: bytes, cid: int, level: int) -> Optional[Dict[str, int]]:
    """This character's own baseline stats at `level`, straight off their growth
    curves. Returns None if the record looks unusable."""
    level = max(1, min(99, int(level)))
    rec = cid * _GROW_RECORD_SIZE
    if rec + 9 > len(grow) or _GROW_CURVE_BASE + 64 * _GROW_CURVE_STRIDE > len(grow):
        return None
    curves = list(grow[rec:rec + 9])          # str vit mag spr dex lck hp mp exp
    if any(c >= 64 for c in curves[:8]):
        return None
    out: Dict[str, int] = {}
    for name, idx in zip(("str", "vit", "mag", "spr", "dex", "lck"), curves):
        grad, base = _growth_curve(grow, idx, level)
        out[name] = max(1, min(255, base + grad * level // 100))
    grad, base = _growth_curve(grow, curves[6], level)
    out["hp"] = max(1, min(9999, base * 40 + level * grad))
    grad, base = _growth_curve(grow, curves[7], level)
    out["mp"] = max(1, min(999, base * 2 + grad * level // 10))
    return out


# Equipment/materia sections of the same kernel.bin. Record sizes and the
# materia-slot array offsets were pinned down against the shipped kernel by
# requiring every record's 8 slot bytes to be a non-zero prefix followed by zeros
# — that constraint leaves exactly ONE candidate offset for each. Validated
# against vanilla: Buster Sword 2 slots, Nail Bat 0, Ultima Weapon 8 (all four
# linked pairs), Bronze Bangle 0, Iron Bangle 1. A slot byte is non-zero when that
# physical slot exists; the value encodes linked/growth which we do not care about.
_KERNEL_SECTION_WEAPON  = 5     # 128 weapons x 44 bytes
_KERNEL_SECTION_ARMOR   = 6     #  32 armors  x 36 bytes
_KERNEL_SECTION_MATERIA = 8     #  96 materia x 20 bytes
_WEAPON_RECORD, _WEAPON_SLOTS_OFF = 44, 0x1C
_ARMOR_RECORD,  _ARMOR_SLOTS_OFF  = 36, 0x09
_MATERIA_RECORD = 20

# Soft power cap, mirroring Gold Saucer's StartingEquipmentRandomizer so a
# client-rebuilt character is not better equipped than a GS-randomized one.
_MAX_WEAPON_MATERIA = 3
_MAX_ARMOR_MATERIA  = 2

_kernel_cache: Dict[int, Optional[bytes]] = {}


def _kernel_section(pm: "pymem.Pymem", ctx: Optional["FF7Context"], index: int) -> Optional[bytes]:
    """Cached kernel.bin section from the PLAYER'S install (never shipped)."""
    if index not in _kernel_cache:
        path = _find_kernel_bin(pm, ctx)
        data = _read_kernel_section(path, index) if path else None
        if data:
            logger.debug(f"Loaded kernel section {index} ({len(data)} bytes) from {path}")
        else:
            logger.warning(f"Could not read kernel.bin section {index}")
        _kernel_cache[index] = data
    return _kernel_cache[index]


def _growth_data(pm: "pymem.Pymem", ctx: Optional["FF7Context"] = None) -> Optional[bytes]:
    """Cached battle-and-growth section from the player's kernel.bin."""
    data = _kernel_section(pm, ctx, _KERNEL_SECTION_GROWTH)
    if data is None:
        logger.debug("No growth data — AP-delivered characters keep Cloud's stats")
    return data


def _equipment_slot_counts(pm: "pymem.Pymem", ctx: Optional["FF7Context"],
                           weapon_num: int, armor_num: int) -> Tuple[int, int]:
    """(weapon slots, armor slots) physically present on that equipment. Returns
    (0, 0) when the kernel can't be read, so nothing gets placed blindly."""
    wsec = _kernel_section(pm, ctx, _KERNEL_SECTION_WEAPON)
    asec = _kernel_section(pm, ctx, _KERNEL_SECTION_ARMOR)
    w = a = 0
    try:
        if wsec and 0 <= weapon_num < len(wsec) // _WEAPON_RECORD:
            o = weapon_num * _WEAPON_RECORD + _WEAPON_SLOTS_OFF
            w = sum(1 for b in wsec[o:o + 8] if b)
        if asec and 0 <= armor_num < len(asec) // _ARMOR_RECORD:
            o = armor_num * _ARMOR_RECORD + _ARMOR_SLOTS_OFF
            a = sum(1 for b in asec[o:o + 8] if b)
    except Exception as exc:
        logger.debug(f"slot-count read failed: {exc}")
    return w, a


def _materia_pool(pm: "pymem.Pymem", ctx: Optional["FF7Context"] = None) -> List[int]:
    """Materia ids safe to hand out. An unused ("gap") id has an all-0xFF record
    and renders as a nameless orb — those same gap ids are exactly what Gold
    Saucer reserves for AP shop tokens, so dropping them keeps both problems out
    in one filter."""
    sec = _kernel_section(pm, ctx, _KERNEL_SECTION_MATERIA)
    if not sec:
        return []
    pool: List[int] = []
    for i in range(len(sec) // _MATERIA_RECORD):
        rec = sec[i * _MATERIA_RECORD:(i + 1) * _MATERIA_RECORD]
        if any(b != 0xFF for b in rec):
            pool.append(i)
    return pool


# FF7CHAR field offsets used during record initialisation.
_CHR_ID = 0x00; _CHR_LEVEL = 0x01; _CHR_NAME = 0x10
_CHR_STATS = 0x02              # Str, Vit, Mag, Spr, Dex, Lck (1 byte each)
_CHR_STAT_KEYS = ("str", "vit", "mag", "spr", "dex", "lck")
_CHR_LIMITLEVEL = 0x0E; _CHR_LIMITBAR = 0x0F   # current limit level (1-4) / gauge
_CHR_LIMITS = 0x22; _CHR_KILLS = 0x24          # learned-limit bitmask / kills+uses
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


def _init_character_record(pm: "pymem.Pymem", cid: int, seed: str = "",
                           ctx: Optional["FF7Context"] = None) -> None:
    """Seed an uninitialised character record so a delivered party member is
    playable. Optional characters (Cait Sith, Cid …) never get their join-event
    record in Free Roam, so their savemap slot reads all-zero — which the engine
    treats as id 0 ("Cloud") with 0 max HP (instant death). We clone Cloud's
    record for the level/EXP scaffolding and retarget it: own id, name, own
    weapon and a basic armor, no accessory, empty materia — then overwrite the
    stat block with THIS character's own growth-curve baseline so they are no
    longer a Cloud clone."""
    chars = SAVEMAP_BASE + _CHARS_OFFSET
    rec = bytearray(pm.read_bytes(chars, _CHAR_RECORD_SIZE))  # Cloud (slot 0)
    rec[_CHR_ID] = cid
    rec[_CHR_NAME:_CHR_NAME + 12] = _encode_ff7_name(
        _CHAR_DEFAULT_NAMES.get(cid, "AP Char"))
    _range = _CHAR_WEAPON_RANGES.get(cid)
    if _range is None:                    # don't silently hand out Cloud's sword
        logger.warning(f"No weapon range mapped for char id {cid} — "
                       "falling back to Cloud's sword range")
        _range = _CHAR_WEAPON_RANGES[0]
    _start, _count = _range
    rec[_CHR_WEAPON] = _start + _seeded_index(seed, f"weapon:{cid}", _count)
    # FF7 has NO "empty armor" state (a character always has armor equipped), so an
    # 0xFF armor id renders as a garbage name in the menus. Equip a RANDOM valid
    # armor (number 0x00-0x1F = the 32 armors, composite 0x100-0x11F). Accessory DOES
    # support empty (0xFF), so leave it none. (Materia stays empty below, so the
    # armor's slot count doesn't matter; HP/MP collapse to base ignores any bonus.)
    rec[_CHR_ARMOR] = _seeded_index(seed, f"armor:{cid}", 0x20)  # valid armor (NOT 0xFF)
    # Accessory, same story as the weapon/armor/materia above: Gold Saucer rolls
    # one into the kernel for everyone else, and this rebuild path discarded it for
    # Cait Sith and Vincent, who then turned up with an empty accessory slot.
    # Accessory number 0x00-0x1F (composite 0x120-0x13F); 0xFF means none.
    rec[_CHR_ACCESSORY] = _seeded_index(seed, f"accessory:{cid}", 0x20)
    rec[_CHR_STATUS] = 0x00         # normal (clear sadness/fury)
    rec[_CHR_ROW] = 0xFF            # front row (0xFF front / 0xFE back — 0x01 is invalid)
    # Limit state: a cloned/uninitialised limit LEVEL can point at a limit the
    # character hasn't learned, which softlocks battle when the gauge fills (the
    # player's workaround is to set it to Level 1 in the menu). Seed a clean
    # fresh-character state: Level 1 selected, empty gauge, only Level 1 Limit 1
    # learned (bit 0), and zeroed kill / limit-use counters.
    rec[_CHR_LIMITLEVEL] = 0x01
    rec[_CHR_LIMITBAR]   = 0x00
    rec[_CHR_LIMITS:_CHR_LIMITS + 2] = (0x0001).to_bytes(2, "little")
    rec[_CHR_KILLS:_CHR_KILLS + 8]   = b"\x00" * 8   # kills + timesused1/2/3
    rec[_CHR_MATERIA:_CHR_MATERIA + 16 * 4] = b"\xFF" * (16 * 4)  # empty slots
    # Give them seed-deterministic starting materia. Gold Saucer randomizes this
    # into the kernel for everyone else, but Cait Sith and Vincent ship with
    # placeholder kernel records (ids 9 = Young Cloud, 10 = Sephiroth), so this
    # rebuild path throws GS's work away and they arrived with nothing equipped.
    # Bound by the equipment's REAL slot count, and by physical slot INDEX: record
    # slots 0-7 are the weapon's, 8-15 the armor's, so a 2-slot weapon must only
    # ever fill indices 0-1. (Filling by count alone is the bug GS already hit —
    # materia placed in a slot the item does not have is invisible and unusable.)
    _pool = _materia_pool(pm, ctx)
    if _pool:
        _wslots, _aslots = _equipment_slot_counts(
            pm, ctx, rec[_CHR_WEAPON], rec[_CHR_ARMOR])
        for _phys, _cap, _base in ((_wslots, _MAX_WEAPON_MATERIA, 0),
                                   (_aslots, _MAX_ARMOR_MATERIA, 8)):
            for _i in range(min(_phys, _cap, 8)):
                _mid = _pool[_seeded_index(seed, f"materia:{cid}:{_base}:{_i}",
                                           len(_pool))]
                _o = _CHR_MATERIA + (_base + _i) * 4
                rec[_o:_o + 4] = bytes((_mid, 0, 0, 0))   # id + 0 AP (fresh)
        logger.debug(f"cid {cid}: {min(_wslots, _MAX_WEAPON_MATERIA)} weapon + "
                     f"{min(_aslots, _MAX_ARMOR_MATERIA)} armor materia "
                     f"(slots {_wslots}/{_aslots})")
    # Replace Cloud's inherited stats with this character's own baseline at the
    # cloned level (so they join at party level, but as themselves). If kernel.bin
    # can't be read we leave the clone's values — playable, just not accurate.
    grow = _growth_data(pm, ctx)
    stats = _growth_stats(grow, cid, rec[_CHR_LEVEL]) if grow else None
    if stats:
        for i, key in enumerate(_CHR_STAT_KEYS):
            rec[_CHR_STATS + i] = stats[key]
        rec[_CHR_BASEHP:_CHR_BASEHP + 2] = stats["hp"].to_bytes(2, "little")
        rec[_CHR_BASEMP:_CHR_BASEMP + 2] = stats["mp"].to_bytes(2, "little")
        logger.debug(f"cid {cid} lvl {rec[_CHR_LEVEL]} growth stats: {stats}")
    # With equipment/materia stripped, max == base; keep HP/MP consistent & alive.
    base_hp = int.from_bytes(rec[_CHR_BASEHP:_CHR_BASEHP + 2], "little") or 1
    base_mp = int.from_bytes(rec[_CHR_BASEMP:_CHR_BASEMP + 2], "little")
    for off in (_CHR_CURHP, _CHR_MAXHP):
        rec[off:off + 2] = base_hp.to_bytes(2, "little")
    for off in (_CHR_CURMP, _CHR_MAXMP):
        rec[off:off + 2] = base_mp.to_bytes(2, "little")
    pm.write_bytes(chars + cid * _CHAR_RECORD_SIZE, bytes(rec), _CHAR_RECORD_SIZE)


# A character record must look invalid for this many CONSECUTIVE polls before the
# client rebuilds it. Rebuilding wipes equipped materia, so a one-tick blip (a
# cutscene writing the char block while we happen to read it) must not trigger one.
_CHAR_REBUILD_STABLE_TICKS = 3
_char_invalid_ticks: Dict[int, int] = {}


def _ensure_character_record(pm: "pymem.Pymem", cid: int, seed: str = "",
                             ctx: Optional["FF7Context"] = None) -> bool:
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
            # DEBOUNCE. This check runs every poll for every received character, and
            # a rebuild WIPES equipped materia. A record that reads invalid for a
            # single tick is far more likely to be caught mid-write by a cutscene
            # than to be genuinely uninitialised — reported 2026-07-23: visiting the
            # Shinra Mansion basement (sininb1) after receiving Vincent via AP
            # deleted the materia slotted to him, even though Gold Saucer's removal
            # of the join opcodes is verified present in the randomized flevel.
            # A genuinely unseeded record stays invalid, so it still gets built a
            # few ticks later; a transient blip no longer costs the player anything.
            ticks = _char_invalid_ticks.get(cid, 0) + 1
            _char_invalid_ticks[cid] = ticks
            if ticks < _CHAR_REBUILD_STABLE_TICKS:
                logger.debug(f"cid {cid} record looks invalid (tick {ticks}/"
                             f"{_CHAR_REBUILD_STABLE_TICKS}) — waiting before rebuild")
                return False
            # Preserve materia when this is genuinely HIS record (id matches) and
            # only the other fields are broken: rebuilding would otherwise throw
            # away everything the player had socketed.
            keep = None
            if id_byte == cid:
                try:
                    blk = pm.read_bytes(rec_base + _CHR_MATERIA, 16 * 4)
                    if any(b != 0xFF for b in blk):
                        keep = blk
                except Exception:
                    keep = None
            _init_character_record(pm, cid, seed, ctx)
            if keep is not None:
                try:
                    pm.write_bytes(rec_base + _CHR_MATERIA, keep, 16 * 4)
                    logger.debug(f"cid {cid}: preserved equipped materia across rebuild")
                except Exception:
                    pass
            _char_invalid_ticks.pop(cid, None)
            return True
        _char_invalid_ticks.pop(cid, None)
        # Yuffie's shipped kernel record has an ALL-ZERO stat block (vanilla only
        # fills it in during her join event, which Free Roam never runs) — she
        # passes the validity test above and would otherwise be played with 0 in
        # every stat. Repair the stats in place rather than rebuilding, so a
        # progressed record keeps its equipment, materia and level.
        stats_now = pm.read_bytes(rec_base + _CHR_STATS, 6)
        if not any(stats_now):
            grow = _growth_data(pm, ctx)
            stats = _growth_stats(grow, cid, level) if grow else None
            if stats:
                pm.write_bytes(rec_base + _CHR_STATS,
                               bytes(stats[k] for k in _CHR_STAT_KEYS), 6)
                if pm.read_ushort(rec_base + _CHR_BASEHP) == 0:
                    pm.write_ushort(rec_base + _CHR_BASEHP, stats["hp"])
                if pm.read_ushort(rec_base + _CHR_BASEMP) == 0:
                    pm.write_ushort(rec_base + _CHR_BASEMP, stats["mp"])
                logger.debug(f"Repaired zeroed stat block for cid {cid}: {stats}")
        # The record is otherwise valid — but heal a broken LIMIT state without
        # wiping progress. Battle softlocks when the gauge fills if the selected
        # limit level is out of range (1-4) OR that level's limit isn't learned
        # (a character cloned from a limit-inconsistent record). Clamp to Level 1
        # (always learnable) and ensure Level 1 Limit 1 (bit 0) is learned. The
        # learned-limit bit for each level's first limit: L1=0, L2=3, L3=6, L4=9.
        limit_level = pm.read_uchar(rec_base + _CHR_LIMITLEVEL)
        limits = pm.read_ushort(rec_base + _CHR_LIMITS)
        _lvl1_bit = {1: 0, 2: 3, 3: 6, 4: 9}.get(limit_level)
        if _lvl1_bit is None or not (limits & (1 << _lvl1_bit)):
            pm.write_uchar(rec_base + _CHR_LIMITLEVEL, 0x01)
            if not (limits & 0x0001):
                pm.write_ushort(rec_base + _CHR_LIMITS, limits | 0x0001)
            logger.debug(f"Healed invalid limit state for cid {cid} -> Level 1")
    except Exception as exc:
        logger.debug(f"character record check failed for cid {cid}: {exc}")
    return False


# Game functions that rebuild the engine's party-member data after the party
# composition changes (same 2013 exe map as everything else; addresses from
# ff7-ultima's setPartyMemberSlot, which performs the identical slot write we do
# and then calls exactly these FOUR, in order). The member's LIMIT TECHNIQUE
# LIST is resolved by the LAST TWO — verified live 2026-07-15: with only the
# first two the limit gauge fills but pressing Limit shows no techniques (the
# player's workaround was an in-game menu limit-set, which runs the same full
# rebuild). A raw party-slot byte write without all four leaves the list empty.
#   0x6cd13a()      global party refresh
#   0x6c545b(slot)  rebuild one member's core data
#   0x5cb2cc(slot)  rebuild the member's materia-derived commands
#   0x5cb127()      finalise
#   0x6cbd1e()      rebuild every slot's battle LIMIT BLOCK   (the real fix)
# The last one is NOT in ff7-ultima's sequence and is the one that matters for
# limits: it is the thunk game-init itself calls (-> 0x703517), which loops the
# 3 party slots and builds PO[slot]+0xAC (availability) / +0xB4 (3x28-byte limit
# attack records copied from the kernel table at 0x91F6D4, populated at game
# init) from the char's savemap limit level. Game init only runs it for the
# launch party (Cloud in Free Roam), so client-inserted members stay limit-less
# in battle until it is re-run — verified live 2026-07-15 on a fresh launch
# (no menu opened): one call rebuilt Red XIII's block and Sled Fang worked.
_PARTY_SET_FNS = ((0x6CD13A, False), (0x6C545B, True), (0x5CB2CC, True),
                  (0x5CB127, False), (0x6CBD1E, False))
# Back-compat aliases (still referenced elsewhere).
_PARTY_REFRESH_FN = 0x6CD13A
_PARTY_BUILD_MEMBER_FN = 0x6C545B


def _set_party_member(pm: "pymem.Pymem", slot: int) -> None:
    """Run the game's full party-member-set sequence for an already-written slot
    (ff7-ultima setPartyMemberSlot). Idempotent — safe to re-run on a built
    member. The last two calls build the limit technique list."""
    for addr, takes_slot in _PARTY_SET_FNS:
        _call_game_fn(pm, addr, slot if takes_slot else None)

# (The old battle-side limit-menu "fix" that called 0x434df3 per slot is gone:
# its +0x48 broken-marker read a zero-run and matched every slot, and 0x434df3
# turned out to be the limit-bar RESET routine, not a builder. The real repair
# is the 0x6cbd1e limit-block rebuild in _PARTY_SET_FNS above, which runs at
# delivery and via _heal_party_limit_lists in field/world.)


# Consecutive stable polls a party composition must hold before we rebuild it —
# lets the field/world engine finish (re)loading party state so the rebuild fns
# act on settled data (the delivery-time call races this and loses for the 1st
# member). ~0.6s at POLL_INTERVAL=0.2.
_PARTY_REBUILD_STABLE_TICKS = 3


def _heal_party_limit_lists(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Re-run the full party-member-set sequence once per stable party
    composition (see FF7Context._party_sig). This cures the FIRST client-
    delivered member, whose limit technique list stays empty if the delivery-time
    rebuild lands while the field is still loading. _set_party_member is
    idempotent, so re-running it on already-built members is harmless. Only runs
    while the party is stable in a gameplay module, so it never races battle/menu
    working copies. Slot 0 (the leader / Cloud) is never client-inserted, so it's
    left alone."""
    base = SAVEMAP_BASE + _PARTY_OFFSET
    try:
        sig = pm.read_bytes(base, 3)
    except Exception:
        return
    if sig != ctx._party_sig:
        ctx._party_sig = sig
        ctx._party_sig_stable = 0
        return
    ctx._party_sig_stable += 1
    if sig == ctx._party_rebuilt_sig or ctx._party_sig_stable < _PARTY_REBUILD_STABLE_TICKS:
        return
    try:
        for slot in range(1, 3):
            if sig[slot] not in (0xFF, 0xFE):
                _set_party_member(pm, slot)
        ctx._party_rebuilt_sig = sig
        logger.debug(f"Rebuilt party limit data for composition {sig.hex()}")
    except Exception as exc:
        logger.debug(f"party limit rebuild failed: {exc}")


def _call_game_fn(pm: "pymem.Pymem", address: int, param: Optional[int] = None) -> None:
    """Run a game function on a remote thread. CreateRemoteThread's lpParameter
    lands as the callee's first stack argument, which covers the zero/one-arg
    cdecl functions we call. Waits briefly so calls execute in order."""
    import ctypes
    thread = pm.start_thread(address, param)
    if thread:
        ctypes.windll.kernel32.WaitForSingleObject(thread, 1000)
        ctypes.windll.kernel32.CloseHandle(thread)


def _deliver_character(pm: "pymem.Pymem", char_name: str, seed: str = "",
                       ctx: Optional["FF7Context"] = None) -> bool:
    """Unlock an optional party member: make them available in the PHS, and drop
    them into an empty active party slot if one is free."""
    cid = _CHARACTER_IDS.get(char_name)
    if cid is None:
        return False
    try:
        bit = 1 << cid
        # Seed the savemap record if it was never initialised / is invalid (reads
        # as id 0 "Cloud" with 0 max HP → instant death). Clone-and-retarget Cloud.
        if _ensure_character_record(pm, cid, seed, ctx):
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
                    # Rebuild the engine's party-member data (incl. the limit
                    # technique list) exactly like the game's own party-change
                    # code does. The self-healing pass in the poll loop re-runs
                    # this once the field/world module is stable, in case this
                    # delivery-time call lands while the field is still loading.
                    try:
                        _set_party_member(pm, i)
                        logger.debug(f"Rebuilt party data for slot {i} ({char_name})")
                    except Exception as exc:
                        logger.debug(f"party rebuild call failed (non-fatal): {exc}")
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


# Rooms whose one-time story sequence latches "done" flags that leave the room
# permanently inert on revisit (actors idle, chests dead, exits refusing to
# transition). Clearing the latches from an ADJACENT field makes the room replay
# fresh on every entry — never clear inside the room itself, or a running scene
# could glitch. Keyed by field name -> ((savemap_offset, clear_mask), ...).
_FIELD_FLAG_RESETS: Dict[str, Tuple[Tuple[int, int], ...]] = {
    # Underwater Reactor dock (semkin_5: Reno scene + Carry Armor + chests +
    # exit line). The sequence sets 0x1029 bits 1+2 and 0x102A bit 6 (found by
    # working-vs-after savemap diff, verified live 2026-07-11); any of them
    # parks every actor in the room. Chest opened-flags are separate, so a
    # replay can't duplicate loot.
    "semkin_4": ((0x1029, 0x06), (0x102A, 0x40)),
    "semkin_6": ((0x1029, 0x06), (0x102A, 0x40)),
}

# Cleared ONCE on field ENTRY rather than every tick, because the field's own
# script legitimately SETS these later in its sequence — a continuous reset would
# fight it and leave actors on screen that are meant to have gone.
#
# Northern Crater party splits. Every character entity in las0_8 / las2_1 opens
# with `IFUB bank-F var149 bit<n> bitOFF -> else VISI 00`, i.e. it is only visible
# while a "scene done" bit in savemap 0x1039 (0xFA4 + 149) is CLEAR:
#   * las0_8 checks bit 1 and sets it at the END of its own sequence (cloud S0
#     @3040), so a replay correctly shows an empty map.
#   * las2_1 checks bit 0 — which las0_8 sets PARTWAY THROUGH its sequence (cloud
#     S0 @2054). So once you have done the first split, every character is
#     invisible in the second one, by design in vanilla but wrong for Free Roam.
# Free Roam can also arrive with the byte already dirty. Clearing on entry restores
# the cast; each script still sets its bit at the proper moment.
_FIELD_FLAG_RESETS_ON_ENTRY: Dict[str, Tuple[Tuple[int, int], ...]] = {
    "las0_8": ((0x1039, 0x02),),
    "las2_1": ((0x1039, 0x01),),
}
# Fields whose reset must happen AT MOST ONCE PER GAME rather than on every entry.
#
# las0_8's own script BITONs 0x1039 bit 1 at the END of the split sequence
# (@0x0BE0) — that latch is what tells the field the scene is finished. Re-clearing
# it on every entry put the field permanently back into split-setup state, so the
# cliff never became climbable again once the player left the screen and returned
# (reported 2026-08-01; confirmed live — standing in las0_8 after a completed split,
# 0x1039 read 0x00). Resetting once still covers the reason the reset exists at all
# (Free Roam can arrive with the byte already dirty) while letting the script latch
# it permanently afterwards.
#
# las2_1 is deliberately NOT in here yet (user's call 2026-08-01): it clears
# las0_8's PARTWAY marker (bit 0) and is currently what makes the cast show there,
# and that field is progressing fine. It has the same shape, so if the same symptom
# ever appears on the second split, add it here first.
_FIELD_FLAG_RESET_ONCE: "frozenset[str]" = frozenset({"las0_8"})


def _apply_field_flag_resets(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Clear latched sequence-done flags while in a field adjacent to a locked
    room (see _FIELD_FLAG_RESETS). Idempotent — only writes when a bit is set."""
    if not ctx.free_roam:
        return
    field = _read_field_name(pm)

    # Entry-only resets: apply the first tick we see a new field name, so the
    # field's own script can still set the bit later in its sequence.
    if getattr(ctx, "_flag_reset_field", None) != field:
        ctx._flag_reset_field = field
        if field in _FIELD_FLAG_RESET_ONCE and field in ctx._flag_reset_once_done:
            return                      # one reset per game — see _FIELD_FLAG_RESET_ONCE
        if field in _FIELD_FLAG_RESET_ONCE:
            ctx._flag_reset_once_done.add(field)
        for off, mask in _FIELD_FLAG_RESETS_ON_ENTRY.get(field, ()):
            try:
                v = pm.read_uchar(SAVEMAP_BASE + off)
                if v & mask:
                    pm.write_uchar(SAVEMAP_BASE + off, v & ~mask)
                    logger.debug(f"Field entry flag reset ({field}): cleared savemap "
                                 f"0x{off:X} mask 0x{mask:02x}")
            except Exception as exc:
                logger.debug(f"field entry flag reset failed: {exc}")

    resets = _FIELD_FLAG_RESETS.get(field)
    if not resets:
        return
    for off, mask in resets:
        try:
            v = pm.read_uchar(SAVEMAP_BASE + off)
            if v & mask:
                pm.write_uchar(SAVEMAP_BASE + off, v & ~mask)
                logger.debug(f"Field flag reset: cleared savemap 0x{off:X} mask 0x{mask:02x}")
        except Exception as exc:
            logger.debug(f"field flag reset failed: {exc}")


def _read_field_name(pm: "pymem.Pymem") -> str:
    """Current field's name (e.g. 'crcin_1'), or '' on failure. Null/backslash-terminated."""
    try:
        raw = pm.read_bytes(FIELD_NAME_ADDR, 16)
    except Exception:
        return ""
    for sep in (b"\x00", b"\\"):
        i = raw.find(sep)
        if i >= 0:
            raw = raw[:i]
    try:
        return raw.decode("ascii", "ignore")
    except Exception:
        return ""


# Story fields entered out of sequence in Free Roam load some NPCs non-solid /
# non-interactable (their field-script SOLID/VISI state doesn't stick), which blocks
# interacting with them. Force those models solid + interactable + visible live,
# exactly like FF7 Ultima's field-model toggle. Keyed by field name -> model indices
# (each = the entity's CHAR-opcode operand).
_FORCE_INTERACTABLE_MODELS: Dict[str, Tuple[int, ...]] = {
    "crcin_1": (8,),   # esto = Ester, the Chocobo Square race manager (CHAR a1 08)
}


# Northern Crater party splits: the cast is invisible for a reason the savemap
# flag reset can't touch. Every character entity's S0 init ends with
#     IFSW game_moment (savemap 0x0BA4) >= 1997 -> TLKON 1 / SOLID 1 / VISI 0
# and Free Roam pins game_moment to exactly 1997, so that branch ALWAYS taken and
# every character hides itself at field load. (The 0x1039 "scene done" latch in
# _FIELD_FLAG_RESETS_ON_ENTRY is a separate, real gate — it just isn't what was
# hiding them.) Clearing a flag can't help either way: these run in the init
# script at load, before the client's first poll tick, so the only fix is to write
# the model state back live — the same approach as Ester in crcin_1.
#
# field -> (savemap addr, mask) of the scene-done latch. While it is CLEAR the cast
# is forced visible; once the field's own script sets it at the end of the
# sequence, we stop and they disappear as intended.
_CRATER_SPLIT_CAST: Dict[str, Tuple[int, int]] = {
    "las0_8": (0x1039, 0x02),
    # las2_1 REMOVED 2026-08-01 (user's call). Forcing the cast visible there also
    # forces them SOLID (+0x5f=0) and interactable, and because the entry flag reset
    # keeps 0x1039.0 clear the forcing never stops — so seven collidable bodies stand
    # in the room for as long as the player is in it. Players reported being unable to
    # leave the Northern Cave from the party split. las0_8 is the split that actually
    # needed this (its own bit is 0x02 and its script latches it at the END, so the
    # forcing self-terminates there).
}
# CHAR-opcode model index -> character id, for those two fields. NOT identity:
# model 3 is Red XIII (char 4) onward, because Aerith (char 3) has no model
# anywhere in the las* chain. Model 0 is Cloud, the player — never touched.
_CRATER_CAST_MODEL_CHAR: Dict[int, int] = {1: 1, 2: 2, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8}
# Characters to force HIDDEN + non-solid in a given crater field, by character id.
# Keyed on the CHARACTER, not the raw model index, so it resolves through
# _CRATER_CAST_MODEL_CHAR above — the mapping already proven in las0_8 — instead of
# hard-coding an index that would silently hide the wrong person if it were wrong.
# _force_crater_cast_visible skips anyone listed here, or the two would fight over
# the same bytes on every poll.
_FIELD_HIDE_CHARS: Dict[str, Tuple[int, ...]] = {
    "las2_1": (_CHARACTER_IDS["Cid"],),
}


# Bizarro Sephiroth (lastmap) multi-party formation. HEN S3/S4/S5 each form one
# group: MENU 0x07 (assign) -> GTPYE reads the group into persistent savemap
# bank-F vars -> a PRTYM chain pulls each assigned member out of the availability
# pool so the next group can't reuse them. Every chain covers ids [1,2,4,5,6,7,8]
# — EVERY character EXCEPT Aerith (3), because vanilla never expected her alive
# here. So a picked Aerith is never removed and reappears in each subsequent
# group (playtester report 2026-07-23).
#
# We want her USABLE across the groups, just not duplicated — i.e. mirror the
# missing `PRTYM 03` live: once she is committed to a group, clear her
# PHS-availability bit so she drops out of the pool for the remaining groups,
# exactly as the script does for everyone else. GTPYE stores each formed group in
# savemap bank F (nibble F -> 0xFA4): group1 at 0xFA4+0x60..0x62 (0x1004-0x1006),
# group2 0x1007-0x1009, group3 0x100A-0x100C. Cloud (0) is the locked group-1
# leader, so his presence in those slots is the "a group has actually been formed"
# signal that guards against acting on stale/uninitialised var data.
#
# CAVEAT: this only helps the NEXT group's menu if that menu re-reads availability
# while open (the script itself maintains availability between the separate MENU
# 0x07 calls, so each reads it at least on open). If FF7 snapshots the list a hair
# before the client's poll lands, an immediate re-pick could still slip through —
# in which case the robust fix is a field-script edit adding `PRTYM 03` to the
# chains. Test in-game before assuming this alone suffices.
_AERITH_CHAR_ID = _CHARACTER_IDS["Aerith"]
_BIZARRO_PARTY_VARS = tuple(range(0x1004, 0x100D))   # 3 groups x 3 slots, bank-F
# The optional-recruit characters the lastmap formation mishandles:
#   Aerith (3) — no case ANYWHERE (removal, initial enable, or redo re-enable).
#   Yuffie (5) / Vincent (7) — in the removal chains, but the redo re-enable
#     (`MMBud 01 <id>` in AD3 @7260/7269) is GATED on their "joined" story flags
#     (var207 bit0 / var80 bit2), which AP recruitment never sets (they are the
#     recruit locations' detection bits). So a 'Hold on a moment' redo drops them.
# The client owns all three during lastmap. The REDO SIGNAL is the script
# re-enabling the characters it handles UNCONDITIONALLY — Barret/Tifa/Red/Cait/Cid
# — so those are the canary; the mishandled trio can't be trusted to signal it.
_BIZARRO_MANAGED = (_AERITH_CHAR_ID, _CHARACTER_IDS["Yuffie"], _CHARACTER_IDS["Vincent"])
_BIZARRO_RELIABLE = (_CHARACTER_IDS["Barret"], _CHARACTER_IDS["Tifa"],
                     _CHARACTER_IDS["Red XIII"], _CHARACTER_IDS["Cait Sith"],
                     _CHARACTER_IDS["Cid"])


def _char_is_recruited(pm: "pymem.Pymem", cid: int) -> bool:
    """True if character `cid` is a real, usable party member (valid savemap
    record) — covers both AP-delivered and directly-added characters, so it does
    not depend on ctx.items_received."""
    try:
        rec = SAVEMAP_BASE + _CHARS_OFFSET + cid * _CHAR_RECORD_SIZE
        return (pm.read_uchar(rec + _CHR_ID) == cid
                and pm.read_ushort(rec + _CHR_MAXHP) > 0)
    except Exception:
        return False


def _dedupe_bizarro_aerith(pm: "pymem.Pymem", ctx: FF7Context) -> None:
    """Own the availability of the optional recruits (Aerith/Yuffie/Vincent) during
    the Bizarro Sephiroth party formation so each can be used in exactly one group,
    never duplicated, and — crucially — re-offered after a 'Hold on a moment' redo.

    The script maintains availability for the characters it knows: PRTYM removes an
    assigned member so later groups can't reuse them, and on a redo `MMBud 01 <id>`
    re-enables them. But it re-enables Yuffie/Vincent only behind their (unset)
    join flags, and never handles Aerith at all — so the client fills those gaps.
    State is recomputed each poll, so it self-heals through the redo.

    Redo detection: a character the script re-enables UNCONDITIONALLY (the reliable
    set) that was assigned to a group but is available again means the pool was just
    reset — so the managed trio belongs back in it."""
    if not ctx.free_roam or _read_field_name(pm) != "lastmap":
        return
    try:
        vis_addr = SAVEMAP_BASE + _PHS_VISIBLE_OFFSET
        vis = pm.read_ushort(vis_addr)
        groups = [pm.read_uchar(SAVEMAP_BASE + a) for a in _BIZARRO_PARTY_VARS]
    except Exception as exc:
        logger.debug(f"Bizarro dedupe read failed: {exc}")
        return
    # Pool reset (redo) iff any UNCONDITIONALLY-re-enabled character that was
    # committed to a group is available again.
    pool_reset = any(c in groups and (vis >> c) & 1 for c in _BIZARRO_RELIABLE)
    new_vis = vis
    for cid in _BIZARRO_MANAGED:
        if not _char_is_recruited(pm, cid):
            continue                       # not in the roster — never offer them
        bit = 1 << cid
        committed = (cid in groups) and not pool_reset
        if committed:
            new_vis &= ~bit                # used this pass — drop from later groups
        else:
            new_vis |= bit                 # fresh pass / redo — keep selectable
    if new_vis != vis:
        try:
            pm.write_ushort(vis_addr, new_vis)
            logger.debug(f"Bizarro Sephiroth: party pool {vis:#06x}->{new_vis:#06x} "
                         f"(reset={pool_reset})")
        except Exception as exc:
            logger.debug(f"Bizarro dedupe write failed: {exc}")


def _force_crater_cast_visible(pm: "pymem.Pymem", ctx: FF7Context) -> None:
    """Keep the party-split cast on screen during the left/right sequence.

    Skips anyone currently in the ACTIVE party (the script hides their standing
    model on purpose — they are already walking behind you, so forcing it would
    show a duplicate), and anyone not yet recruited."""
    if not ctx.free_roam:
        return
    gate = _CRATER_SPLIT_CAST.get(_read_field_name(pm))
    if gate is None:
        return
    addr, mask = gate
    try:
        if pm.read_uchar(SAVEMAP_BASE + addr) & mask:
            return                      # scene finished — let them vanish
        party = {pm.read_uchar(SAVEMAP_BASE + _PARTY_OFFSET + i) for i in range(3)}
        avail = pm.read_ushort(SAVEMAP_BASE + _PHS_VISIBLE_OFFSET)
    except Exception as exc:
        logger.debug(f"crater cast read failed: {exc}")
        return
    hidden = set(_FIELD_HIDE_CHARS.get(_read_field_name(pm), ()))
    for idx, cid in _CRATER_CAST_MODEL_CHAR.items():
        if cid in party or not (avail & (1 << cid)):
            continue
        if cid in hidden:
            continue            # deliberately hidden in this field — see _FIELD_HIDE_CHARS
        base = FIELD_MODELS_OBJS + idx * _FIELD_MODEL_STRUCT_SIZE
        try:
            # VISIBILITY ONLY (+0x62). Collision (+0x5f) and interaction (+0x61)
            # are deliberately NOT written any more.
            #
            # This used to mirror the script's full VISIBLE branch (TLKON 0 /
            # SOLID 0 / VISI 1), which also made all seven standing models SOLID.
            # Players could then not climb out of las0_8 — collidable bodies parked
            # across the exit path (reported 2026-08-01, after the same forcing was
            # narrowed off las2_1 for the same reason). las2_1 shows the cast fine
            # with no forcing at all, so solidity was never what put them on screen;
            # only the visible byte matters, and it cannot block movement.
            if pm.read_uchar(base + 0x62) != 1:
                pm.write_uchar(base + 0x62, 1)
                logger.debug(f"Crater split: forced model {idx} (char {cid}) visible")
        except Exception:
            pass


# Characters whose kernel record is a PLACEHOLDER until the AP item arrives:
# Cait Sith's slot ships id 9 (Young Cloud), Vincent's ships id 10 (Sephiroth).
# Everyone else has a real record, so a native grant of THEM is only a logic
# problem, not a cosmetic one — these two are the pair that show a wrong name.
_PLACEHOLDER_CHARS: Tuple[int, ...] = (_CHARACTER_IDS["Cait Sith"],
                                       _CHARACTER_IDS["Vincent"])


def _suppress_undelivered_chars(pm: "pymem.Pymem", ctx: FF7Context) -> None:
    """Keep natively-granted characters out of the party until AP delivers them.

    Vanilla makes the whole roster available in several places we do not patch —
    the Northern Cave chain (`las0_1`, `las4_0`, `lastmap`, `crater_2`) and the
    `blackbg*` set all run the same "everyone is available" opcode that
    `sininb1`/`yufy1` do. Free Roam walks into those long before the AP character
    item exists, and because only the client builds these two records, the player
    ends up with a party member literally named **Sephiroth** (Vincent's slot still
    holds kernel id 10) or Young Cloud (Cait Sith, id 9). Reported 2026-08-01.

    Rather than neuter every grant site — the endgame genuinely needs the full
    roster for the Bizarro Sephiroth three-party split — this reverses the symptom
    wherever it comes from: while the record is still a placeholder, that character
    is not PHS-visible, not locked, and not in the active party. It self-clears the
    instant the AP item lands and `_init_character_record` writes a real record.

    The party LEADER (slot 0) is deliberately left alone — evicting them would
    leave the field with no player model. That is logged instead, loudly, because
    it needs manual attention and should be impossible via a normal grant."""
    if not ctx.free_roam:
        return
    try:
        vis_addr = SAVEMAP_BASE + _PHS_VISIBLE_OFFSET
        lock_addr = SAVEMAP_BASE + _PHS_LOCK_OFFSET
        base = SAVEMAP_BASE + _PARTY_OFFSET
        received = getattr(ctx, "_received_item_names", set())
        name_of = {cid: nm for nm, cid in _CHARACTER_IDS.items()}
        for cid in _PLACEHOLDER_CHARS:
            # NEVER touch a character AP has actually sent. `_deliver_character`
            # calls `_ensure_character_record`, which DEBOUNCES: a record that reads
            # invalid is left alone for _CHAR_REBUILD_STABLE_TICKS polls, because a
            # single bad tick is usually a mid-cutscene write and a rebuild wipes
            # equipped materia. So right after delivery the record is still the
            # kernel placeholder for a few polls — and without this check the
            # suppression below saw `id != cid`, called it a native grant, and undid
            # the delivery. Players had to send Vincent / Cait Sith TWICE (reported
            # 2026-08-01). The item having been received is proof it is not native.
            if name_of.get(cid) in received:
                continue
            rec = SAVEMAP_BASE + _CHARS_OFFSET + cid * _CHAR_RECORD_SIZE
            if pm.read_uchar(rec + _CHR_ID) == cid:
                continue                    # real record — AP has delivered them
            bit = 1 << cid
            vis = pm.read_ushort(vis_addr)
            if vis & bit:
                pm.write_ushort(vis_addr, vis & ~bit)
                logger.debug(f"Undelivered char {cid}: cleared PHS visibility "
                             f"(record still holds placeholder id "
                             f"{pm.read_uchar(rec + _CHR_ID)})")
            lock = pm.read_ushort(lock_addr)
            if lock & bit:
                pm.write_ushort(lock_addr, lock & ~bit)
            for i in range(3):
                if pm.read_uchar(base + i) != cid:
                    continue
                if i == 0:
                    logger.info(f"Character {cid} was granted by the game before "
                                f"Archipelago delivered them and is the party "
                                f"LEADER — swap leader in the PHS, then they will "
                                f"be removed automatically.")
                    continue
                pm.write_uchar(base + i, 0xFF)      # 0xFF = empty slot
                logger.debug(f"Undelivered char {cid}: removed from party slot {i}")
    except Exception as exc:
        logger.debug(f"undelivered-char suppression failed: {exc}")


def _apply_field_hide_chars(pm: "pymem.Pymem", ctx: FF7Context) -> None:
    """Force listed characters INVISIBLE and non-solid in their field.

    The inverse of _force_crater_cast_visible: collision OFF (+0x5f=1), interaction
    OFF (+0x61=1), visible OFF (+0x62=0). Interaction is disabled along with the
    model deliberately — an invisible-but-talkable body is worse than a visible one.

    Runs AFTER the visibility pass so it wins outright even if some other path
    turns the model back on.  Idempotent: only writes a byte that is wrong."""
    if not ctx.free_roam:
        return
    fname = _read_field_name(pm)
    chars = _FIELD_HIDE_CHARS.get(fname)
    if not chars:
        return
    model_of = {cid: idx for idx, cid in _CRATER_CAST_MODEL_CHAR.items()}
    for cid in chars:
        idx = model_of.get(cid)
        if idx is None:
            continue                     # no model for that character in this chain
        base = FIELD_MODELS_OBJS + idx * _FIELD_MODEL_STRUCT_SIZE
        try:
            changed = False
            if pm.read_uchar(base + 0x5f) != 1:
                pm.write_uchar(base + 0x5f, 1); changed = True   # collision OFF
            if pm.read_uchar(base + 0x61) != 1:
                pm.write_uchar(base + 0x61, 1); changed = True   # interaction OFF
            if pm.read_uchar(base + 0x62) != 0:
                pm.write_uchar(base + 0x62, 0); changed = True   # invisible
            if changed:
                logger.debug(f"Hid field model {idx} (char {cid}) in {fname}")
        except Exception:
            pass


def _apply_field_model_overrides(pm: "pymem.Pymem", ctx: FF7Context) -> None:
    """When in a field listed in _FORCE_INTERACTABLE_MODELS, force its NPC model(s)
    solid (+0x5f=0), interactable (+0x61=0) and visible (+0x62=1). Idempotent — only
    writes a byte that's wrong, so it costs nothing on the vast majority of polls."""
    if not ctx.free_roam:
        return
    fname = _read_field_name(pm)
    models = _FORCE_INTERACTABLE_MODELS.get(fname)
    if not models:
        return
    for idx in models:
        base = FIELD_MODELS_OBJS + idx * _FIELD_MODEL_STRUCT_SIZE
        try:
            changed = False
            if pm.read_uchar(base + 0x5f) != 0:
                pm.write_uchar(base + 0x5f, 0); changed = True   # collision ON (solid)
            if pm.read_uchar(base + 0x61) != 0:
                pm.write_uchar(base + 0x61, 0); changed = True   # interaction ON (talkable)
            if pm.read_uchar(base + 0x62) != 1:
                pm.write_uchar(base + 0x62, 1); changed = True   # visible
            if changed:
                logger.debug(f"Forced field model {idx} solid/interactable/visible in {fname}")
        except Exception:
            pass


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


def _enable_materia_menu(pm: "pymem.Pymem") -> None:
    """Unlock the Materia main-menu option (savemap 0x0BC0 bit 3).

    Set on connect and re-asserted on game-over recovery — a Free Roam game
    over reloads the md1stin baseline, which does not enable this menu."""
    try:
        addr = SAVEMAP_BASE + 0x0BC0
        cur = pm.read_uchar(addr)
        if not (cur & 0x08):
            pm.write_uchar(addr, cur | 0x08)
            logger.debug("Materia menu enabled")
    except Exception as e:
        logger.debug(f"Could not enable materia menu: {e}")


def _requeue_all_received_items(ctx: "FF7Context") -> int:
    """Re-arm delivery of every AP item already received for this slot.

    Clears the delivered-index set and re-queues ``ctx.items_received`` (the
    authoritative full list CommonContext maintains across the connection), so
    the next delivery pass re-applies everything. Used by the Free Roam
    game-over recovery and the ``/resync`` command: a game over reloads the
    md1stin baseline and wipes every client-delivered item, so re-delivery
    restores them. Flag-type deliveries (key items, vehicles, party members,
    chocobos) are idempotent; stackable items / materia are RECONCILED to their
    AP-granted target (``_resync_reconcile``), so only the missing quantity is
    added — re-syncing with items still present can no longer duplicate them.
    """
    received = list(getattr(ctx, "items_received", None) or [])
    ctx._delivered_item_indices.clear()
    ctx._pending_items = list(enumerate(received))
    # Only arm reconcile mode when there is actually something to reconcile —
    # arming it for an empty list is what got it stuck on (see the early return in
    # _deliver_items_to_game).
    ctx._resync_reconcile = bool(received)
    return len(received)


# Free Roam re-seed detection. Starting a fresh Free Roam run re-seeds the savemap
# from the md1stin baseline, wiping everything the client has written. The
# game-over path already recovers from that (module 26 -> re-deliver), but a plain
# "New Game" from the menu is not a game over: `_delivered_item_indices` still
# lists every item as delivered, so nothing is re-applied and the player silently
# loses key items, recruited characters and chocobos.
#
# md1stin IS the Free Roam opening field, so entering it is the re-seed signal.
# The re-delivery is deferred until the player has LEFT it, because the field's own
# injected script is still seeding flags while it runs — writing over that would
# race it. Same primitive as /resync, so stackables reconcile and flags are
# idempotent; re-running it costs nothing if the items were already intact.
_FREEROAM_SEED_FIELD = "md1stin"


def _pump_reseed_resync(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """Re-deliver every AP item after a fresh Free Roam start wipes the savemap."""
    if not ctx.free_roam:
        return
    try:
        module = pm.read_uchar(GAME_MODULE_ADDR)
    except Exception:
        return
    if module not in (GAME_MODULE_FIELD, GAME_MODULE_WORLD):
        return
    field = _read_field_name(pm) if module == GAME_MODULE_FIELD else ""
    if field == _FREEROAM_SEED_FIELD:
        if not getattr(ctx, "_reseed_pending", False):
            ctx._reseed_pending = True
            logger.debug("Free Roam re-seed detected (md1stin) — will re-deliver "
                         "all AP items once the opening field is left")
        return
    if not getattr(ctx, "_reseed_pending", False):
        return
    # Left md1stin: the baseline is in place, so re-apply everything.
    ctx._reseed_pending = False
    # New savemap -> the once-per-game entry flag resets are owed again.
    ctx._flag_reset_once_done.clear()
    n = _requeue_all_received_items(ctx)
    if n:
        logger.info(f"New game detected — re-applying {n} received AP item(s).")


def _valid_char_records(pm: "pymem.Pymem"):
    """Yield the savemap record base of every INITIALISED character (id byte
    matches the slot). Skips optional-character templates (Cait Sith/Vincent
    slots hold Young Cloud/Sephiroth ids 9/10 until delivered) so their
    placeholder equipment is never counted as player-owned."""
    for cid in range(9):
        rec = SAVEMAP_BASE + _CHARS_OFFSET + cid * _CHAR_RECORD_SIZE
        try:
            if pm.read_uchar(rec + _CHR_ID) == cid:
                yield rec
        except Exception:
            return


def _count_item_qty(pm: "pymem.Pymem", ff7_id: int) -> int:
    """Total quantity of ff7_id currently owned: item inventory PLUS gear
    equipped on characters (weapon/armor/accessory) — otherwise a resync
    counts equipped AP gear as missing and duplicates it."""
    base = SAVEMAP_BASE + ITEM_LIST_OFFSET
    total = 0
    for slot in range(ITEM_SLOT_COUNT):
        word = pm.read_ushort(base + slot * 2)
        if word != EMPTY_ITEM_WORD and (word & 0x1FF) == ff7_id:
            total += (word >> 9) & 0x7F
    # equipped gear (composite ids: weapons 128+n, armor 256+n, accessory 288+n)
    for rec in _valid_char_records(pm):
        w = pm.read_uchar(rec + _CHR_WEAPON)
        a = pm.read_uchar(rec + _CHR_ARMOR)
        acc = pm.read_uchar(rec + _CHR_ACCESSORY)
        if w != 0xFF and 128 + w == ff7_id:
            total += 1
        if a != 0xFF and 256 + a == ff7_id:
            total += 1
        if acc != 0xFF and 288 + acc == ff7_id:
            total += 1
    return total


def _count_materia_qty(pm: "pymem.Pymem", ff7_id: int) -> int:
    """Number of materia of ff7_id currently owned: materia inventory PLUS
    materia socketed in every character's weapon/armor slots (16 × 4 bytes at
    rec+0x40) — otherwise a resync counts equipped AP materia as missing and
    duplicates it."""
    base = SAVEMAP_BASE + MATERIA_LIST_OFFSET
    count = 0
    for slot in range(MATERIA_SLOT_COUNT):
        if pm.read_uchar(base + slot * 4) == ff7_id:
            count += 1
    for rec in _valid_char_records(pm):
        equipped = pm.read_bytes(rec + _CHR_MATERIA, 16 * 4)
        for s in range(16):
            if equipped[s * 4] == ff7_id:
                count += 1
    return count


def _deliver_items_to_game(pm: "pymem.Pymem", ctx: FF7Context) -> None:
    """Drain ctx._pending_items and write each one to FF7 memory."""
    from worlds.ff7.TrapLink import TRAP_REGISTRY

    if not ctx._pending_items:
        # Nothing queued means the re-delivery has drained, so reconcile mode is
        # over BY DEFINITION. Returning without clearing it left the flag stuck on
        # forever whenever a re-queue produced an empty list — which is what the
        # md1stin re-seed does for a player who has not received anything yet. The
        # next real item then took the reconcile path and was silently dropped
        # (reported 2026-08-01: Alexander logged as delivered, absent in-game).
        ctx._resync_reconcile = False
        return

    still_pending: List[Tuple[int, object]] = []
    code_map = _get_code_to_item_name()

    # Resync/game-over re-delivery: precompute the AP-granted target quantity of
    # each stackable item / materia (from the full items_received), so below we
    # add only the deficit vs current inventory instead of stacking on top of
    # whatever survived. Flag-type items (key/vehicle/char/chocobo) are idempotent
    # and skipped here. Normal incremental delivery (flag off) stays additive.
    item_targets: Dict[int, int] = {}
    materia_targets: Dict[int, int] = {}
    if ctx._resync_reconcile:
        for _net in (getattr(ctx, "items_received", None) or []):
            _code = getattr(_net, "item", None)
            _nm = code_map.get(_code) if isinstance(_code, int) else (_code if isinstance(_code, str) else None)
            if not _nm:
                continue
            if (_nm in CHOCOBO_ITEM_NAMES or _nm in VEHICLE_ITEM_FLAGS
                    or _nm in _CHARACTER_IDS or _nm in KEY_ITEM_FLAGS):
                continue
            _r = _item_name_to_ff7_id(_nm)
            if _r is None:
                continue
            _cat, _fid = _r
            if _cat == "materia":
                materia_targets[_fid] = materia_targets.get(_fid, 0) + 1
            else:
                item_targets[_fid] = item_targets.get(_fid, 0) + 1

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

        # traps route into the trap queue instead of the inventory. _seen_trap_indices
        # fires each received trap once, even across a game-over re-deliver.
        if item_name in TRAP_REGISTRY:
            if item_index not in ctx._seen_trap_indices:
                ctx._trap_queue.append(TRAP_REGISTRY[item_name])
                ctx._seen_trap_indices.add(item_index)
                # Persist immediately: a crash between queueing and firing should
                # cost the trap, not re-fire it on every future connect.
                _save_fired_traps(ctx)
                logger.info(f"Trap received: {item_name} (queued).")
            ctx._delivered_item_indices.add(item_index)
            continue

        if item_name in CHOCOBO_ITEM_NAMES:
            sender = ctx.player_names.get(getattr(net_item, "player", None), "")
            if _deliver_chocobo(pm, item_name, sender):
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
            if _deliver_character(pm, item_name, _ap_seed(ctx), ctx):
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
            # Count what we ACTUALLY write. The success line used to be
            # unconditional, so a reconcile pass that wrote nothing still reported
            # "Delivered item: X" — the log actively misled the investigation into
            # the dropped-Alexander bug. A no-op is legitimate during a resync
            # (the player already has the item) but must never read as a delivery.
            wrote = 0
            if category == "materia":
                if ctx._resync_reconcile:
                    # add only the missing count (reading current fresh means the
                    # first index of a type adds the deficit, later ones add 0)
                    deficit = materia_targets.get(ff7_id, 0) - _count_materia_qty(pm, ff7_id)
                    for _ in range(max(0, deficit)):
                        _write_materia(pm, ff7_id)
                        wrote += 1
                else:
                    _write_materia(pm, ff7_id)
                    wrote = 1
            else:
                # items / weapons / armors / accessories all go in the item list
                if ctx._resync_reconcile:
                    deficit = item_targets.get(ff7_id, 0) - _count_item_qty(pm, ff7_id)
                    if deficit > 0:
                        _write_item(pm, ff7_id, deficit)
                        wrote = deficit
                else:
                    _write_item(pm, ff7_id)
                    wrote = 1
            ctx._delivered_item_indices.add(item_index)
            if wrote:
                logger.debug(f"Delivered item: {item_name} (ff7_id={ff7_id})"
                             + (f" x{wrote}" if wrote > 1 else ""))
            else:
                logger.debug(f"Nothing written for '{item_name}' (ff7_id={ff7_id}) — "
                             f"reconcile found the AP-granted count already met")
        except Exception as exc:
            logger.debug(f"Item delivery failed for '{item_name}': {exc}")
            still_pending.append((item_index, net_item))

    ctx._pending_items = still_pending
    # Reconcile mode ends once the full re-delivery has drained.
    if not still_pending:
        ctx._resync_reconcile = False


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
    check. Each line is "<shop_id>:<section>:<index>" (section 4 = item-space
    token, 13 = materia-space token). The DLL already suppressed the inventory
    grant and only signals a buy for a cell the seed reserved as an AP slot, so
    the client just maps (shop, section, index) to its location and fires it."""
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
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3:
            continue
        try:
            shop = int(parts[0].strip(), 0)
            section = int(parts[1].strip(), 0)
            index = int(parts[2].strip(), 0)
        except ValueError:
            continue
        loc = ctx.shop_ap_locations.get((shop, section, index))
        if loc is None:
            logger.debug(f"AP shop buy {shop}:{section}:{index} has no mapped location.")
            continue
        if loc in ctx.checked_locations or loc in ctx._checked_this_session:
            continue
        newly.append(loc)
        ctx._checked_this_session.add(loc)
        logger.debug(f"AP shop purchase (shop {shop} {section}:{index}) → firing location {loc}")
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


def _strip_leaked_shop_materia(pm: "pymem.Pymem", ctx: "FF7Context",
                               fired: List[int]) -> None:
    """Remove a materia token that leaked into inventory despite the DLL's grant
    suppression (FF7 grants shop materia through a path the 0x6CBCF3 hook doesn't
    fully cover — the long-standing reason a token could show up as a broken
    materia).

    Deliberately TARGETED, unlike the old blanket id-strip: it only runs for a
    materia slot JUST purchased this tick, and removes ONE slot holding that exact
    token id with AP 0 (a freshly granted materia), scanning newest-first. A real
    materia of the same id that the player already owns is therefore left alone —
    which matters because under the shop-id-aware scheme a token id can be a real
    materia. (Proper fix is to find the second grant path and suppress it in the
    DLL; this keeps inventory clean until then.)"""
    if not fired or not ctx.shop_ap_locations:
        return
    fired_set = set(fired)
    tokens = {idx for (_shop, sec, idx), loc in ctx.shop_ap_locations.items()
              if sec == KTEXT_MATERIA and loc in fired_set}
    if not tokens:
        return
    try:
        base = SAVEMAP_BASE + MATERIA_LIST_OFFSET
        raw = bytearray(pm.read_bytes(base, MATERIA_SLOT_COUNT * 4))
        changed = False
        for tok in tokens:
            for i in range(MATERIA_SLOT_COUNT - 1, -1, -1):
                mid = raw[i * 4]
                ap = int.from_bytes(raw[i * 4 + 1:i * 4 + 4], "little")
                if mid == tok and ap == 0:
                    del raw[i * 4:i * 4 + 4]
                    raw += b"\xff\xff\xff\xff"      # keep the list compacted
                    changed = True
                    logger.debug(f"Removed leaked shop-token materia id={tok} (slot {i})")
                    break
        if changed:
            pm.write_bytes(base, bytes(raw), len(raw))
    except Exception as exc:
        logger.debug(f"leaked token materia strip failed: {exc}")


# ── Game watcher ──────────────────────────────────────────────────────────────

async def game_watcher(ctx: FF7Context) -> None:
    """Poll FF7's in-memory Savemap; send LocationChecks when BITON flags flip."""
    from worlds.ff7.DeathLink import apply_pending_kill, pump_outbound
    from worlds.ff7.TrapLink import pump_trap_queue

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

                    # The EXP/Gil/AP reward patch lives in the exe's code, so a
                    # fresh process (game relaunched / crashed without an AP
                    # reconnect) starts UNPATCHED. Re-arm the one-shot so it
                    # re-applies to this process — otherwise the multipliers
                    # silently stop after any restart (the "inconsistent
                    # multipliers" report).
                    ctx._reward_mult_applied = False

                    # ── Enable materia menu from start ─────────────────────
                    _enable_materia_menu(pm)

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
                            if ctx._shop_ap_names:
                                _write_shop_ap_txt(exe_dir_p, ctx._shop_ap_names,
                                                   ctx._shop_ap_descs)
                            # Seed shop_sold.txt with already-obtained AP cells so
                            # the DLL removes them from stock on the first shop open.
                            ctx._shop_sold_written = _shop_sold_keys(ctx)
                            _write_shop_sold_txt(exe_dir_p, ctx._shop_sold_written)
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
                            if ctx.shop_ap_locations:
                                logger.debug(
                                    "Shop detection: watching shop_buys.txt for "
                                    f"{len(ctx.shop_ap_locations)} AP shop slot(s)."
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

        # ── Force story-field NPCs interactable in Free Roam (e.g. Ester in the
        # Chocobo Square) so the player can actually talk to them. ──
        _apply_field_model_overrides(pm, ctx)
        # Undo any native character grant the game made ahead of AP delivery.
        _suppress_undelivered_chars(pm, ctx)
        _force_crater_cast_visible(pm, ctx)
        # AFTER the visibility pass — hiding must win if both would touch a model.
        _apply_field_hide_chars(pm, ctx)
        _dedupe_bizarro_aerith(pm, ctx)

        # ── Un-latch one-time story rooms so they replay on revisit ────────
        _apply_field_flag_resets(pm, ctx)

        # ── Free Roam game-over redundancy ─────────────────────────────────
        # A game over reloads the md1stin baseline, wiping every client-
        # delivered AP item (key items, vehicles, party, chocobos, inventory).
        # Watch the live module: when the player returns to gameplay after a
        # Game Over (26), re-queue all received items so nothing is lost. The
        # debounce lets the new-game savemap seeding finish before we re-write,
        # and module 26 is an unambiguous signal (no false positives → no
        # duplicate grants during normal play). Mirrors the /resync command.
        try:
            _module = pm.read_uchar(GAME_MODULE_ADDR)
        except Exception:
            _module = None
        # deathlink outbound: one death per game over, re-armed on resume.
        pump_outbound(ctx, _module)
        if _module == GAME_MODULE_GAMEOVER:
            ctx._game_over_seen = True
            ctx._resume_debounce = 0
        elif ctx._game_over_seen and _module in (GAME_MODULE_FIELD, GAME_MODULE_WORLD):
            ctx._resume_debounce += 1
            if ctx._resume_debounce >= _RESUME_REDELIVER_TICKS:
                ctx._game_over_seen = False
                ctx._resume_debounce = 0
                _enable_materia_menu(pm)   # md1stin baseline disables it again
                _n = _requeue_all_received_items(ctx)
                if _n:
                    logger.info(
                        f"Recovered from a game over — re-delivering {_n} "
                        "received AP item(s)."
                    )

        # ── Deliver queued items (gameplay-module gate) ───────────────────
        # Hold every pending AP item until the player has been in the FIELD or
        # WORLD module for two consecutive polls (same module both ticks).
        # Battle/menu modules operate on their own copies of inventory/party
        # state and write them back over the savemap when they exit, so
        # anything delivered mid-module can be silently lost. Two stable ticks
        # also skip the module-load instant, when the engine is still
        # (re)initialising savemap-backed state.
        # ── Self-heal party limit-technique lists ─────────────────────────
        # Only while stable in a gameplay module (never in battle/menu, whose
        # working copies we'd race). Rebuilds each new party composition once,
        # curing the first-delivered member's empty limit list.
        if _module in (GAME_MODULE_FIELD, GAME_MODULE_WORLD):
            _heal_party_limit_lists(pm, ctx)
        else:
            ctx._party_sig = b""
            ctx._party_sig_stable = 0

        if ctx._pending_items:
            _in_gameplay = (_module in (GAME_MODULE_FIELD, GAME_MODULE_WORLD)
                            and ctx._last_module == _module)
            if _in_gameplay:
                if ctx._delivery_held_logged:
                    logger.debug(
                        f"Gameplay module stable (module={_module}) — flushing "
                        f"{len(ctx._pending_items)} held AP item(s)."
                    )
                ctx._delivery_held_logged = False
                _deliver_items_to_game(pm, ctx)
            elif not ctx._delivery_held_logged:
                ctx._delivery_held_logged = True
                logger.debug(
                    f"Holding {len(ctx._pending_items)} AP item(s) until the "
                    f"player is in the field or world module (module={_module})."
                )
        ctx._last_module = _module

        # ── trap queue + inbound deathlink ────────────────────────────────
        apply_pending_kill(pm, ctx)
        pump_trap_queue(pm, ctx)

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
                # Build the (rel-offset, bit) -> [codes] reverse index so the
                # client's own flag writes can suppress matching pickup locations.
                ctx._biton_rev = {}
                for _c, (_bk, _ad, _bt) in ctx.biton_map.items():
                    _rel = _biton_byte_addr(_bk, _ad) - SAVEMAP_BASE
                    ctx._biton_rev.setdefault((_rel, _bt), []).append(_c)
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

            # ── Retry queued /highwind moves once the entity reloads ──────────
            _pump_vehicle_moves(pm, ctx)

            # ── Self-heal a lost/bad parked coord (no live entity required) ────
            _repair_vehicle_parked_coords(pm, ctx)

            # ── Fresh Free Roam start wipes the savemap: re-deliver everything ─
            _pump_reseed_resync(pm, ctx)

            # ── Drive the Northern Crater gate flag from received goal items ───
            _enforce_crater_lock(pm, ctx)

            # ── Free Roam: skip the Great Glacier snowboard-arrival cutscene ───
            _seed_glacier_wakeup(pm, ctx)

            # ── Free Roam: disarm the engine-driven Diamond Highwind scene ─────
            _suppress_diamond_scene(pm, ctx)

            if ctx.free_roam:
                # Initialise Ultimate's HP field so his chase can start
                # (a fresh Free Roam save leaves it at 0). Nothing else
                # about him is client-driven any more.
                _seed_ultimate_hp(pm, ctx)
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
                        if _ensure_character_record(pm, _cid, _ap_seed(ctx), ctx):
                            logger.debug(f"Re-seeded {_cname} record (was invalid)")
                # Lock the PHS (party-swap) menu until enough characters have been
                # received via AP. Re-asserted each poll (idempotent) so it holds
                # through game-over reloads / md1stin re-seeding; once the player
                # has _PHS_UNLOCK_CHARACTERS members it stays unlocked.
                try:
                    _have = len(ctx._received_item_names & _CHARACTER_ITEM_NAMES)
                    _maddr = SAVEMAP_BASE + _MENU_VISIBLE_OFFSET
                    _mmask = 1 << _MENU_PHS_BIT
                    _mcur = pm.read_ushort(_maddr)
                    _mwant = (_mcur | _mmask) if _have >= _PHS_UNLOCK_CHARACTERS \
                        else (_mcur & ~_mmask)
                    if _mwant != _mcur:
                        pm.write_ushort(_maddr, _mwant)
                except Exception:
                    pass
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
                    # The client owns this bit — never let it fire a pickup location.
                    _suppress_client_flag_locations(ctx, _off, _bit)
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
                        # This gate flag is client-set once the item is received;
                        # suppress any pickup location sharing the bit so the
                        # client's own write can't fire it as a phantom check.
                        _suppress_client_flag_locations(ctx, _goff, _gbit)
                # Vehicle unlock flags. These were previously written ONCE at
                # delivery, but the savemap is re-seeded from the md1stin baseline
                # on a fresh Free Roam start — which wiped them while the gate
                # flags above kept being re-asserted. For the Submarine that meant
                # it spawned "owned" (0xEF4.3 / 0xEF6.2 re-asserted) but with
                # tut_sub CLEAR, so the game ran its ACQUISITION path instead of
                # the parked one: the tutorial fired and the sub drove itself
                # inland along the scripted escape route (playtester 2026-07-23 —
                # coords were a symptom, this is the cause). Re-assert each poll so
                # they survive a re-seed. No AP location uses either byte
                # (0x0C1E / 0x0C23), so there is no detection-bit collision.
                for _vname, (_vaddr, _vmask, _) in VEHICLE_ITEM_FLAGS.items():
                    if _vname not in ctx._received_item_names:
                        continue
                    try:
                        _a = _biton_byte_addr(1, _vaddr)
                        _v = pm.read_uchar(_a)
                        if not (_v & _vmask):
                            pm.write_uchar(_a, _v | _vmask)
                            logger.debug(f"Re-asserted vehicle flag for {_vname} "
                                         f"(addr=0x{_vaddr:02X} mask=0x{_vmask:02X})")
                    except Exception:
                        pass

            # ── Shop purchases: detect token buys, fire checks ────────────────
            # Chocobo racing ranks: earned by win count, not a latched flag.
            race_checks = _chocobo_rank_checks(pm, ctx)
            if race_checks and ctx.server and ctx.slot:
                await ctx.send_msgs([{"cmd": "LocationChecks", "locations": race_checks}])
                for code in race_checks:
                    logger.debug(f"Checked location: {ctx.location_names.lookup_in_game(code)}")

            shop_checks = _process_shop_purchases(pm, ctx)
            if shop_checks:
                # A bought materia token can slip past the DLL's grant suppression
                # and land in inventory as a broken materia — clear just that one.
                _strip_leaked_shop_materia(pm, ctx, shop_checks)
            if shop_checks and ctx.server and ctx.slot:
                await ctx.send_msgs([{"cmd": "LocationChecks", "locations": shop_checks}])
                for code in shop_checks:
                    logger.debug(f"Checked location: {ctx.location_names.lookup_in_game(code)}")
            # Keep shop_sold.txt current: when a shop cell becomes checked (here or
            # via server sync), rewrite so the DLL drops it from stock next visit.
            if ctx._shop_buys_path is not None and ctx.shop_ap_locations:
                _sold = _shop_sold_keys(ctx)
                if _sold != ctx._shop_sold_written:
                    ctx._shop_sold_written = _sold
                    _write_shop_sold_txt(ctx._shop_buys_path.parent, _sold)

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
