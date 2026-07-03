"""trap system + traplink for the ff7 archipelago client.

FF7Client routes trap items into ctx._trap_queue, and game_watcher calls pump_trap_queue each tick to fire
them (battle_only traps wait until the player is in a battle). handle_bounced
queues an inbound trap, send_trap_link sends ours.
"""
from __future__ import annotations

import random
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, FrozenSet, List, Optional, Tuple

from CommonClient import logger
from Utils import async_start

from worlds.ff7.FF7Client import (
    GAME_MODULE_ADDR,
    GAME_MODULE_BATTLE,
    GAME_MODULE_FIELD,
    GAME_MODULE_WORLD,
    GIL_OFFSET,
    SAVEMAP_BASE,
)

if TYPE_CHECKING:
    import pymem
    from worlds.ff7.FF7Client import FF7Context

# ── in-battle actor memory (party status/hp, shared with deathlink) ───────────
# live battle character array, addresses from ff7-ultima / ff7-lib (base + i*104, hp at +0x2C, status at +0)
# actors 0-2 are the player party, 3 is the formation ai, 4-9 are enemies.
# only valid while module == 2 (battle).
BATTLE_CHAR_BASE     = 0x9AB0DC   # actor 0 base
BATTLE_CHAR_STRIDE   = 104        # 0x68 bytes per actor
BATTLE_PARTY_SLOTS   = (0, 1, 2)  # player-controlled actors
BATTLE_STATUS_OFFSET = 0x00       # u32 status bitfield within an actor
BATTLE_CURHP_OFFSET  = 0x2C       # current hp within an actor
BATTLE_CURMP_OFFSET  = 0x28       # current mp within an actor
# status bits
FF7_STATUS_DEAD           = 0x00000001
FF7_STATUS_NEAR_DEATH     = 0x00000002
FF7_STATUS_SLEEP          = 0x00000004
FF7_STATUS_POISON         = 0x00000008
FF7_STATUS_SADNESS        = 0x00000010
FF7_STATUS_FURY           = 0x00000020
FF7_STATUS_CONFUSION      = 0x00000040
FF7_STATUS_SILENCE        = 0x00000080
FF7_STATUS_HASTE          = 0x00000100
FF7_STATUS_SLOW           = 0x00000200
FF7_STATUS_STOP           = 0x00000400
FF7_STATUS_FROG           = 0x00000800
FF7_STATUS_SMALL          = 0x00001000
FF7_STATUS_SLOW_NUMB      = 0x00002000
FF7_STATUS_PETRIFY        = 0x00004000
FF7_STATUS_REGEN          = 0x00008000
FF7_STATUS_BARRIER        = 0x00010000
FF7_STATUS_MBARRIER       = 0x00020000
FF7_STATUS_REFLECT        = 0x00040000
FF7_STATUS_DUAL           = 0x00080000 #unused
FF7_STATUS_SHIELD         = 0x00100000
FF7_STATUS_DEATH_SENTENCE = 0x00200000
FF7_STATUS_MANIPULATE     = 0x00400000
FF7_STATUS_BERSERK        = 0x00800000
FF7_STATUS_PEERLESS       = 0x01000000
FF7_STATUS_PARALYZE       = 0x02000000
FF7_STATUS_DARKNESS       = 0x04000000
FF7_STATUS_DUAL_DRAIN     = 0x08000000
FF7_STATUS_DEATH_FORCE    = 0x10000000
FF7_STATUS_RESIST         = 0x20000000
FF7_STATUS_LUCKY_GIRL     = 0x40000000
FF7_STATUS_IMPRISONED     = 0x80000000

# minimum seconds between trap activations so we arent spamming traps
TRAP_ACTIVATION_COOLDOWN = 5.0


@dataclass(frozen=True)
class TrapSpec:
    name: str                      # ap item name (== items.json key)
    battle_only: bool              # defer application until module == battle
    apply: Callable                # apply(pm, ctx) -> bool (true once it has fired)
    traplink_send: str             # canonical name broadcast to traplink players
    traplink_recv: FrozenSet[str]  # inbound traplink names that map to this trap
