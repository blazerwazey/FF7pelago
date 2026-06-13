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
    random:  Items are replaced with a completely random selection.
    """
    display_name = "Field Items Mode"
    option_shuffle = 0
    option_random = 1
    default = option_shuffle


class FieldItemsKeepType(Toggle):
    """When using Random mode, keep the same item type (weapon stays weapon, etc.)."""
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

    # Goal
    victory_condition: VictoryCondition
    death_link: DeathLink
