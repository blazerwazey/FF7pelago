"""Progression rules for the Final Fantasy VII Archipelago world."""
from __future__ import annotations

from typing import TYPE_CHECKING

from BaseClasses import CollectionState

if TYPE_CHECKING:  # pragma: no cover - hints only
    from .__init__ import FF7World

from .Locations import ALL_LOCATION_TABLE


# Early-game maps: Midgar opening through Sector 7 (pre-Shinra Building).
# This set was derived by freezing the previous name-substring heuristic, which
# was verified to be fully map-consistent (every map's locations classified
# uniformly early-or-late). Matching on the exact field map removes the fragile
# substring logic + manual late-area exclude list while producing identical
# classification. If new early-game locations are added, add their map here.
EARLY_GAME_MAPS = frozenset({
    "gnmk", "md8_3", "mds7_w2", "mds7st1", "mds7st2",
    "nmkin_1", "nmkin_3", "nmkin_5", "nrthmk",
    "sbwy4_6", "smkin_1", "smkin_5",
})


def _is_early_game_location(location) -> bool:
    """True if the location is in early-game Midgar (pre-Shinra Building).

    Restricting linear-mode progression items to these maps keeps the early
    game completable. Classification is by exact field map (data-driven), which
    is robust where the old name-substring matching was not.
    """
    data = ALL_LOCATION_TABLE.get(location.name)
    return data is not None and data.map in EARLY_GAME_MAPS


def apply_rules(world: "FF7World") -> None:
    """Apply FF7-specific access logic."""
    if world.options.free_roam:
        _apply_free_roam_rules(world)
    else:
        _apply_linear_rules(world)

    world.multiworld.completion_condition[world.player] = (
        lambda state: state.has(world.victory_item_name, world.player)
    )


def _apply_linear_rules(world: "FF7World") -> None:
    """Restrict progression items to early game locations to prevent softlocks (linear mode)."""
    player = world.player
    multiworld = world.multiworld

    # List of progression items that must stay in early game
    progression_items = [
        "Battery", "PHS",
        "Cotton Dress", "Satin Dress", "Silk Dress",
        "Wig", "Dyed Wig", "Blonde Wig",
        "Keycard 60", "Keycard 62", "Keycard 65", "Keycard 66", "Keycard 68",
    ]

    # For each location that is NOT early game, prevent progression items from being placed there
    for location in multiworld.get_locations(player):
        if not _is_early_game_location(location):
            location.item_rule = lambda item, prog_items=progression_items: item.name not in prog_items


# Regions reachable with only the forced-early/starter traversal (Green Chocobo +
# Submarine, both pushed into sphere 1) or a single key item — i.e. NOT requiring
# true open-ocean (Blue/Black Chocobo) or the Highwind:
#   foot:            Kalm, Mythril Mines, Chocobo Farm, Fort Condor
#   Green (mountain):Junon Lower, Junon Upper
#   Submarine (_sub):Corel, Gold Saucer Area
#   Key to Sector 5: Midgar Sector 5
# Gold Chocobo is a "skeleton key" (satisfies mountain + ocean + sub at once), so
# we keep it OUT of these regions — the player can't get it until they already have
# real ocean/Highwind traversal, making it a convenience upgrade, not the first key.
_GOLD_CHOCOBO_EARLY_REGIONS = frozenset({
    "Kalm", "Mythril Mines", "Chocobo Farm", "Fort Condor",
    "Junon Lower", "Junon Upper",
    "Corel", "Gold Saucer Area",
    "Midgar Sector 5",
})


def _apply_free_roam_rules(world: "FF7World") -> None:
    """Free Roam: region connections enforce most access. Additionally keep the
    Gold Chocobo out of the early, starter-reachable regions (incl. their shop
    slots) so it can't be obtained before real ocean/Highwind traversal. Safe:
    Gold Chocobo is never a sole requirement (every gate that accepts it also
    accepts Green/Blue/Black/Submarine/Highwind, and the goal needs Highwind), so
    restricting where it can be placed cannot soft-lock a seed."""
    player = world.player
    for location in world.multiworld.get_locations(player):
        region = location.parent_region
        if region is not None and region.name in _GOLD_CHOCOBO_EARLY_REGIONS:
            prev = location.item_rule
            location.item_rule = (
                lambda item, _prev=prev: _prev(item) and item.name != "Gold Chocobo"
            )