# refer to trap link spreadsheet
# https://docs.google.com/spreadsheets/d/1yoNilAzT5pSU9c2hYK7f2wHAe9GiWDiHFZz8eMe1oeQ/edit?gid=811965759#gid=811965759 
# for trap names (generally we send & receive as 1 name and then also receive as a few alternatives to broaden support)
# imo if you can figure out a wacky and unfair trap go all in because thats just good sportsmanship :)

def live_party_actors(pm: "pymem.Pymem") -> List[Tuple[int, int, int]]:
    """return [(actor_index, base_addr, status)] for each living party actor
    (hp > 0, not dead) in the current battle."""
    live: List[Tuple[int, int, int]] = []
    for idx in BATTLE_PARTY_SLOTS:
        base = BATTLE_CHAR_BASE + idx * BATTLE_CHAR_STRIDE
        try:
            hp = pm.read_ushort(base + BATTLE_CURHP_OFFSET)
            status = struct.unpack("<I", pm.read_bytes(base + BATTLE_STATUS_OFFSET, 4))[0]
        except Exception:
            continue
        if 0 < hp <= 9999 and not (status & FF7_STATUS_DEAD):
            live.append((idx, base, status))
    return live


def _inflict_status_on_random_party(pm: "pymem.Pymem", bit: int, flavour: str) -> Optional[int]:
    """or a status bit onto a random living party member that doesn't already
    have it. returns the actor index, or None when there's no valid target yet
    (the trap stays queued to retry)."""
    targets = [(i, b, s) for (i, b, s) in live_party_actors(pm) if not (s & bit)]
    if not targets:
        return None
    idx, base, status = random.choice(targets)
    pm.write_bytes(base + BATTLE_STATUS_OFFSET, struct.pack("<I", status | bit), 4)
    logger.info(f"{flavour} (party actor {idx}).")
    return idx


def _apply_frog_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """turn a random living party member into a frog."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_FROG, "Frog Trap: turned a party member into a frog") is not None


def _apply_confusion_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """confuse a random living party member."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_CONFUSION, "Confusion Trap: confused a party member") is not None


def _apply_frozen_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """slow-numb a random living party member (freezes toward petrify)."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_SLOW_NUMB, "Frozen Trap: froze a party member") is not None


def _apply_slowness_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """slow a random living party member."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_SLOW, "Slowness Trap: slowed a party member") is not None


def _apply_slow_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """stop a random living party member."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_STOP, "Slow Trap: stopped a party member") is not None


def _apply_instant_death_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """death-sentence a random living party member (countdown to death)."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_DEATH_SENTENCE, "Instant Death Trap: sentenced a party member") is not None


def _apply_double_damage_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """berserk a random living party member."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_BERSERK, "Double Damage: berserked a party member") is not None


def _apply_poison_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """poison a random living party member."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_POISON, "Poison Trap: poisoned a party member") is not None


def _apply_tiny_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """shrink a random living party member."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_SMALL, "Tiny Trap: shrank a party member") is not None


def _apply_instant_crystal_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """petrify a random living party member."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_PETRIFY, "Instant Crystal Trap: petrified a party member") is not None


def _apply_sleep_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """put a random living party member to sleep."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_SLEEP, "Sleep Trap: put a party member to sleep") is not None


def _apply_mana_drain_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """drain a random living party member's mp to zero."""
    targets = [(i, b) for (i, b, s) in live_party_actors(pm)
               if pm.read_ushort(b + BATTLE_CURMP_OFFSET) > 0]
    if not targets:
        return False
    idx, base = random.choice(targets)
    pm.write_bytes(base + BATTLE_CURMP_OFFSET, struct.pack("<H", 0), 2)
    logger.info(f"Mana Drain Trap: drained party actor {idx}'s MP.")
    return True


