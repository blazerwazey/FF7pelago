"""Final Fantasy VII Archipelago options.

Archipelago option definitions for the FF7pelago world.
Values are exported into the .apff7 seed file for the client to read.
"""
from __future__ import annotations

from dataclasses import dataclass

from Options import Choice, Range, Toggle, DefaultOnToggle, DeathLink, PerGameCommonOptions


# ---------------------------------------------------------------------------
# Randomizer options
# ---------------------------------------------------------------------------

class RandomizeFieldItems(Toggle):
    """Randomize items found in the field (treasure chests, pickups, materia)."""
    display_name = "Randomize Field Items"
    default = True


class FieldItemsMode(Choice):
    """Controls how field items are randomized.

    shuffle: Items are shuffled among the original locations (same pool, different spots).
    replace: Items are replaced with a completely random selection.

    (Note: "random" cannot be used as a Choice value name in Archipelago — it is
    reserved for the meta "pick a random setting" keyword — so this mode is named
    "replace". The exported value is unchanged: shuffle=0, replace=1.)
    """
    display_name = "Field Items Mode"
    option_shuffle = 0
    option_replace = 1
    default = option_shuffle


class FieldItemsKeepType(Toggle):
    """When using Replace mode, keep the same item type (weapon stays weapon, etc.)."""
    display_name = "Field Items Keep Type"
    default = False


class RandomizeShops(Toggle):
    """Randomize shop inventories."""
    display_name = "Randomize Shops"
    default = False



class RandomizeStartingEquipment(DefaultOnToggle):
    """Randomize starting equipment for each character (base Gold Saucer feature)."""
    display_name = "Randomize Starting Equipment"


class StartingEquipmentTier(Range):
    """Tier of starting equipment when randomization is enabled (1-5, higher = better)."""
    display_name = "Starting Equipment Tier"
    range_start = 1
    range_end = 5
    default = 3


# ---------------------------------------------------------------------------
# Goal
# ---------------------------------------------------------------------------

class VictoryCondition(Choice):
    """Goal required to complete the seed."""
    display_name = "Victory Condition"
    option_defeat_sephiroth = 0
    # option_escape_midgar = 1
    default = option_defeat_sephiroth


class FreeRoam(Toggle):
    """Start the game on the world map at game moment 1603 (near-endgame state).

    When enabled, the game begins with Cloud and party on the world map with
    foot access to all continents. Vehicles (Tiny Bronco, Highwind, Submarine)
    and Midgar re-entry (via Key to Sector 5) are locked until received as
    Archipelago items. Location pool expands to include Kalm, Junon (lower and
    upper), Gold Saucer area, and Corel in addition to Sector 5 Midgar maps.

    Requires a compatible Gold Saucer .apff7 seed file with free_roam enabled
    to patch the starting save slot.
    """
    display_name = "Free Roam"
    default = True


# ---------------------------------------------------------------------------
# Gameplay QoL — battle reward multipliers (applied live by the client by
# patching the battle EXP/AP/Gil calc instructions; 1 = vanilla)
# ---------------------------------------------------------------------------

class ExpMultiplier(Range):
    """Multiply all battle EXP gained. 1 = normal."""
    display_name = "EXP Multiplier"
    range_start = 1
    range_end = 50
    default = 1


class GilMultiplier(Range):
    """Multiply all battle Gil gained. 1 = normal."""
    display_name = "Gil Multiplier"
    range_start = 1
    range_end = 50
    default = 1


class APMultiplier(Range):
    """Multiply all battle AP (materia ability points) gained. 1 = normal."""
    display_name = "AP Multiplier"
    range_start = 1
    range_end = 50
    default = 1


class DisableGoldSaucer(Toggle):
    """Remove every Gold Saucer check from the pool (Free Roam only).

    When enabled, no locations inside the Gold Saucer (Wonder Square, Battle
    Square / Arena, Chocobo Square, Ghost Hotel, Speed Square, Event Square,
    Gondola, the Keystone and Gold Ticket key items, etc.) are checks, and the
    Gold Saucer shop slots are dropped. Useful if you'd rather not be required
    to play the minigames. The Gold Ticket item still controls access to the
    area for any logic that needs it.
    """
    display_name = "Disable Gold Saucer Checks"
    default = False


class WeaponFightChecks(DefaultOnToggle):
    """Include the optional Weapon superbosses as check locations (Free Roam).

    When on (default), defeating Ultimate, Ruby, and Emerald Weapon each award
    an Archipelago check. When off, the Weapons are not checks — they can still
    be fought, but no item is placed on them. Reaching each still requires the
    relevant traversal (Ruby/Ultimate: open ocean; Emerald: Submarine).
    """
    display_name = "Weapon Fight Checks"


# ---------------------------------------------------------------------------
# Options dataclass
# ---------------------------------------------------------------------------

@dataclass
class FF7Options(PerGameCommonOptions):
    """Container for all FF7pelago Archipelago settings."""

    # Randomizers
    randomize_field_items: RandomizeFieldItems
    field_items_mode: FieldItemsMode
    field_items_keep_type: FieldItemsKeepType
    randomize_shops: RandomizeShops
    #disable_shops: DisableShops
    #randomize_bosses: RandomizeBosses
    #boss_min_stat_multiplier: BossMinStatMultiplier
    #boss_max_stat_multiplier: BossMaxStatMultiplier
    randomize_starting_equipment: RandomizeStartingEquipment
    starting_equipment_tier: StartingEquipmentTier

    # World
    free_roam: FreeRoam

    # Gameplay QoL
    exp_multiplier: ExpMultiplier
    gil_multiplier: GilMultiplier
    ap_multiplier: APMultiplier
    disable_gold_saucer: DisableGoldSaucer
    weapon_fight_checks: WeaponFightChecks

    # Goal
    victory_condition: VictoryCondition
    death_link: DeathLink
