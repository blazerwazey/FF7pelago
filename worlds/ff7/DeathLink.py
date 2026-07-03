"""deathlink for the ff7 archipelago client.

inbound: FF7Context.on_deathlink stores the death in ctx._pending_kill, and
apply_pending_kill (called from game_watcher each tick) kills one random
living party member once the player is in a battle. outbound: pump_outbound
broadcasts our death once per game over, with a grace window so a wipe
caused by an inbound kill doesn't echo back out to the network.
"""
from __future__ import annotations

import random
import struct
import time
from typing import TYPE_CHECKING

from CommonClient import logger
from Utils import async_start

from worlds.ff7.FF7Client import (
    GAME_MODULE_ADDR,
    GAME_MODULE_BATTLE,
    GAME_MODULE_GAMEOVER,
)
from worlds.ff7.TrapLink import (
    BATTLE_CURHP_OFFSET,
    BATTLE_STATUS_OFFSET,
    FF7_STATUS_DEAD,
    live_party_actors,
)

if TYPE_CHECKING:
    import pymem
    from worlds.ff7.FF7Client import FF7Context

# after applying an inbound kill, suppress our own outbound death for this long
# so a party wipe caused by that kill doesn't echo back out to the network.
DEATHLINK_ECHO_GRACE = 10.0


def apply_pending_kill(pm: "pymem.Pymem", ctx: "FF7Context") -> None:
    """kill one random living party member (inbound deathlink). only fires in
    battle; otherwise the kill stays pending until the next battle."""
    if ctx._pending_kill is None:
        return
    try:
        if pm.read_uchar(GAME_MODULE_ADDR) != GAME_MODULE_BATTLE:
            return
    except Exception:
        return
    live = live_party_actors(pm)
    if not live:
        return
    idx, base, _status = random.choice(live)
    try:
        pm.write_ushort(base + BATTLE_CURHP_OFFSET, 0)
        pm.write_bytes(base + BATTLE_STATUS_OFFSET, struct.pack("<I", FF7_STATUS_DEAD), 4)
    except Exception as exc:
        logger.debug(f"DeathLink kill failed: {exc}")
        return
    logger.info(f"DeathLink: killed party actor {idx} ({ctx._pending_kill}).")
    ctx._pending_kill = None
    ctx._deathlink_kill_time = time.time()  # grace window so a resulting wipe doesn't echo


def pump_outbound(ctx: "FF7Context", module) -> None:
    """broadcast our death once per game over (full party wipe), unless a
    recent inbound kill caused it (grace window = no echo). any other module
    re-arms the outbound death latch."""
    if module == GAME_MODULE_GAMEOVER:
        if ("DeathLink" in ctx.tags and not ctx._death_sent_this_over
                and time.time() - ctx._deathlink_kill_time > DEATHLINK_ECHO_GRACE):
            ctx._death_sent_this_over = True
            who = ctx.player_names.get(ctx.slot) if ctx.slot is not None else None
            async_start(ctx.send_death(f"{who or 'FF7'}'s party was wiped out."),
                        name="ff7-deathlink-send")
    elif module is not None:
        ctx._death_sent_this_over = False   # re-arm the outbound death latch