def _apply_market_crash_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """wipe out all party gil."""
    gil = pm.read_uint(SAVEMAP_BASE + GIL_OFFSET)
    pm.write_bytes(SAVEMAP_BASE + GIL_OFFSET, struct.pack("<I", 0), 4)
    logger.info(f"Market Crash Trap: wiped out {gil} gil.")
    return True


def _apply_depression_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """sadden a random living party member."""
    return _inflict_status_on_random_party(pm, FF7_STATUS_SADNESS, "Depression Trap: saddened a party member") is not None


def _apply_curse_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """inflict slow-numb or death sentence on a random living party member."""
    bit, label = random.choice([
        (FF7_STATUS_SLOW_NUMB, "slow-numb"),
        (FF7_STATUS_DEATH_SENTENCE, "death sentence"),
    ])
    return _inflict_status_on_random_party(pm, bit, f"Curse Trap: {label} on a party member") is not None


# start-a-battle mechanism (thanks ff7-ultima)
BOMB_TRAP_BATTLE_ID   = 500        # formation 500 = bomb [north corel]
FIELD_OBJ_PTR         = 0xCBF9D8   # -> current module global object
BATTLE_MODULE_FIELD   = 0xCBF6B8   # field module transition latch
BATTLE_ID_WORLD       = 0xE3A88C
WORLD_BATTLE_FLAG1    = 0xE2BBC8
WORLD_BATTLE_FLAG2    = 0x969950
WORLD_BATTLE_FLAG3    = 0xE3A884
WORLD_BATTLE_FLAG4    = 0xE045E4


def _apply_bomb_trap(pm: "pymem.Pymem", ctx: "FF7Context") -> bool:
    """start a battle against a bomb. fires from the field or the world map,
    stays queued in any other module (battles, menus, etc)."""
    module = pm.read_uchar(GAME_MODULE_ADDR)
    if module == GAME_MODULE_FIELD:
        ptr = pm.read_uint(FIELD_OBJ_PTR)
        if ptr < 0x400000:
            return False
        pm.write_bytes(ptr + 1, b"\x02", 1)
        pm.write_bytes(ptr + 2, struct.pack("<H", BOMB_TRAP_BATTLE_ID), 2)
        pm.write_bytes(ptr + 38, struct.pack("<H", 0), 2)
        pm.write_bytes(ptr + 68, b"\x00", 1)   # battle music (0 = standard theme)
        pm.write_bytes(BATTLE_MODULE_FIELD, b"\x01", 1)
        ctx._bomb_field_reset = ptr    # ptr+1 cleared once the battle starts
        logger.info("Bomb Trap: a Bomb attacks!")
        return True
    if module == GAME_MODULE_WORLD:
        pm.write_bytes(BATTLE_ID_WORLD, struct.pack("<I", BOMB_TRAP_BATTLE_ID), 4)
        pm.write_bytes(WORLD_BATTLE_FLAG1, struct.pack("<I", 0), 4)
        pm.write_bytes(WORLD_BATTLE_FLAG2, struct.pack("<I", 0), 4)
        pm.write_bytes(WORLD_BATTLE_FLAG3, struct.pack("<I", 1), 4)
        pm.write_bytes(WORLD_BATTLE_FLAG4, struct.pack("<I", 3), 4)
        logger.info("Bomb Trap: a Bomb attacks!")
        return True
    return False


