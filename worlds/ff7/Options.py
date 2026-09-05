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
    """How strong the randomized starting equipment is (1 = weakest, 5 = strongest).

    Each character's weapon list, and the armor and accessory lists, are broadly
    ordered weakest to strongest in FF7. The tier picks which fifth of those lists
    your starting gear is drawn from, so tier 1 starts Cloud on a Buster Sword and
    tier 5 can start him near the top of his list.

    It also scales how much starting materia you get, and how likely you are to
    begin with an accessory at all. Materia is chosen from the full list at every
    tier: that list is ordered by type rather than power, so slicing it would leave
    low tiers with no healing magic and high tiers with nothing but summons.

    Has no effect unless randomize_starting_equipment is on.
    """
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
    """Start the game on the world map at game moment 1997 (near-endgame state).

    When enabled, the game begins with Cloud and party on the world map with
    foot access to all continents. Vehicles (Tiny Bronco, Highwind, Submarine)
    and Midgar re-entry (via Key to Sector 5) are locked until received as
    Archipelago items. Location pool expands to include Kalm, Junon (lower and
    upper), Gold Saucer area, and Corel in addition to Sector 5 Midgar maps.

    Requires a compatible Gold Saucer .apff7 seed file with free_roam enabled
    to patch the starting save slot.

    **(DO NOT SET TO FALSE)** — Free Roam is the only supported mode. Turning it
    off produces a seed the randomizer and client are not built to run.
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
    to play the minigames. (The Gold Ticket item is not part of the Free Roam
    pool; the area is gated on transport alone.)
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


class ShopSlotsPerShop(Range):
    """How many Archipelago check slots each shop gets (Free Roam, shops on).

    0 (default) keeps the hand-authored counts, which vary from 1 to 6 depending
    on the shop. Any value from 1 to 10 overrides that uniformly: every shop
    offers exactly that many AP slots.

    10 is the hard ceiling — the game's shop records hold ten 8-byte slots each,
    and AP slots take priority over the shop's ordinary stock, so a high value
    means shops sell less of their normal inventory. Raising this adds a lot of
    checks: at 10 the pool gains well over 300.

    Has no effect unless randomize_shops is on.
    """
    display_name = "Shop Slots Per Shop"
    range_start = 0
    range_end = 10
    default = 0


class ProgressiveChocobos(Toggle):
    """Replace the four colour chocobos with one progressive item (Free Roam).

    Off (default): Green, Blue, Black and Gold Chocobo are four separate items,
    each granting its own terrain.

    On: the pool instead holds four copies of "Progressive Chocobo", and each
    copy grants the next bird in order —

      1. Yellow — a chocobo of your own, but it crosses nothing
      2. Green  — cross the Junon mountains
      3. Black  — mountains and open ocean
      4. Gold   — all terrain, including Knights of the Round

    Blue is not on the ladder. Blue and Green are siblings rather than steps —
    one crosses water, the other mountains — so a four-rung ladder has to pick
    one, and this order is how a player actually breeds them.

    Holding N copies means you own every bird up to N, so the tiers are
    cumulative in capability. The first copy opens no new destination; ocean
    crossings arrive with Black at tier 3, which is also when the Chocobo Sage,
    the HP<->MP Cave and the Ancient Forest open.
    """
    display_name = "Progressive Chocobos"
    default = False


class PartyLevelSync(DefaultOnToggle):
    """Characters join at the party leader's level instead of their vanilla one.

    In Free Roam the story never recruits anyone, so six of the eight optional
    characters arrived at their kernel starting level — near level 1 — while
    Cloud had been raised to the Free Roam start level and levelled from there.
    With this on, a delivered character is raised to Cloud's current level, with
    stats computed from their OWN growth curve, once each. Their materia,
    equipment and limit progress are untouched, and a character you have already
    levelled past Cloud is left alone.

    Turn it off if you would rather bring newcomers up yourself.
    """
    display_name = "Party Level Sync"


class DisableGilDumpChecks(Toggle):
    """Remove the "pay a pile of gil" check locations from the pool (Free Roam).

    Two checks are gated purely on spending money rather than on reaching
    anywhere: the Wall Market weapon shop's Sneak Glove, and buying Cloud's
    Villa in Costa del Sol. Enable this if you'd rather not have checks that
    amount to a gil tax. The items on them return to the pool and appear
    elsewhere, so nothing is lost.
    """
    display_name = "Disable Gil Dump Checks"
    default = False


class DisableBoneVillageDigs(Toggle):
    """Remove the Bone Village dig checks from the pool (Free Roam only).

    Drops all five dig rewards — Buntline, Megalixir and Mop, plus the two key
    items normally dug up there (the Lunar Harp and the Key to Sector 5).
    Enable this if you'd rather not play the excavation minigame.

    The Lunar Harp and Key to Sector 5 ITEMS stay in the pool and are placed
    somewhere else, so you never have to dig for them — only the dig locations
    go away. Removing just the three loot digs would have left you excavating
    for two progression items anyway, which is why this covers all five.
    """
    display_name = "Disable Bone Village Digs"
    default = False


class WeaponFightChecks(DefaultOnToggle):
    """Include the optional Weapon superbosses as check locations (Free Roam).

    When on (default), defeating Ultimate, Ruby, and Emerald Weapon each award
    an Archipelago check. When off, the Weapons are not checks — they can still
    be fought, but no item is placed on them.

    Logic requires the Highwind for Ultimate and Ruby, and the Submarine for
    Emerald. Ruby additionally cannot be fought until Ultimate is dead, but that
    is enforced by the GAME, not by logic — Ruby does not spawn until then, and
    Archipelago cannot model a battle outcome, only which items you hold. So
    Ruby enters logic alongside Ultimate: you are expected to kill Ultimate
    first, which the Highwind already lets you do.
    """
    display_name = "Weapon Fight Checks"


class ChocoboRaceChecks(Toggle):
    """Include the Gold Saucer chocobo-racing results as check locations (Free Roam).

    When on, running your first chocobo race and reaching racing Rank S each award
    an Archipelago check. Off by default because it requires the (optional) chocobo-
    racing minigame — enable it only if you want racing in logic.

    **Requires disable_gold_saucer to be set to false.** Chocobo Square is inside
    the Gold Saucer, so disabling Gold Saucer checks removes these two locations as
    well, even with this option turned on.
    """
    display_name = "Chocobo Race Checks"
    default = False


class TownGating(Toggle):
    """Lock towns on the world map behind Archipelago key items (Free Roam only).

    When enabled, certain towns cannot be entered from the world map until you
    receive that town's key item (e.g. "Fort Condor Key", "Junon Key"). Walking
    onto a locked town simply bounces you off until the key arrives. Kalm (the
    starting town) is never locked. Adds the town keys to the item pool as
    progression items, so reaching those towns' checks requires finding the key.
    """
    display_name = "Town Gating"
    default = False


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
    # Kept directly under disable_gold_saucer: the race checks live in the Gold
    # Saucer, so that option silently removes them and the two must be read together.
    chocobo_race_checks: ChocoboRaceChecks
    disable_fort_condor_checks: DisableFortCondorChecks
    shop_slots_per_shop: ShopSlotsPerShop
    progressive_chocobos: ProgressiveChocobos
    party_level_sync: PartyLevelSync
    disable_gil_dump_checks: DisableGilDumpChecks
    disable_bone_village_digs: DisableBoneVillageDigs
    weapon_fight_checks: WeaponFightChecks
    town_gating: TownGating

    # Goal
    victory_condition: VictoryCondition

    # Traps
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
    death_link: DeathLink
