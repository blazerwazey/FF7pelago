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


class DisableFortCondorChecks(Toggle):
    """Remove the Fort Condor check locations from the pool (Free Roam only).

    When enabled, the Fort Condor minigame/battle rewards are not checks: the
    Watch Room rewards, Phoenix, Super Ball, and the Fort Condor Huge Materia
    location are all dropped. Useful if you'd rather not play the Fort Condor
    tower-defense minigame. The Fort Condor SHOP is unaffected — its slots stay
    in the pool. (The Huge Materia item itself still appears elsewhere in the
    pool, so the goal is unaffected.)
    """
    display_name = "Disable Fort Condor Checks"
    default = False


class WeaponFightChecks(DefaultOnToggle):
    """Include the optional Weapon superbosses as check locations (Free Roam).

    When on (default), defeating Ultimate, Ruby, and Emerald Weapon each award
    an Archipelago check. When off, the Weapons are not checks — they can still
    be fought, but no item is placed on them. Reaching each still requires the
    relevant traversal (Ruby/Ultimate: open ocean; Emerald: Submarine).
    """
    display_name = "Weapon Fight Checks"


class StartWithChocoboLure(Toggle):
    """Start with a Chocobo Lure materia in your inventory.

    Chocobo Lure raises the chocobo encounter rate on chocobo tracks, making it
    easier to find (and catch) chocobos early. When enabled you begin the game
    with one Chocobo Lure already in your materia stock, in addition to any that
    may appear in the item pool.
    """
    display_name = "Start with Chocobo Lure"
    default = False


# ---------------------------------------------------------------------------
# Options dataclass
# ---------------------------------------------------------------------------

class TrapFillPercent(Range):
    """Percentage of filler item slots to replace with traps (0 = no traps)."""
    display_name = "Trap Fill Percent"
    range_start = 0
    range_end = 100
    default = 0


class FrogTrapWeight(Range):
    """Relative weight of the Frog Trap among enabled traps (0 disables it).

    The Frog Trap gives a random living party member the Frog status during
    battle."""
    display_name = "Frog Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class ConfusionTrapWeight(Range):
    """Relative weight of the Confusion Trap among enabled traps (0 disables it).

    Confuses a random living party member during battle."""
    display_name = "Confusion Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class FrozenTrapWeight(Range):
    """Relative weight of the Frozen Trap among enabled traps (0 disables it).

    Slow-numbs a random living party member during battle."""
    display_name = "Frozen Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class SlownessTrapWeight(Range):
    """Relative weight of the Slowness Trap among enabled traps (0 disables it).

    Slows a random living party member during battle."""
    display_name = "Slowness Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class SlowTrapWeight(Range):
    """Relative weight of the Slow Trap among enabled traps (0 disables it).

    Stops a random living party member during battle."""
    display_name = "Slow Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class InstantDeathTrapWeight(Range):
    """Relative weight of the Instant Death Trap among enabled traps (0 disables it).

    Death-sentences a random living party member during battle."""
    display_name = "Instant Death Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class DoubleDamageWeight(Range):
    """Relative weight of the Double Damage trap among enabled traps (0 disables it).

    Berserks a random living party member during battle."""
    display_name = "Double Damage Weight"
    range_start = 0
    range_end = 100
    default = 100


class PoisonTrapWeight(Range):
    """Relative weight of the Poison Trap among enabled traps (0 disables it).

    Poisons a random living party member during battle."""
    display_name = "Poison Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class TinyTrapWeight(Range):
    """Relative weight of the Tiny Trap among enabled traps (0 disables it).

    Shrinks a random living party member during battle."""
    display_name = "Tiny Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class InstantCrystalTrapWeight(Range):
    """Relative weight of the Instant Crystal Trap among enabled traps (0 disables it).

    Petrifies a random living party member during battle."""
    display_name = "Instant Crystal Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class SleepTrapWeight(Range):
    """Relative weight of the Sleep Trap among enabled traps (0 disables it).

    Puts a random living party member to sleep during battle."""
    display_name = "Sleep Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class ManaDrainTrapWeight(Range):
    """Relative weight of the Mana Drain Trap among enabled traps (0 disables it).

    Drains a random living party member's MP to zero during battle."""
    display_name = "Mana Drain Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class MarketCrashTrapWeight(Range):
    """Relative weight of the Market Crash Trap among enabled traps (0 disables it).

    The market crashes: all of your gil is wiped out."""
    display_name = "Market Crash Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class DepressionTrapWeight(Range):
    """Relative weight of the Depression Trap among enabled traps (0 disables it).

    Saddens a random living party member during battle."""
    display_name = "Depression Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class CurseTrapWeight(Range):
    """Relative weight of the Curse Trap among enabled traps (0 disables it).

    Gives a random living party member Slow-numb or Death-sentence during battle."""
    display_name = "Curse Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class BombTrapWeight(Range):
    """Relative weight of the Bomb Trap among enabled traps (0 disables it).

    Starts a battle against a Bomb (fires on the field or the world map)."""
    display_name = "Bomb Trap Weight"
    range_start = 0
    range_end = 100
    default = 100


class TrapLink(Toggle):
    """Share traps with other TrapLink players. When you receive a trap it is
    broadcast to them, and traps they receive are applied to you too."""
    display_name = "Trap Link"


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
    disable_fort_condor_checks: DisableFortCondorChecks
    weapon_fight_checks: WeaponFightChecks
    start_with_chocobo_lure: StartWithChocoboLure

    # Goal
    victory_condition: VictoryCondition
    death_link: DeathLink
    trap_fill_percent: TrapFillPercent
    frog_trap_weight: FrogTrapWeight
    confusion_trap_weight: ConfusionTrapWeight
    frozen_trap_weight: FrozenTrapWeight
    slowness_trap_weight: SlownessTrapWeight
    slow_trap_weight: SlowTrapWeight
    instant_death_trap_weight: InstantDeathTrapWeight
    double_damage_weight: DoubleDamageWeight
    poison_trap_weight: PoisonTrapWeight
    tiny_trap_weight: TinyTrapWeight
    instant_crystal_trap_weight: InstantCrystalTrapWeight
    sleep_trap_weight: SleepTrapWeight
    mana_drain_trap_weight: ManaDrainTrapWeight
    market_crash_trap_weight: MarketCrashTrapWeight
    depression_trap_weight: DepressionTrapWeight
    curse_trap_weight: CurseTrapWeight
    bomb_trap_weight: BombTrapWeight
    trap_link: TrapLink