def _pump_bomb_field_reset(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """clear the field object's module-request byte once the battle is live."""
    ptr = getattr(ctx, "_bomb_field_reset", None)
    if ptr is None:
        return
    try:
        if pm.read_uchar(GAME_MODULE_ADDR) == GAME_MODULE_BATTLE:
            pm.write_bytes(ptr + 1, b"\x00", 1)
            ctx._bomb_field_reset = None
    except Exception as exc:
        logger.debug(f"bomb field reset failed: {exc}")
        ctx._bomb_field_reset = None


TRAP_REGISTRY: Dict[str, TrapSpec] = {
    "Frog Trap": TrapSpec(
        name="Frog Trap",
        battle_only=True,
        apply=_apply_frog_trap,
        traplink_send="Frog Trap",
        traplink_recv=frozenset({"Frog Trap", "Toad Trap", "Frog"}),
    ),
    "Confusion Trap": TrapSpec(
        name="Confusion Trap",
        battle_only=True,
        apply=_apply_confusion_trap,
        traplink_send="Confusion Trap",
        traplink_recv=frozenset({"Confusion Trap", "Confuse Trap", "Confound Trap"}),
    ),
    "Frozen Trap": TrapSpec(
        name="Frozen Trap",
        battle_only=True,
        apply=_apply_frozen_trap,
        traplink_send="Frozen Trap",
        traplink_recv=frozenset({"Frozen Trap", "Freeze Trap", "Frost Trap"}),
    ),
    "Slowness Trap": TrapSpec(
        name="Slowness Trap",
        battle_only=True,
        apply=_apply_slowness_trap,
        traplink_send="Slowness Trap",
        traplink_recv=frozenset({"Slowness Trap"}),
    ),
    "Slow Trap": TrapSpec(
        name="Slow Trap",
        battle_only=True,
        apply=_apply_slow_trap,
        traplink_send="Slow Trap",
        traplink_recv=frozenset({"Slow Trap"}),
    ),
    "Instant Death Trap": TrapSpec(
        name="Instant Death Trap",
        battle_only=True,
        apply=_apply_instant_death_trap,
        traplink_send="Instant Death Trap",
        traplink_recv=frozenset({"Instant Death Trap"}),
    ),
    "Double Damage": TrapSpec(
        name="Double Damage",
        battle_only=True,
        apply=_apply_double_damage_trap,
        traplink_send="Double Damage",
        traplink_recv=frozenset({"Double Damage"}),
    ),
    "Poison Trap": TrapSpec(
        name="Poison Trap",
        battle_only=True,
        apply=_apply_poison_trap,
        traplink_send="Poison Trap",
        traplink_recv=frozenset({"Poison Trap", "Radiation Trap", "Toxin Trap"}),
    ),
    "Tiny Trap": TrapSpec(
        name="Tiny Trap",
        battle_only=True,
        apply=_apply_tiny_trap,
        traplink_send="Tiny Trap",
        traplink_recv=frozenset({"Tiny Trap", "Poison Mushroom", "Squash Trap"}),
    ),
    "Instant Crystal Trap": TrapSpec(
        name="Instant Crystal Trap",
        battle_only=True,
        apply=_apply_instant_crystal_trap,
        traplink_send="Instant Crystal Trap",
        traplink_recv=frozenset({"Instant Crystal Trap"}),
    ),
    "Sleep Trap": TrapSpec(
        name="Sleep Trap",
        battle_only=True,
        apply=_apply_sleep_trap,
        traplink_send="Sleep Trap",
        traplink_recv=frozenset({"Sleep Trap"}),
    ),
    "Mana Drain Trap": TrapSpec(
        name="Mana Drain Trap",
        battle_only=True,
        apply=_apply_mana_drain_trap,
        traplink_send="Mana Drain Trap",
        traplink_recv=frozenset({"Mana Drain Trap", "Depletion Trap"}),
    ),
    "Market Crash Trap": TrapSpec(
        name="Market Crash Trap",
        battle_only=False,
        apply=_apply_market_crash_trap,
        traplink_send="Market Crash Trap",
        traplink_recv=frozenset({"Market Crash Trap"}),
    ),
    "Depression Trap": TrapSpec(
        name="Depression Trap",
        battle_only=True,
        apply=_apply_depression_trap,
        traplink_send="Depression Trap",
        traplink_recv=frozenset({"Depression Trap"}),
    ),
    "Curse Trap": TrapSpec(
        name="Curse Trap",
        battle_only=True,
        apply=_apply_curse_trap,
        traplink_send="Curse Trap",
        traplink_recv=frozenset({"Curse Trap"}),
    ),
    "Bomb Trap": TrapSpec(
        name="Bomb Trap",
        battle_only=False,
        apply=_apply_bomb_trap,
        traplink_send="Bomb Trap",
        traplink_recv=frozenset({"Bomb Trap", "Bomb", "Explosion Trap"}),
    ),
}
# inbound traplink lookup: canonical trap name -> local TrapSpec.
TRAPLINK_RECV_INDEX: Dict[str, TrapSpec] = {
    name: spec for spec in TRAP_REGISTRY.values() for name in spec.traplink_recv
}


def resolve_trap(query: str) -> Optional[TrapSpec]:
    """resolve a user-typed name like 'frog' or 'sleep trap' to a TrapSpec.
    matches the full name, the name minus ' trap', then a substring, then any
    accepted traplink alias."""
    q = query.strip().lower()
    if not q:
        return None
    for spec in TRAP_REGISTRY.values():
        name = spec.name.lower()
        if q == name or q == name.replace(" trap", "").strip():
            return spec
    for spec in TRAP_REGISTRY.values():
        if q in spec.name.lower():
            return spec
    for alias, spec in TRAPLINK_RECV_INDEX.items():
        if q == alias.lower():
            return spec
    return None


def _can_activate_trap(pm: "pymem.Pymem", spec: TrapSpec) -> bool:
    try:
        module = pm.read_uchar(GAME_MODULE_ADDR)
    except Exception:
        return False
    if spec.battle_only:
        return module == GAME_MODULE_BATTLE
    return module in (GAME_MODULE_FIELD, GAME_MODULE_WORLD, GAME_MODULE_BATTLE)


def handle_bounced(ctx: "FF7Context", args: dict) -> None:
    """traplink bounce packet: another player got a trap, queue our matching one.
    (deathlink bounces are dispatched to on_deathlink by CommonContext.)"""
    data = args.get("data", {})
    tags = args.get("tags", [])
    if ("TrapLink" in ctx.tags and "TrapLink" in tags and ctx.slot is not None
            and data.get("source") != ctx.player_names.get(ctx.slot)):
        spec = TRAPLINK_RECV_INDEX.get(data.get("trap_name", ""))
        if spec is not None:
            ctx._priority_trap = spec
            logger.info(f"TrapLink: {data.get('source')} sent {data.get('trap_name')}.")


async def send_trap_link(ctx: "FF7Context", trap_name: str) -> None:
    """broadcast a locally-fired trap to other traplink players (bounce)."""
    if "TrapLink" not in ctx.tags or ctx.slot is None:
        return
    await ctx.send_msgs([{
        "cmd": "Bounce", "tags": ["TrapLink"],
        "data": {
            "time": time.time(),
            "source": ctx.player_names[ctx.slot],
            "trap_name": trap_name,
        },
    }])


def pump_trap_queue(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """apply at most one queued trap per tick, deferring battle_only traps
    until the player is in a valid state."""
    _pump_bomb_field_reset(pm, ctx)

    now = time.time()
    if now - ctx._last_trap_activation < TRAP_ACTIVATION_COOLDOWN:
        return

    spec: Optional[TrapSpec] = None
    from_trap_link = False
    if ctx._priority_trap is not None and _can_activate_trap(pm, ctx._priority_trap):
        spec, from_trap_link = ctx._priority_trap, True
    elif ctx._trap_queue and _can_activate_trap(pm, ctx._trap_queue[0]):
        spec = ctx._trap_queue[0]
    if spec is None:
        return

    try:
        fired = bool(spec.apply(pm, ctx))
    except Exception as exc:
        logger.debug(f"Trap {spec.name} failed: {exc}")
        fired = False
    if not fired:
        return  # no valid target this instant - leave queued, retry next tick

    ctx._last_trap_activation = now
    if from_trap_link:
        ctx._priority_trap = None
    else:
        ctx._trap_queue.popleft()
        if "TrapLink" in ctx.tags:
            async_start(send_trap_link(ctx, spec.traplink_send), name="ff7-traplink-send")
