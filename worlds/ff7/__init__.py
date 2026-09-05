"""Final Fantasy VII IronMog Archipelago world implementation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import logging
import settings

from BaseClasses import Item, ItemClassification, MultiWorld, Region, Tutorial
from Options import DeathLink, OptionGroup, PerGameCommonOptions
from worlds.AutoWorld import WebWorld, World
from worlds.LauncherComponents import Component, Type, components, launch

from .Items import ITEM_TABLE, create_ff7_item, item_name_groups, item_name_to_id
from .Locations import (
    ALL_LOCATION_TABLE, FF7Location, PLACEABLE_LOCATION_CODES,
    SHOP_LOCATION_TABLE, SHOP_SLOTS_BY_SHOP, SHIPPED_SHOP_CODES,
    location_name_groups, location_name_to_id,
)
from .Options import (
    FF7Options,
    RandomizeFieldItems,
    FieldItemsMode,
    FieldItemsKeepType,
    RandomizeShops,
    RandomizeStartingEquipment,
    StartingEquipmentTier,
    FreeRoam,
    DisableGoldSaucer,
    ChocoboRaceChecks,
    DisableFortCondorChecks,
    DisableGilDumpChecks,
    DisableBoneVillageDigs,
    ShopSlotsPerShop,
    ProgressiveChocobos,
    PartyLevelSync,
    WeaponFightChecks,
    TownGating,
    ExpMultiplier,
    GilMultiplier,
    APMultiplier,
    VictoryCondition,
    TrapFillPercent,
    FrogTrapWeight,
    ConfusionTrapWeight,
    FrozenTrapWeight,
    SlownessTrapWeight,
    SlowTrapWeight,
    InstantDeathTrapWeight,
    DoubleDamageWeight,
    PoisonTrapWeight,
    TinyTrapWeight,
    InstantCrystalTrapWeight,
    SleepTrapWeight,
    ManaDrainTrapWeight,
    MarketCrashTrapWeight,
    DepressionTrapWeight,
    CurseTrapWeight,
    BombTrapWeight,
    TrapLink,
)
from .Rules import apply_rules
from .json_export import FF7JSONExporter


FREE_ROAM_REGION_MAP: dict[str, str] = {
    # --- Kalm (field prefix: elm) ---
    "elm":        "Kalm",
    "elmin1_1":   "Kalm",
    "elmin1_2":   "Kalm",
    "elmin2_1":   "Kalm",
    "elmin2_2":   "Kalm",
    "elmin3_1":   "Kalm",
    "elmin3_2":   "Kalm",
    "elmin4_1":   "Kalm",
    "elmin4_2":   "Kalm",
    "elminn_1":   "Kalm",
    "elminn_2":   "Kalm",
    "elm_wa":     "Kalm",
    "elm_i":      "Kalm",
    "elmpb":      "Kalm",
    "elmtow":     "Kalm",

    # --- Mythril Mines (field prefix: psdun) — foot-reachable from Kalm ---
    "psdun_1":    "Mythril Mines",
    "psdun_2":    "Mythril Mines",
    "psdun_3":    "Mythril Mines",
    "psdun_4":    "Mythril Mines",

    # --- Junon Lower (under-Junon / beach area) ---
    "ujunon1":    "Junon Lower",
    "ujunon2":    "Junon Lower",
    "ujunon3":    "Junon Lower",
    "ujunon4":    "Junon Lower",
    "ujunon5":    "Junon Lower",
    "junonl1":    "Junon Lower",
    "junonl2":    "Junon Lower",
    "junonl3":    "Junon Lower",
    "prisila":    "Junon Lower",
    "ujun_w":     "Junon Lower",
    "jumin":      "Junon Lower",
    "juninn":     "Junon Lower",
    "junpb_1":    "Junon Lower",
    "junpb_2":    "Junon Lower",
    "junpb_3":    "Junon Lower",
    "junmin4":    "Junon Lower",
    "junmin5":    "Junon Lower",
    "jundoc1a":   "Junon Lower",
    "jundoc1b":   "Junon Lower",

    # --- Junon Upper ---
    "junon":      "Junon Upper",
    "junonr1":    "Junon Upper",
    "junonr2":    "Junon Upper",
    "junonr3":    "Junon Upper",
    "junonr4":    "Junon Upper",
    "jun_wa":     "Junon Upper",
    "jun_i1":     "Junon Upper",
    "jun_m":      "Junon Upper",
    "junmin1":    "Junon Upper",
    "junmin2":    "Junon Upper",
    "junmin3":    "Junon Upper",
    "junin1":     "Junon Upper",
    "junin1a":    "Junon Upper",
    "junele1":    "Junon Upper",
    "junin2":     "Junon Upper",
    "junin3":     "Junon Upper",
    "junele2":    "Junon Upper",
    "junin4":     "Junon Upper",
    "junin5":     "Junon Upper",
    "junin6":     "Junon Upper",
    "junin7":     "Junon Upper",
    "junbin1":    "Junon Upper",
    "junbin12":   "Junon Upper",
    "junbin21":   "Junon Upper",
    "junbin22":   "Junon Upper",
    "junbin3":    "Junon Upper",
    "junbin4":    "Junon Upper",
    "junbin5":    "Junon Upper",
    "junmon":     "Junon Upper",
    "junone2":    "Junon Upper",
    "junone3":    "Junon Upper",
    "junone4":    "Junon Upper",
    "junone5":    "Junon Upper",
    "junone6":    "Junon Upper",
    "junone7":    "Junon Upper",
    "junair":     "Junon Upper",
    "junair2":    "Junon Upper",
    "jumsbd1":    "Junon Upper",

    # --- Corel / North Corel ---
    "ncorel":     "Corel",
    "ncorel2":    "Corel",
    "ncorel3":    "Corel",
    "ncoin1":     "Corel",
    "ncoin2":     "Corel",
    "ncoin3":     "Corel",
    "ncoinn":     "Corel",
    "ropest":     "Corel",
    # Mt. Corel (the mountain trek) needs Highwind/Gold like Gongaga — the
    # Submarine reaches North Corel + Gold Saucer but not here. Split off.
    "mtcrl_0":    "Mt. Corel (Costa)",
    "mtcrl_1":    "Mt. Corel (Costa)",
    "mtcrl_2":    "Mt. Corel (Costa)",
    "mtcrl_3":    "Mt. Corel (Costa)",
    "mtcrl_4":    "Mt. Corel (Costa)",
    "mtcrl_5":    "Mt. Corel (Costa)",
    "mtcrl_6":    "Mt. Corel (Costa)",
    "mtcrl_7":    "Mt. Corel (North)",
    "mtcrl_8":    "Mt. Corel (North)",
    "mtcrl_9":    "Mt. Corel (North)",
    "jail1":      "Corel",
    "jail2":      "Corel",
    "jail3":      "Corel",
    "jail4":      "Corel",
    "jailin1":    "Corel",
    "jailin2":    "Corel",
    "jailin3":    "Corel",
    "jailin4":    "Corel",
    "jailpb":     "Corel",
    "dyne":       "Corel",
    "desert1":    "Corel",
    "desert2":    "Corel",
    "corelin":    "Corel",

    # --- Gold Saucer Area (requires Gold Ticket) ---
    "gldst":      "Gold Saucer Area",
    "gldgate":    "Gold Saucer Area",
    "gldinfo":    "Gold Saucer Area",
    "gldelev":    "Gold Saucer Area",
    "games":      "Gold Saucer Area",
    "games_1":    "Gold Saucer Area",
    "games_2":    "Gold Saucer Area",
    "coloss":     "Gold Saucer Area",
    "coloin1":    "Gold Saucer Area",
    "coloin2":    "Gold Saucer Area",
    "clsin2_1":   "Gold Saucer Area",
    "clsin2_2":   "Gold Saucer Area",
    "clsin2_3":   "Gold Saucer Area",
    "ghotel":     "Gold Saucer Area",
    "ghotin_1":   "Gold Saucer Area",
    "ghotin_2":   "Gold Saucer Area",
    "ghotin_3":   "Gold Saucer Area",
    "ghotin_4":   "Gold Saucer Area",
    "crcin_1":    "Gold Saucer Area",
    "crcin_2":    "Gold Saucer Area",
    "chorace":    "Gold Saucer Area",
    "chorace2":   "Gold Saucer Area",
    "jet":        "Gold Saucer Area",
    "jetin1":     "Gold Saucer Area",
    "bigwheel":   "Gold Saucer Area",
    "bwhlin":     "Gold Saucer Area",
    "bwhlin2":    "Gold Saucer Area",
    "astage_a":   "Gold Saucer Area",
    "astage_b":   "Gold Saucer Area",
    "mogu_1":     "Gold Saucer Area",

    # --- Midgar Sector 5 (requires Key to Sector 5) ---
    "mds5_1":     "Midgar Sector 5",
    "mds5_2":     "Midgar Sector 5",
    "mds5_3":     "Midgar Sector 5",
    "mds5_4":     "Midgar Sector 5",
    # mds5_5 is the field the Midgar highway EMPTIES INTO — the outskirts, on the
    # near side of the Sector 5 walkmesh gate Gold Saucer installs. It is walkable
    # from Kalm with no transport, so the Motor Ball chest standing there needs no
    # Key to Sector 5. Confirmed in play 2026-09-05; it had inherited the interior
    # region's rule, which was logic stricter than the game.
    # It is the ONLY location on this field, so nothing else moves with it.
    "mds5_5":     "Midgar Outskirts",
    "mds5_dk":    "Midgar Sector 5",
    "mds5_w":     "Midgar Sector 5",
    "mds5_i":     "Midgar Sector 5",
    "mds5_m":     "Midgar Sector 5",
    "church":     "Midgar Sector 5",
    "chrin_1a":   "Midgar Sector 5",
    "chrin_1b":   "Midgar Sector 5",
    "chrin_2":    "Midgar Sector 5",
    "chrin_3a":   "Midgar Sector 5",
    "chrin_3b":   "Midgar Sector 5",
    "eals_1":     "Midgar Sector 5",
    "ealin_1":    "Midgar Sector 5",
    "ealin_12":   "Midgar Sector 5",
    "ealin_2":    "Midgar Sector 5",
    "min51_1":    "Midgar Sector 5",
    "min51_2":    "Midgar Sector 5",
    # Wall Market (also Sector 5 pass gated)
    "mds6_1":     "Midgar Sector 5",
    "mds6_2":     "Midgar Sector 5",
    "mds6_22":    "Midgar Sector 5",
    "mds6_3":     "Midgar Sector 5",
    "mrkt1":      "Midgar Sector 5",
    "mrkt2":      "Midgar Sector 5",
    "mrkt3":      "Midgar Sector 5",
    "mrkt4":      "Midgar Sector 5",
    "mktpb":      "Midgar Sector 5",
    "mkt_w":      "Midgar Sector 5",
    "mkt_mens":   "Midgar Sector 5",
    "mkt_ia":     "Midgar Sector 5",
    "mktinn":     "Midgar Sector 5",
    "mkt_m":      "Midgar Sector 5",
    "mkt_s1":     "Midgar Sector 5",
    "mkt_s2":     "Midgar Sector 5",
    "mkt_s3":     "Midgar Sector 5",
    "colne_1":    "Midgar Sector 5",
    "colne_2":    "Midgar Sector 5",
    "colne_3":    "Midgar Sector 5",
    "colne_4":    "Midgar Sector 5",
    "colne_5":    "Midgar Sector 5",
    "colne_6":    "Midgar Sector 5",
    "colne_b1":   "Midgar Sector 5",
    "colne_b3":   "Midgar Sector 5",
    "onna_52":    "Midgar Sector 5",

    # --- Eastern continent, foot-reachable (no gate) ---
    "farm":       "Chocobo Farm",
    "convil_1":   "Fort Condor",
    "convil_2":   "Fort Condor",
    "convil_4":   "Fort Condor",

    # --- Western continent (nearest-tier chocobo crossing) ---
    "delmin1":    "Costa del Sol",
    "delmin12":   "Costa del Sol",
    "gonjun1":    "Gongaga",
    # Boss in a Box (v0.0.6): the jungle crossroads carries the Reno & Rude
    # chest, so the field needs a region of its own to hang the check on.
    "gonjun2":    "Gongaga",
    "gninn":      "Gongaga",
    "goson":      "Gongaga",
    "gnmk":       "Gongaga",        # Meltdown Reactor (Titan materia)
    "zz3":        "Chocobo Sage",   # Chocobo Sage's house (Enemy Skill)
    "zz5":        "Mime Cave",      # Mime materia (Green/Black/Gold Chocobo)
    "zz6":        "HP <-> MP Cave", # HP <-> MP materia (Black/Gold Chocobo)
    "zz7":        "Quadra Magic Cave", # Quadra Magic materia (Blue/Black/Gold Chocobo)
    "zz8":        "Knights of the Round Cave", # Knights of the Round (Gold Chocobo)
    "cos_btm":    "Cosmo Canyon",
    "cos_btm2":   "Cosmo Canyon",
    "cosmin6":    "Cosmo Canyon",
    "cosmin7":    "Cosmo Canyon",
    "nivl_3":     "Nibelheim",
    "nvmin1_1":   "Nibelheim",
    "nvmin1_2":   "Nibelheim",
    "nivinn_1":   "Nibelheim",
    "niv_w":      "Nibelheim",
    "niv_ti1":    "Nibelheim",
    "niv_ti2":    "Nibelheim",
    "sinin1_2":   "Nibelheim",
    "sinin2_1":   "Nibelheim",
    "sinin2_2":   "Nibelheim",
    # sininb42 (Destruct) is NOT behind the Basement-Key door — playtest-confirmed
    # 2026-08-05. Gold Saucer's BASEMENT_GATE patch re-gates sininb2, and sininb42
    # sits on the near side of it, so the materia is reachable from the mansion
    # proper. It lives in Nibelheim with the rest of the mansion (sinin1_2 /
    # sinin2_1 / sinin2_2) rather than in the key-gated basement region.
    "sininb42":   "Nibelheim",
    "sininb1":    "Shinra Mansion Basement",   # Vincent's coffin room (Recruit Vincent check)
    "mtnvl2":     "Mt. Nibel",
    "mtnvl3":     "Mt. Nibel",
    "nvdun1":     "Mt. Nibel",
    "nvdun2":     "Mt. Nibel",
    "nvdun3":     "Mt. Nibel",
    "nvdun4":     "Mt. Nibel",
    "rckt":       "Rocket Town",
    "rckt2":      "Rocket Town",
    "rktsid":     "Rocket Town",
    "rktmin2":    "Rocket Town",
    "rkt_i":      "Rocket Town",
    "rkt_w":      "Rocket Town",
    "rcktin4":    "Rocket Town",
    "rcktin6":    "Rocket Town",
    # --- Cave of the Gi: reachable in Free Roam now that Gold Saucer re-opens the
    #     cosin2 door (FieldScriptEditor triangle-activate insert + movie NOPs).
    #     The 6 cave pickups live in gidun_1/2/4; access is gated by Cosmo Canyon
    #     (the player enters the cave from there). gidun_3 has no checks but is on
    #     the path, mapped for completeness.
    "gidun_1":    "Cosmo Canyon",
    "gidun_2":    "Cosmo Canyon",
    "gidun_3":    "Cosmo Canyon",
    "gidun_4":    "Cosmo Canyon",
    "anfrst_1":   "Ancient Forest",
    "anfrst_2":   "Ancient Forest",
    "anfrst_3":   "Ancient Forest",
    "anfrst_5":   "Ancient Forest",

    # --- Wutai (extended-tier chocobo) ---
    "datiao_8":   "Wutai",
    "tower5":     "Wutai",
    "yufy1":      "Wutai",
    "utapb":      "Wutai",
    "hideway1":   "Wutai",   # Wutai Hidden Passage — Magic Shuriken
    "hideway2":   "Wutai",   # Wutai Hidden Passage — Hairpin
    "hideway3":   "Wutai",   # Wutai Hidden Passage — HP Absorb

    # --- Temple of the Ancients (v0.0.6). The old note here claimed the temple
    #     "collapses at ~moment 1000" and was permanently unreachable. That was
    #     WRONG: the temple is a self-contained state machine driven by its own
    #     progress value, not something the global moment destroys. Gold Saucer now
    #     moves that machine onto a private savemap word, restores the entry route
    #     (jtempl -> gateway -> jtmpin1 -> jtmpin2 -> kuro_1) and the client seeds
    #     the machine only once the Keystone has been received — which is what gates
    #     the dungeon in-game. Only the 7 check-bearing maps need mapping; the
    #     connecting rooms (jtempl/jtmpin1/jtmpin2/kuro_4/9/10/11/12) hold no checks.
    "kuro_1":     "Temple of the Ancients",
    "kuro_2":     "Temple of the Ancients",
    "kuro_3":     "Temple of the Ancients",
    "kuro_5":     "Temple of the Ancients",
    "kuro_6":     "Temple of the Ancients",
    "kuro_7":     "Temple of the Ancients",
    "kuro_82":    "Temple of the Ancients",
    # Demons Gate's own field. It stages BATTLE 644 already; the fight no
    # longer fires in Free Roam, so a boss chest gives it back (see
    # kBossChests). Without this line the chest location has no region and
    # would be silently dropped from the pool.
    "kuro_12":    "Temple of the Ancients",

    # --- Northern forests (extended-tier chocobo; Forgotten Capital also Lunar Harp) ---
    "bonevil":    "Bone Village",
    "slfrst_2":   "Sleeping Forest",
    "losin1":     "Forgotten Capital",
    "losin2":     "Forgotten Capital",
    "losin3":     "Forgotten Capital",
    "losinn":     "Forgotten Capital",
    "loslake1":   "Forgotten Capital",
    "sango3":     "Forgotten Capital",
    "sango1":     "Corel Valley",
    "sandun_1":   "Corel Valley",
    "sandun_2":   "Corel Valley",

    # --- Snow / Glacier / Crater (Highwind; deeper areas also Snowboard) ---
    "sninn_2":    "Icicle Inn",
    "sninn_b1":   "Icicle Inn",
    "snmin1":     "Icicle Inn",
    "snmin2":     "Icicle Inn",
    "snmayor":    "Icicle Inn",
    "hyou5_1":    "Great Glacier",
    "hyou2":      "Great Glacier",
    "hyou8_2":    "Great Glacier",
    "hyou5_3":    "Great Glacier",
    "hyou12":     "Great Glacier",
    "hyou13_2":   "Great Glacier",
    "move_d":     "Great Glacier",
    # --- Gaea's Cliff: NOT mapped. Its checks are part of the one-way climb to
    #     the Whirlwind Maze (disc-2 story, ~moment 1100); at Free Roam moment
    #     1997 the gaiin_*/holu_1 fields are dead, so dropping the maps here
    #     drops all of their locations.
    "trnad_1":    "Whirlwind Maze",
    "trnad_2":    "Whirlwind Maze",
    "trnad_3":    "Whirlwind Maze",
    "trnad_4":    "Whirlwind Maze",
    "woa_1":      "Whirlwind Maze",
    # --- Northern Cave (interior, las*): Re-added with Highwind access
    "las0_4":     "Northern Cave",
    "las0_5":     "Northern Cave",
    "las0_6":     "Northern Cave",
    "las0_7":     "Northern Cave",
    "las1_2":     "Northern Cave",
    "las1_3":     "Northern Cave",
    "las2_2":     "Northern Cave",
    "las2_3":     "Northern Cave",
    "las3_1":     "Northern Cave",
    "las3_2":     "Northern Cave",
    "las3_3":     "Northern Cave",
    "las4_0":     "Northern Cave",
    "las4_1":     "Northern Cave",

    # --- Mideel (Highwind) ---
    "itown1b":    "Mideel",
    "itown_w":    "Mideel",
    "itown_i":    "Mideel",
    "itown_m":    "Mideel",
    "itmin2":     "Mideel",

    # --- Underwater Reactor (Submarine; reached via nearest-tier Junon access) ---
    "semkin_6":   "Underwater Reactor",
    "semkin_7":   "Underwater Reactor",
    "subin_1a":   "Red Submarine",    # Red Submarine interior (Huge Materia Underwater)
    "subin_1b":   "Red Submarine",    # Red Submarine interior

    # --- Sunken Gelnika (Submarine only) ---
    # NOTE: the Sunken Gelnika field maps are named qa/qb/qc/qd in flevel.lgp
    # (NOT q_1-q_4). The map name must match the LGP field name so Gold Saucer's
    # (map,item_text) pickup match works — otherwise the chests/materia keep
    # their vanilla item grant (double-dip with the AP check).
    "qa":         "Gelnika",   # was "q_1" (Heaven's Cloud)
    "qb":         "Gelnika",   # was "q_2" (no AP checks)
    "qc":         "Gelnika",   # was "q_3" (Double Cut, Escort Guard, Conformer, Megalixir)
    "qd":         "Gelnika",   # was "q_4" (Hades, Highwind, Outsider, Spirit Lance, Megalixir)
}

# Items that only belong in the pool when Free Roam is enabled (world-map
# traversal unlocks: the Highwind + the layered colour chocobos).
# The four colour chocobos, as a set — replaced in the pool by 4x "Progressive
# Chocobo" when the progressive_chocobos option is on (v0.0.6).
_COLOUR_CHOCOBO_ITEMS = frozenset({
    "Green Chocobo", "Blue Chocobo", "Black Chocobo", "Gold Chocobo",
})

_FREE_ROAM_ONLY_ITEMS = frozenset({
    "Highwind", "Submarine",
    "Green Chocobo", "Blue Chocobo", "Black Chocobo", "Gold Chocobo",
    "Progressive Chocobo",
    # Party members — in linear mode they join via story, so they are only AP
    # items in Free Roam. Vincent and Yuffie are the optional recruits (not
    # goal requirements); their joins are neutered by Gold Saucer so AP grants
    # them.
    "Barret", "Tifa", "Aerith", "Red XIII", "Cait Sith", "Cid", "Vincent",
    "Yuffie",
})

# Optional party members (progression in Free Roam) and how many the goal
# requires — winning needs the Highwind plus a real squad, not just Cloud.
_PARTY_MEMBER_ITEMS = ["Barret", "Tifa", "Aerith", "Red XIII", "Cait Sith", "Cid"]

# The deep-endgame areas — all three Weapons, the sunken Gelnika and the Underwater
# Reactor — are brutal for a low-level solo Cloud, and Archipelago cannot see LEVEL
# (fill reasons about items and events only). Party size is the one strength proxy
# knowable at fill time, so it stands in for "you are equipped to be here". This
# does not stop a player walking in underlevelled; it stops the SEED from ever
# requiring them to.
#
# The POOL is deliberately wider than _PARTY_MEMBER_ITEMS: for raw combat strength
# Vincent and Yuffie count like anyone else, so three of any EIGHT satisfies this.
# The goal gate above stays on the six, because that is about having recruited the
# canonical party, not about being strong enough for a fight.
_ENDGAME_SQUAD_POOL = _PARTY_MEMBER_ITEMS + ["Vincent", "Yuffie"]
_ENDGAME_SQUAD_SIZE = 3
# Goal also requires the 4 Huge Materia (kept progression in Free Roam below).
_GOAL_HUGE_MATERIA = [
    "Huge Materia (Fort Condor)", "Huge Materia (Corel)",
    "Huge Materia (Underwater)", "Huge Materia (Rocket)",
]

# --- Free Roam item reclassification ---------------------------------------
# Most Midgar / Wall Market / Shinra-building key items gate nothing once the
# game starts past Midgar, so in Free Roam they are downgraded. A handful stay
# meaningful. (Linear mode keeps every item's original classification.)
# Still progression in Free Roam (gate a region or future content): the colour
# chocobos (Green/Blue/Black/Gold), Key to Sector 5, Highwind, Lunar Harp,
# Glacier Map, Snowboard, Basement Key, the 6 party members, and the 4 Huge
# Materia (the last two are goal requirements, so they must stay progression).
# Upgraded to "useful" in Free Roam only. Done here rather than by editing the
# item's record in items.json, because that record's `filler` classification is
# load-bearing in three other places that read the RAW value: the padding cycle
# in create_items, the truncation sort key, and get_filler_item_name. This set is
# consulted only by _effective_classification, which is exactly the blast radius
# we want. (One consequence: AP core can still hand out the single Hero Drink as
# plando/item-link filler, because get_filler_item_name reads the raw table.)
_FREE_ROAM_USEFUL_ITEMS = frozenset({
    "Hero Drink",   # full-fight stat boost; genuinely worth finding (v0.0.6)
})
_FREE_ROAM_FILLER_ITEMS = frozenset({
    "Battery",
    "Cotton Dress", "Satin Dress", "Silk Dress",
    "Wig", "Dyed Wig", "Blonde Wig",
    # Keystone is NOT here any more (v0.0.6): it gates the Temple of the Ancients,
    # both in logic and in-game — the client withholds the temple's state seed until
    # it has been received. It keeps its `progression` classification from items.json.
    "Key to Ancients", "Black Materia", "PHS",
    "Keycard 60", "Keycard 62", "Keycard 65", "Keycard 66", "Keycard 68",
    "Midgar Parts 1", "Midgar Parts 2", "Midgar Parts 3",
    "Midgar Parts 4", "Midgar Parts 5",
})
# Items never placed as Archipelago items in Free Roam. (The Submarine is now a
# real AP vehicle — it gates North Corel/Gold Saucer + underwater spots — so it
# is no longer excluded.)
# Gold Ticket: the Gold Saucer tram is open in Free Roam and nothing else
# consumes the item, so it is dead weight in the pool (removed 2026-07-09).
# "Fort Condor Key" gates nothing now that Fort Condor is ungated; without this it
# would still be pooled as progression whenever town_gating is ON.
_FREE_ROAM_EXCLUDE_ITEMS = frozenset({"Gold Ticket", "Fort Condor Key"})

# Locations that cannot be obtained in Free Roam (game moment 1997), so they
# must not receive items or they soft-lock the seed:
#   300062  Chocobo farm - Chocobo Lure — bought via a dialogue scene that the
#           late game state skips, so the pickup flag is never set.
#   300061  Chocobo farm - Kujata — bogus entry (no longer in the dataset).
#   310038  Fort Condor - Super Ball (convil_2) — a Fort Condor minigame reward
#           that the Free Roam state can't reach, so its flag never sets.
#   310014  Kalm - KeyItem: PHS (elminn_1) — the PHS hand-over only runs in the
#           post-flashback script (~moment 100); it never fires at 1997, and
#           the PHS is an AP-sent item in Free Roam anyway.
#   310010, 310020-310035  Wall Market Don Corneo dress-quest chain (Member's
#           Card, Colognes, Pharmacy Coupon, Wigs, Dresses, Disinfectant trio,
#           Tiaras) — disc-1-only events (~moment 300-400); the NPCs/scripts
#           are replaced on the disc-2 Midgar return, so none fire at 1997.
#   200018  Chocobo farm - Choco/Mog (farm) — the "talk to the chocobo" scene
#           that grants the summon doesn't fire at 1997.
#   310071  Nibelheim - Played piano during flashback (niv_ti2) — only set
#           inside the Kalm flashback (~moment 70); never fires at 1997.
#           (Listed once, with the Nibelheim House group further down.)
# (frcyo "Chocobo Ranch" locations are dropped via FREE_ROAM_REGION_MAP, and
#  the whole Temple of the Ancients is dropped the same way — it has collapsed
#  by moment 1997.)
_FREE_ROAM_DEAD_LOCATION_CODES = frozenset({
    300061, 300062, 310038, 310014, 200018,
    # Wall Market dress-quest chain:
    310010, 310020, 310021, 310022, 310023, 310024, 310025, 310026,
    310027, 310028, 310029, 310030, 310031, 310032, 310033, 310034, 310035,
    # Removed by request: Sewer (Midgar, colne_b1) + Whirlwind Maze (trnad_*).
    300031, 300032,                                  # Sewer
    300248, 300249, 300398, 310016, 310074, 310075,  # Whirlwind Maze
    # Removed by request: Sector 7 (mds7 maps + shops)
    200000, 200001, 200002, 200003, 200004, 200005, 200006, 200007,  # Train Graveyard + No. 1 Reactor
    300160, 300161,                                  # Beginner's Hall
    # Sector 7 shops (shop_ids 0/2/9 — all AP slots, per the expanded shops.json).
    # 320004-320007 were here too, as shop 1's slots. Shop 1 is NOT Sector 7's —
    # it is a Mideel storefront (see _DEAD_SHOP_IDS) — so they are live again.
    320000, 320001, 320002, 320003,
    320008, 320009, 320010, 320026, 320027, 320028,
    # Removed by request: Nibelheim House (niv_ti maps). 310071/072 are flashback-only
    # piano flags on the niv_ti2 bank11 0x01 byte (0x0EA5), which is VOLATILE in Free
    # Roam (reused by other field logic). 310070 (Final Heaven) ALSO used that byte and
    # fired randomly — but it is RE-INTRODUCED with a dedicated stable detection bit
    # (locations.json + field_pickup_flags 310070 -> bank13 0x94.5, free + clear at
    # start) and item_text fixed to "Final Heaven" so GS matches/relocates the piano
    # give-STITM there. The player gets it by playing the correct piano tune.
    300179, 300180, 310071, 310072,
    # Removed by request: Turtle Paradise flyers
    310058, 310059, 310060, 310061, 310062, 310063, 310064, 310065,
    # Removed by request: Junon Inn - Potion
    300100,
    # Removed by request: Underwater Reactor key item (semkin_6/7, subin_1a)
    310013,                   # Key to Ancients
    # Removed by request (2026-07-07): Keystone check (clsin2_2, Gold Saucer).
    # The Keystone ITEM stays in the filler pool; only the location is dead.
    310015,                   # Gold Saucer Area - KeyItem: Keystone
    # Removed by request (2026-07-19): Ghost Hotel chest (ghotin_2).
    300068,                   # Ghost Hotel - Elixir
    # Removed by request (2026-07-19): Chocobo Square waiting room — Esther blocks
    # the jockey-room entrance in Free Roam, making it unobtainable.
    300346,                   # Waiting Room - Ramuh (crcin_2)
    # 310012 (Huge Materia Underwater) RE-INTRODUCED: the "Red Submarine" you drive
    # into underwater. Its item_text was aligned to "Huge Materia (Underwater)" so
    # GS's getKeyItemName matches it (was "Huge Materia: UnderWater" -> no AP entry).
    # Removed by request: all Corneo mansion locations (colne_*; Sewer already above)
    300029,                   # Corneo Hall, 2f - Phoenix Down (colne_3)
    300030,                   # Torture Room - Ether (colne_4)
    300297,                   # Corneo Hall, 2f - Hyper (colne_6)
    # Gold Saucer Chocobo Racing — "Rewards From Ester" is a 19-race grind; kept out
    # of the pool. (The chocobo_race_checks option adds First Race + Rank S instead;
    # "30Gp from Mog's House" is a Wonder Square check, not racing, and stays as-is.)
    310069,
    # Removed by request: all min51_2 checks (Flyer #1 already above)
    300163,                   # Sector 5 House 2f - Turbo Ether
    310046,                   # Midgar Sector 5 - Found hidden draw
    310047,                   # Midgar Sector 5 - Stole boys 5 gil
    # Removed by request: all Shinra Building locations (blin*; Flyer #2 already above)
    200135, 300007, 300008, 300011, 300012, 300013, 300014, 300016, 300017,
    300018, 300022, 300281, 300284, 300285, 300286, 300391,
    310002, 310003, 310004, 310005, 310006, 310007, 310008,
    310040, 310041, 310048, 310049, 310050, 310051, 310052, 310053, 310054,
    310055, 310056, 310076, 310077, 310078, 310079,
    # Temple of the Ancients (v0.0.6): the 16 kuro_* field checks are LIVE again —
    # the dungeon is reachable and Keystone-gated, so they are no longer listed
    # here. (Being absent from this set is only half of it; they also need their
    # maps in FREE_ROAM_REGION_MAP, which they now have.)
    #
    # The 2 SHOP slots stay dead: exe shop 45 is labelled "Temple of Ancients" and
    # (Temple of the Ancients shop slots 320105/320106 were excluded here while a
    # static scan could not prove a shop existed inside. It does: kuro_2's `shop`
    # entity runs MENU type=0x08 param=45 — exe shop 45 — and the room is on the
    # normal route. Confirmed in game 2026-08-31, so both slots are live again.)
    # Removed by request: all Cargo Ship locations + shop (shpin_2/shpin_3,
    # shop_id 25). NOTE: this is the Junon->Costa boat, NOT the Gelnika
    # "Cargo Room" (q_4, 200296/310090) which stays in.
    300224, 300225, 300226,                          # Ether, All, Wind Slash
    320065, 320066, 320067, 320068,                  # Cargo Ship Item - AP Slots 1-4
    # Removed by request (2026-06-22):
    200351,                                          # Materia Room - Huge Materia: Rocket
    200371,                                          # Mideel, House 2 - Elixir
    300038, 300040, 300041,                          # Fort Condor Watch Room (all rewards)
    300175,                                          # North Corel - Ultima #2
    300174,                                          # North Corel - Catastrophe
    310017,                                          # Corel - Huge Materia: Corel
    300208,                                          # Rocket Town - Yoshiyuki
    300250,                                          # Under Junon - Shiva
    310067,                                          # Midgar Sector 5 - Lingerie
    200321, 200322,                                  # Mt. Corel - Star Pendant / Wizard Staff
    310036,                                          # Midgar Sector 5 - Batteries
    300177, 300193,                                  # Nibelheim Luck Sources (Inn / House)
    300182,                                          # Nibelheim Item Store - Elixir
    310044,                                          # Nibelheim - Mind Plus
    # 310043 (Nibelheim - Key To Basement) RE-INTRODUCED: Gold Saucer now re-gates
    # the sininb2 basement on the Basement-Key possession bit (Var[1][0x43].4)
    # instead of 0x0C8C.1, and the client no longer sets 0x0C8C.1 — so that flag is
    # free to serve purely as this pickup's detection bit again. (Pairs with the
    # "BASEMENT_GATE" FieldPickup patch + the client gate-flag removal.)
    # Corneo dress key items still live (rest of the chain already excluded above):
    310066,                                          # Midgar Sector 5 - Bikini briefs
    # (Shinra HQ's shop slots used to be four literal codes here. They are keyed
    # on the SHOP ID now — see _DEAD_SHOP_IDS — because the slot grid can generate
    # more of them and listing codes would silently miss the new ones.)
    # Removed by request (2026-07-27, v0.0.5): Speed Square (jetin1) Umbrella prize.
    # The Flayer prize on the same screen (300090, same byte 0x0BD7 at bit 3) STAYS.
    300089,                                          # Gold Saucer Rollercoaster - Umbrella
})

# Fort Condor (non-shop) check locations, dropped only when the player sets the
# disable_fort_condor_checks YAML option. Covers the Watch Room minigame rewards,
# the Phoenix / Super Ball battle rewards, and the Huge Materia given by the old
# man after the final minigame fight. The Fort Condor SHOP slots are NOT here,
# so the store stays in the pool. (Some of these also live in
# _FREE_ROAM_DEAD_LOCATION_CODES already; the overlap is harmless.)
# Shops whose slots are never checks in Free Roam, keyed by SHOP ID rather than
# by location code. This exists because of the slot grid (v0.0.6): the dead-code
# list names individual codes, so generating more slots for an excluded shop
# would smuggle live checks into a shop the player can never reach. Every shop
# here has ALL of its shipped slots in _FREE_ROAM_DEAD_LOCATION_CODES already —
# this just makes the exclusion whole-shop so it covers generated slots too.
_DEAD_SHOP_IDS = frozenset({
    0, 2, 9,      # Sector 7 Weapon / Item / Pillar — sector destroyed
    10,           # Shinra HQ — the whole building is unreachable at moment 1997
    25,           # Cargo Ship Item — removed by request (v0.0.5)
})
# Shop 1 was in that set as "Sector 7 Item", which it is not. A field-script scan
# of every MENU(type=8) shows shop 1 is opened by `itown1b` — MIDEEL's town field —
# and by nothing else in the game; Sector 7's own stores are 0 (mds7_w1),
# 2 (mds7_im) and 9 (mds7plr1). Dead-listing it deleted four AP slots from a
# storefront the player can walk up to, which is the 2026-09-03 report that some
# Mideel stores never populate. Its records now live in the Mideel region.

_FORT_CONDOR_CHECK_CODES = frozenset({
    300038, 300040, 300041,   # Fort Condor Watch Room - Megalixir / Peace Ring / Magic Comb
    310037,                   # Fort Condor - Phoenix
    310038,                   # Fort Condor - Super Ball
    310011,                   # Fort Condor - KeyItem: Huge Materia: Fort Condor (old man)
})

# "Gil dump" checks: gated on spending money rather than on reaching anywhere.
# Dropped by the disable_gil_dump_checks YAML option (v0.0.6).
_GIL_DUMP_CHECK_CODES = frozenset({
    200012,   # Wall Market Weapon Store - Sneak Glove       (mkt_w,   bank 1/37/4)
    200320,   # Cloud's Villa - Purchased The House          (delmin1, bank 13/114/3)
})

# Bone Village excavation rewards, dropped by disable_bone_village_digs (v0.0.6).
# ALL FIVE digs, not just the three loot ones: the Lunar Harp and Key to Sector 5
# are dug up there too, and leaving those behind would mean opting out of the
# minigame while still having to play it for two progression items. Their ITEMS
# stay in the pool and get placed elsewhere, so nothing is lost.
_BONE_VILLAGE_DIG_CODES = frozenset({
    300287,   # Bone Village - Buntline    (bank 13/35/5)
    300289,   # Bone Village - Megalixir   (bank 13/38/2)
    300290,   # Bone Village - Mop         (bank 13/38/1)
    310042,   # Bone Village - KeyItem: Lunar Harp
    310082,   # Bone Village - KeyItem: Key To Sector 5
})

# Gold Saucer chocobo-racing result checks (Free Roam), gated by the
# chocobo_race_checks option. Detected via the persistent race-progression byte
# (client bank 5 / addr 138 = savemap 0xDA4+0x8A): bit2 = first race, bit4 = won
# 9 races (Rank S). Reachable now that Ester (the race manager) is interactable.
# (bit3 "Beat Mog House" is a Wonder Square check, not racing; bit5 "19 races" is
#  left out of the pool as an excessive grind.)
_CHOCOBO_RACE_CHECK_CODES = frozenset({
    # Progression byte, savemap 0xE2E (bank 5 / addr 138):
    310098,                   # Gold Saucer Area - First Chocobo Race    (bit 2)
    310099,                   # Gold Saucer Area - Chocobo Racing Rank S (bit 4)
    # Rank B and A have NO dedicated flag in 0xE2E — it only records the first
    # race, Rank S (9 wins) and the 19-win Sprint Shoes. Their bank/address/bit in
    # locations.json points at the CURRENT RACING CLASS byte, savemap 0xDBB
    # (bank 5 / addr 23), "00: Class C | 01: Class B | 02: Class A | 03: Class S".
    #
    # That entry is RETAINED ONLY so these codes stay in the pool — Locations.py
    # _load_placeable_codes() keeps a location only if it has bank >= 0, so
    # clearing the flag here would delete both checks outright. The client no
    # longer DETECTS on it: 0xDBB is a value, not an achievement bitfield, so
    # reading bit 0 / bit 1 both misses Rank B when the class is exactly A and
    # misfires on any class the player did not earn. The client's
    # _BITON_SCAN_EXCLUDE skips these two codes and the per-chocobo win counter
    # in _chocobo_rank_checks() is authoritative (2026-08-06).
    310101,                   # Gold Saucer Area - Chocobo Racing Rank B (bit 0)
    310102,                   # Gold Saucer Area - Chocobo Racing Rank A (bit 1)
})

# Weapon boss locations (detected by their savemap defeat flag) and the traversal
# tier needed to reach/fight each in Free Roam (see the chocobo tiers in
# _create_free_roam_regions). Reward items obey these gates.
_FREE_ROAM_WEAPON_BOSSES = {
    "Defeat Ultimate Weapon": "highwind",   # chase requires Highwind
    "Defeat Emerald Weapon":  "underwater", # deep underwater — Submarine
    "Defeat Ruby Weapon":     "ruby",       # Highwind + Ultimate Weapon dead
    # Diamond Weapon is omitted: his world-map model never renders in Free Roam, so
    # he is fully hidden (ambient spawn neutralized in wm0.ev) and is not a check.
}

# Weapons that need _ENDGAME_SQUAD_SIZE characters on top of their traversal tier.
# All three: Ultimate was briefly exempt, but a solo Cloud has no business in any
# Weapon fight, and exempting him left the Ultimate CHECK and the "Ultimate Weapon
# Defeated" EVENT disagreeing about what the same kill costs.
_WEAPON_BOSSES_NEEDING_SQUAD = frozenset({
    "Defeat Ultimate Weapon", "Defeat Emerald Weapon", "Defeat Ruby Weapon",
})

# Kalm Traveler (House: 2f, elmin4_2) trades — each check requires its rare-item
# input. The inputs are made progression (items.json) so the fill places them
# reachably. (The in-game Gold Chocobo reward is suppressed via the field patch in
# FieldPickupRandomizer so it stays AP-only.)
_FREE_ROAM_LOCATION_ITEM_GATES = {
    200300: "Guide Book",    # Show Underwater
    200301: "Earth Harp",    # Show Master Command
    200302: "Earth Harp",    # Show Master Magic
    310092: "Earth Harp",    # Show Master Summon
    200304: "Desert Rose",   # Show Gold Chocobo
    310070: "Tifa",          # Nibelheim - Final Heaven
    # Wutai Da-chao Statue (datiao_8) — the cave/statue rewards require the
    # Leviathan Scales key item in-game (the client also sets the field's
    # "has Leviathan Scales" flag on receipt). Leviathan Scales is progression.
    200337: "Leviathan Scales",   # Da-chao Statue - Steal-As-Well
    # 200338 (Dragoon Lance) removed 2026-07-15: confirmed reachable in-game
    # without Leviathan Scales, and datiao_8's field script gates the treasures
    # on their own pickup/event flags (V[f0][0x8d]), not the Leviathan key item.
    # (Steal-As-Well 200337 + Oritsuru 200346 likely share this — pending confirm.)
    200346: "Leviathan Scales",   # Da-chao Statue - Oritsuru
    # Godo's Pagoda (tower5) — the pagoda challenge requires Yuffie in the
    # party in-game, and Yuffie is an AP item in Free Roam.
    200344: "Yuffie",             # Godo's Pagoda - Leviathan
}

# Town gating (town_gating option, Free Roam): each gated town region -> its key
# item. The world-map entry is gated both in the world script (Gold Saucer inserts
# a PUSH_SAVEMAP_BIT key check before ENTER_FIELD) and in logic (region access_rule
# in _create_free_roam_regions). Kalm is never gated (the start town). The keys are
# added to the pool only when the option is on (see create_items).
_TOWN_GATE_KEYS = {
    # Fort Condor is NOT gated (2026-07-31). It sits on the eastern continent,
    # walkable from Kalm with no transport, so it is one of the very few sphere-0
    # regions — gating it removed 7 starting locations at the same moment town
    # gating ADDED 13 progression keys, which is what made seeds unfillable
    # ("No more spots to place 1 items", multiple testers). Its key item is kept
    # out of the pool entirely (see _TOWN_KEY_ITEMS + _FREE_ROAM_EXCLUDE_ITEMS),
    # and Gold Saucer's patchTownGates towns[] no longer seals condor1 to match.
    "Junon Lower":  "Junon Key",
    "Junon Upper":  "Junon Key",
    "Corel":        "North Corel Key",
    "Mt. Corel (Costa)": "North Corel Key",
    "Mt. Corel (North)": "North Corel Key",   # the mountain path is Corel's back door
    # The Gold Saucer has no world-map entrance of its own — the only way in is
    # North Corel's ropeway station, so it inherits Corel's key.
    "Gold Saucer Area": "North Corel Key",
    "Cosmo Canyon": "Cosmo Canyon Key",
    "Nibelheim":    "Nibelheim Key",
    "Rocket Town":  "Rocket Town Key",
    "Wutai":        "Wutai Key",
    "Icicle Inn":   "Icicle Inn Key",
    "Mideel":       "Mideel Key",
    "Gongaga":      "Gongaga Key",
    "Bone Village": "Bone Village Key",
    "Costa del Sol": "Costa del Sol Key",
    # The Sleeping Forest (and everything past it) is Bone Village's back yard:
    # its only world entrances are the Corral Valley strip, which Gold Saucer
    # seals on the same key, so the whole northern chain flows through Bone
    # Village. (Forgotten Capital / Corel Valley additionally need the Lunar
    # Harp — their entrance rules AND the key in directly, see
    # _create_free_roam_regions.)
    "Sleeping Forest": "Bone Village Key",
}
# Fort Condor Key is listed explicitly: it is no longer in _TOWN_GATE_KEYS, so
# without this the "town keys only when town_gating is enabled" filter would stop
# matching it and it would enter the pool ALWAYS, as progression gating nothing.
# (_FREE_ROAM_EXCLUDE_ITEMS covers the remaining case, Free Roam WITH gating on.)
_TOWN_KEY_ITEMS = set(_TOWN_GATE_KEYS.values()) | {"Fort Condor Key"}


def _ff7_client_start(*args: str) -> None:
    """Picklable entry point for multiprocessing.Process."""
    import asyncio
    import argparse
    from worlds.ff7.FF7Client import main as ff7_client_main

    parser = argparse.ArgumentParser(description="Final Fantasy VII Archipelago Client")
    parser.add_argument("connect",  nargs="?", default=None)
    parser.add_argument("password", nargs="?", default=None)
    parser.add_argument("--name",   default=None)
    parsed = parser.parse_args(list(args))
    asyncio.run(ff7_client_main(parsed))


def run_client(*args: str) -> None:
    """Launch the FF7 client through the Archipelago launcher."""
    launch(_ff7_client_start, name="Final Fantasy VII Client", args=args)


auto_component = Component(
    "Final Fantasy VII Client",
    func=run_client,
    component_type=Type.CLIENT,
)
components.append(auto_component)


class FF7Settings(settings.Group):
    """Host-side configuration for FF7 IronMog."""
    pass


class FF7Web(WebWorld):
    """Web configuration for FF7 IronMog."""

    tutorials = [
        Tutorial(
            "Final Fantasy VII IronMog Setup",
            "How to configure FF7 with IronMog and Archipelago.",
            "English",
            "multiworld_en.md",
            "multiworld/en",
            ["FF7 Archipelago"],
        )
    ]

    # EVERY option must appear in exactly one group here. An option left out is
    # not dropped — get_option_groups() sweeps the leftovers into a synthetic
    # "Game Options" group which the YAML renderer emits FIRST, so the option ends
    # up at the top of the template, far from the ones it relates to. That is how
    # shop_slots_per_shop came to be reported missing from generated templates
    # (2026-09-02) when it was really 113 lines above randomize_shops. The earlier
    # note on ChocoboRaceChecks below is the same bug in its other form.
    option_groups = [
        OptionGroup(
            "Randomizers",
            [
                RandomizeFieldItems,
                FieldItemsMode,
                FieldItemsKeepType,
                RandomizeShops,
                # Directly under RandomizeShops: meaningless without it.
                ShopSlotsPerShop,
                ProgressiveChocobos,
                RandomizeStartingEquipment,
                StartingEquipmentTier,
            ],
        ),
        OptionGroup(
            "World",
            [
                FreeRoam,
                DisableGoldSaucer,
                # Directly under DisableGoldSaucer: that option removes these
                # checks (Chocobo Square is inside the Gold Saucer), so the two
                # need to be read together. Was missing from the groups entirely,
                # so it never appeared in the WebHost Options Creator.
                ChocoboRaceChecks,
                DisableFortCondorChecks,
                DisableGilDumpChecks,
                DisableBoneVillageDigs,
                WeaponFightChecks,
                TownGating,
            ],
        ),
        OptionGroup(
            "Gameplay",
            [
                ExpMultiplier,
                GilMultiplier,
                APMultiplier,
                PartyLevelSync,
            ],
        ),
        OptionGroup(
            "Goal",
            [
                VictoryCondition,
            ],
        ),
        OptionGroup(
            "Traps",
            [
                TrapFillPercent,
                FrogTrapWeight,
                ConfusionTrapWeight,
                FrozenTrapWeight,
                SlownessTrapWeight,
                SlowTrapWeight,
                InstantDeathTrapWeight,
                DoubleDamageWeight,
                PoisonTrapWeight,
                TinyTrapWeight,
                InstantCrystalTrapWeight,
                SleepTrapWeight,
                ManaDrainTrapWeight,
                MarketCrashTrapWeight,
                DepressionTrapWeight,
                CurseTrapWeight,
                BombTrapWeight,
                TrapLink,
                DeathLink,
            ],
        ),
    ]


@dataclass
class RegionCache:
    name: str
    region: Region


class FF7World(World):
    """FF7 IronMog world — Archipelago controls item/location placement."""

    game: ClassVar[str] = "Final Fantasy VII"
    options_dataclass = FF7Options
    options: FF7Options
    settings: ClassVar[FF7Settings]
    settings_key = "ff7_options"
    topology_present = True

    item_name_to_id: ClassVar[dict[str, int]] = item_name_to_id
    location_name_to_id: ClassVar[dict[str, int]] = location_name_to_id
    item_name_groups: ClassVar[dict[str, set[str]]] = item_name_groups
    location_name_groups: ClassVar[dict[str, set[str]]] = location_name_groups
    required_client_version: ClassVar[tuple[int, int, int]] = (0, 6, 7)
    web: ClassVar[FF7Web] = FF7Web()
    # NO `tracker_world` HERE, DELIBERATELY. Declaring it tells Universal Tracker
    # this world ships a tracker PACK, after which UT's load_pack() unconditionally
    # reads map pages out of the apworld:
    #     load_json(PACK_NAME, f"/{tracker_world.map_page_folder}/{map_page}")
    # We ship no map pack, so map_page_folder was empty, the resource became "//",
    # and pkgutil.get_data resolved it to the package DIRECTORY ("ff7\\"), which
    # zipimport cannot read:
    #     OSError: [Errno 0] : 'ff7\'
    # That is raised while UT processes the Connected packet, so every FF7 player
    # got "Failed to connect to the multiworld server" and UT was unusable.
    # Reported 2026-09-02; the hook had been added for map auto-tabbing without the
    # pack it requires.
    #
    # The client still publishes the current map to data storage under
    # "Slot:{player}:Current Map" (FF7Client._publish_tracker_map). That is the half
    # that works and costs nothing; re-add `tracker_world` only together with a real
    # map pack.

    victory_location_name = "Northern Crater - Defeat Sephiroth"
    victory_item_name = "FF7 Victory"

    _ff7_option_names: ClassVar[tuple[str, ...]] | None = None
    _locations_validated: ClassVar[bool] = False

    def __init__(self, multiworld: MultiWorld, player: int) -> None:
        super().__init__(multiworld, player)
        self._regions: dict[str, Region] = {}

    def generate_early(self) -> None:
        # Fail loud on location-dataset over-subscriptions: two+ records sharing
        # a (map, item_text) beyond the real pickup count collide on one field
        # pickup, producing dead checks. Run once per generation (cached).
        if not FF7World._locations_validated:
            from .validate_locations import validate
            errors, _ = validate()
            if errors:
                raise Exception(
                    f"FF7 location dataset has {len(errors)} over-subscribed "
                    f"(map, item_text) group(s) — colliding/dead checks:\n  "
                    + "\n  ".join(errors)
                )
            FF7World._locations_validated = True


    def create_regions(self) -> None:
        if self.options.free_roam:
            self._create_free_roam_regions()
        else:
            self._create_linear_regions()

    def _create_linear_regions(self) -> None:
        multiworld = self.multiworld
        menu = Region("Menu", self.player, multiworld)
        multiworld.regions.append(menu)

        world_region = Region("Gaia", self.player, multiworld)
        multiworld.regions.append(world_region)
        self._regions[world_region.name] = world_region
        menu.connect(world_region)

        # Linear mode is a full-game randomizer: every location goes into the
        # single world region. (It must NOT filter on FREE_ROAM_REGION_MAP —
        # that map is only for assigning Free Roam sub-regions, and excluding it
        # here would drop most of the game from linear seeds.)
        for location_data in ALL_LOCATION_TABLE.values():
            if location_data.name == self.victory_location_name:
                continue
            if location_data.code not in PLACEABLE_LOCATION_CODES:
                continue  # not a real field pickup -> Gold Saucer can't place/track it
            ff7_location = FF7Location(
                self.player,
                location_data.name,
                location_data.code,
                world_region,
            )
            world_region.locations.append(ff7_location)

        # Shop-slot AP locations (linear: all in the single world region).
        # Only exist when shop randomization is on — see the note in
        # _create_free_roam_regions: Gold Saucer never injects the AP token slots
        # with the feature off, so these checks are unobtainable in-game.
        if self.options.randomize_shops:
            for shop_data in self._active_shop_slots():
                shop_loc = FF7Location(
                    self.player, shop_data.name, shop_data.code, world_region,
                )
                world_region.locations.append(shop_loc)

        victory_loc = FF7Location(self.player, self.victory_location_name, None, world_region)
        victory_loc.place_locked_item(
            Item(self.victory_item_name, ItemClassification.progression, None, self.player)
        )
        world_region.locations.append(victory_loc)

    def _create_free_roam_regions(self) -> None:
        multiworld = self.multiworld
        player = self.player

        menu = Region("Menu", player, multiworld)
        multiworld.regions.append(menu)

        world_map = Region("World Map", player, multiworld)
        multiworld.regions.append(world_map)
        self._regions[world_map.name] = world_map
        menu.connect(world_map)

        sub_region_names = [
            "Kalm",
            "Mythril Mines",
            "Chocobo Farm",
            "Chocobo Sage",
            "Fort Condor",
            "Junon Lower",
            "Junon Upper",
            "Costa del Sol",
            "Corel",
            "Mt. Corel (Costa)",
            "Mt. Corel (North)",
            "Gold Saucer Area",
            "Gongaga",
            "Cosmo Canyon",
            "Nibelheim",
            "Midgar Outskirts",
            "Shinra Mansion Basement",
            "Mt. Nibel",
            "Rocket Town",
            "Ancient Forest",
            "Temple of the Ancients",
            "Wutai",
            "Bone Village",
            "Sleeping Forest",
            "Forgotten Capital",
            "Corel Valley",
            "Icicle Inn",
            "Great Glacier",
            "Whirlwind Maze",
            "Northern Cave",
            "Mideel",
            "Underwater Reactor",
            "Red Submarine",
            "Gelnika",
            "Midgar Sector 5",
            "Mime Cave",
            "HP <-> MP Cave",
            "Quadra Magic Cave",
            "Knights of the Round Cave",
        ]
        sub_regions: dict[str, Region] = {}
        for name in sub_region_names:
            r = Region(name, player, multiworld)
            multiworld.regions.append(r)
            self._regions[name] = r
            sub_regions[name] = r

        # Traversal gate helpers.
        def _has(item):
            return lambda state: state.has(item, player)

        # World-map traversal (Free Roam). Boats + the Tiny Bronco are gone, so
        # the Submarine reaches North Corel + the Gold Saucer (and underwater spots)
        # but can't land you on the other continents.
        # Chocobo access:
        # - Green: Junon mountain only
        # - Blue: Open ocean only (not Junon)
        # - Black: Junon mountain + open ocean
        # - Gold: Junon mountain + open ocean
        # - Highwind: Junon mountain + open ocean
        # Capability helpers — ONE definition each, so every chocobo rule below
        # composes these instead of spelling out its own OR over colour names.
        # That refactor (v0.0.6) is what makes progressive_chocobos possible: with
        # the option on, the four colours are replaced in the pool by four copies
        # of a single "Progressive Chocobo", and owning N copies means owning
        # every colour up to tier N.
        #
        # The ladder is Yellow -> Green -> Black -> Gold, the order a player
        # actually breeds them. Blue is NOT on it: Blue and Green are siblings
        # rather than steps (water vs mountains), so a four-rung ladder has to
        # pick one. Two consequences the rules below depend on:
        #   * tier 1 (Yellow) unlocks NO traversal. It is still progression,
        #     because tiers 2-4 cannot be reached without it.
        #   * OCEAN access first arrives with BLACK at tier 3, not tier 2. So
        #     `_blue` — the ocean capability — is tiered at 3 alongside Black,
        #     which is simply when the player can first cross water.
        # Tiers remain cumulative in CAPABILITY, not in animal.
        _prog = bool(self.options.progressive_chocobos)

        def _choco(colour: str, tier: int):
            if _prog:
                return lambda state, t=tier: state.has("Progressive Chocobo", player, t)
            return lambda state, c=colour: state.has(c, player)

        # tier 1 = Yellow, which grants no traversal and so appears in no rule.
        _green = _choco("Green Chocobo", 2)   # Junon mountain only
        _blue  = _choco("Blue Chocobo",  3)   # ocean; first available on Black
        _black = _choco("Black Chocobo", 3)   # mountain + ocean
        _gold  = _choco("Gold Chocobo",  4)   # all terrain
        _highwind = _has("Highwind")

        def _squad(state):
            """At least _ENDGAME_SQUAD_SIZE of the eight recruitable characters.

            Counted rather than named, so any three will do — Vincent and Yuffie
            included, because this gate is about combat strength and they fight as
            well as anyone. Distinct from the goal gate, which wants the canonical
            six by name.
            """
            return sum(state.has(c, player)
                       for c in _ENDGAME_SQUAD_POOL) >= _ENDGAME_SQUAD_SIZE

        def _mountain(state):    # Junon (mountain crossing)
            return (_green(state) or _black(state) or _gold(state)
                    or _highwind(state))

        def _ocean(state):       # open-ocean continents
            return (_blue(state) or _black(state) or _gold(state)
                    or _highwind(state))

        # The Submarine is PARKED AT THE JUNON DOCK (client _VEHICLE_FIXED_POS[13] =
        # 169884/-240/149694), so owning the item is worthless until you can physically
        # walk to Junon — and Junon is behind the mountain crossing. Holding the sub
        # usable on its own let fill strand a seed: put every mountain-capable item
        # (Green/Black/Gold chocobo, Highwind) behind the sub or the ocean and the
        # player is dead on the starting continent holding a sub they can never board.
        # Shipped that way in v0.0.5 — seed 81032245788812016663 (2026-08-04).
        # If the sub is ever re-parked somewhere foot-reachable, drop this AND.
        def _has_sub(state):
            return state.has("Submarine", player) and _mountain(state)

        def _sub(state):         # North Corel + Gold Saucer (Submarine), or full ocean
            return (_has_sub(state) or _blue(state) or _black(state)
                    or _gold(state) or _highwind(state))

        def _underwater(state):  # underwater only (Submarine)
            return _has_sub(state)

        # --- Eastern continent, foot-reachable (no gate) ---
        world_map.connect(sub_regions["Kalm"])
        world_map.connect(sub_regions["Mythril Mines"])
        world_map.connect(sub_regions["Chocobo Farm"])
        _ent_fort_condor = world_map.connect(sub_regions["Fort Condor"])

        # --- Junon (mountain crossing) ---
        _ent_junon_lower = world_map.connect(sub_regions["Junon Lower"])
        _ent_junon_lower.access_rule = _mountain
        _ent_junon_upper = world_map.connect(sub_regions["Junon Upper"])
        _ent_junon_upper.access_rule = _mountain

        # Town gating: collect each gated town's world-map entrance as it is
        # created; the keys are AND-ed into their access_rule once all entrances
        # exist (after the ocean-continent towns are connected, below).
        _town_entrances = {
            "Fort Condor": _ent_fort_condor,
            "Junon Lower": _ent_junon_lower,
            "Junon Upper": _ent_junon_upper,
        }

        # --- Chocobo Sage's house (northern continent, mountain-enclosed) ---
        # Needs BOTH ocean-crossing AND mountain capability: Black/Gold chocobo or
        # the Highwind. (NOT _mountain — that allows Green, which can't cross the
        # ocean to reach this continent; NOT _ocean — that allows Blue, which is
        # ocean-only and can't enter the mountain-walled area.)
        world_map.connect(sub_regions["Chocobo Sage"]).access_rule = (
            lambda state: _black(state) or _gold(state) or _highwind(state)
        )

        # --- Materia Caves (chocobo-specific terrain requirements) ---
        # Mime Cave (zz5, Wutai continent): Black or Gold Chocobo reach it on
        # their own; a Green Chocobo climbs the mountains but needs the Highwind
        # to be ferried to the continent first. Highwind alone can't land there.
        world_map.connect(sub_regions["Mime Cave"]).access_rule = (
            lambda state: (_black(state) or _gold(state)
                           or (_green(state) and _highwind(state)))
        )
        # HP <-> MP Cave (zz6): Black/Gold Chocobo (mountain + ocean)
        world_map.connect(sub_regions["HP <-> MP Cave"]).access_rule = (
            lambda state: _black(state) or _gold(state)
        )
        # Quadra Magic Cave (zz7): Blue/Black/Gold Chocobo only — Highwind can't
        # land here, so this must NOT use _ocean (which includes Highwind).
        world_map.connect(sub_regions["Quadra Magic Cave"]).access_rule = (
            lambda state: _blue(state) or _black(state) or _gold(state)
        )
        # Knights of the Round Cave (zz8): Gold Chocobo only (all terrain)
        world_map.connect(sub_regions["Knights of the Round Cave"]).access_rule = _gold

        # --- North Corel + Gold Saucer (Submarine reaches these; or Gold/Highwind) ---
        _town_entrances["Corel"] = world_map.connect(sub_regions["Corel"])
        _town_entrances["Corel"].access_rule = _sub
        # Gold Ticket removed from the Free Roam pool (2026-07-09) — the tram
        # is open in-game, so the area is gated on transport alone... plus the
        # North Corel Key under town gating: the park has NO world-map entrance
        # of its own (no field.tbl entry), it is only ever entered through North
        # Corel's ropeway station (ropest, a "Corel" field), so Gold Saucer sits
        # behind the North Corel town seal in-game. Registering the entrance here
        # lets the town-gating loop below AND the key in, keeping logic in step
        # with what the player can actually reach.
        _town_entrances["Gold Saucer Area"] = world_map.connect(sub_regions["Gold Saucer Area"])
        _town_entrances["Gold Saucer Area"].access_rule = _sub

        # --- Open-ocean continents (Blue/Black/Gold Chocobo or Highwind) ---
        for _name in ("Costa del Sol", "Gongaga", "Cosmo Canyon", "Nibelheim",
                      "Rocket Town", "Wutai", "Bone Village",
                      "Mideel"):
            _e = world_map.connect(sub_regions[_name])
            _e.access_rule = _ocean
            _town_entrances[_name] = _e

        # --- Sleeping Forest: ocean traversal (+ its town key when gating) ---
        # The Lunar Harp requirement added here in v0.0.6 was DROPPED by request
        # (2026-09-05). Nothing in game enforces it: neither slfrst_1 nor
        # slfrst_2 tests the Harp flag at all — both gate purely on game_moment
        # (slfrst_1 on < 638, slfrst_2 on < 652 / >= 652), and at Free Roam's 1997
        # none of that blocks entry. A logic rule stricter than the game only
        # makes AP treat the forest's Kujata check (300234) as later than it is.
        #
        # Forgotten Capital and Corel Valley KEEP the Harp: their world entries
        # (field.tbl 26/57/58) are already sealed by patchTownGates, so that gate
        # is enforceable there in a way the forest's is not.
        #
        # Registered in _town_entrances BEFORE the town-gating loop below so that
        # loop ANDs in _TOWN_GATE_KEYS["Sleeping Forest"] = "Bone Village Key" for
        # us. Do NOT hand-write the key the way Forgotten Capital does — that
        # region is connected after the loop and has no choice.
        _e = world_map.connect(sub_regions["Sleeping Forest"])
        _e.access_rule = _ocean
        _town_entrances["Sleeping Forest"] = _e

        _town_entrances["Mt. Corel (Costa)"] = world_map.connect(sub_regions["Mt. Corel (Costa)"])
        _town_entrances["Mt. Corel (Costa)"].access_rule = _ocean
        _town_entrances["Mt. Corel (North)"] = world_map.connect(sub_regions["Mt. Corel (North)"])
        _town_entrances["Mt. Corel (North)"].access_rule = _sub

        def _can_reach_icicle(state):
            return (
                _black(state)
                or _gold(state)
                or _highwind(state)
                or (
                    _blue(state)
                    and state.has("Lunar Harp", player)
                    and (not bool(self.options.town_gating)
                         or state.has("Bone Village Key", player))
                )
            )

        _icicle_inn = world_map.connect(sub_regions["Icicle Inn"])
        _icicle_inn.access_rule = _can_reach_icicle
        _town_entrances["Icicle Inn"] = _icicle_inn

        # --- Mt. Nibel: ocean traversal, and NOTHING else. ---
        # It carried the Nibelheim Key under town gating on the assumption that the
        # mountain is entered through the town. It is not — mtnvl2 and mtnvl4 have
        # their own world-map entries, so the key gated a route the player never
        # takes. Dropped by request 2026-09-05.
        #
        # Gold Saucer's patchTownGates rows for tbl#44/#46 were removed in the same
        # change. Dropping only this side would have left the WORLD SCRIPT still
        # sealing both entries on the Nibelheim key while logic believed they were
        # open — logic looser than the game, which is the direction that strands a
        # seed.
        _mt_nibel = world_map.connect(sub_regions["Mt. Nibel"])
        _mt_nibel.access_rule = _ocean

        # --- Town gating: AND each gated town's key into its entrance rule ---
        # (runs here so every gated town's base traversal rule is already set).
        if bool(self.options.town_gating):
            for _region_name, _key in _TOWN_GATE_KEYS.items():
                _ent = _town_entrances.get(_region_name)
                if _ent is None:
                    continue
                _prev = _ent.access_rule  # default (always True) or the traversal gate
                _ent.access_rule = (lambda state, p=_prev, k=_key:
                                    p(state) and state.has(k, player))
        # Ancient Forest sits on a mountain-walled plateau (western continent); its
        # entrance only fires on foot/chocobo. Two routes: a Black/Gold chocobo
        # crosses the terrain to it, or Ultimate Weapon dies — his death advances
        # the overworld to world_progress 4, which opens a walkable foot path in
        # (post-2026-06-18 client behaviour: _resolve_ultimate_weapon sets the
        # post-Ultimate state). A Green chocobo ALONE can't cross the ocean here,
        # so it is intentionally not a standalone route. (No Blue.)
        #
        # v0.0.6: the Highwind half now REQUIRES the Ultimate event instead of
        # just possession. The comment here already said Highwind was only valid
        # because it implies Ultimate dies, but the rule accepted bare Highwind —
        # so the forest was in logic the moment the airship arrived, with Ultimate
        # still alive and no chocobo (reported 2026-08-31). The old comment also
        # claimed the Highwind could ferry a Green chocobo up; it cannot carry
        # chocobos at all, so that route never existed. With the event's own rule
        # left at _has("Highwind") this is behaviourally identical to before —
        # deliberately. The point is that the dependency is now STRUCTURAL: if the
        # event is ever tightened, this tightens with it instead of quietly
        # continuing to disagree with its own comment.
        world_map.connect(sub_regions["Ancient Forest"]).access_rule = (
            lambda state: (_black(state) or _gold(state)
                    or (_highwind(state)
                        and state.has("Ultimate Weapon Defeated", player)))
        )
        # Temple of the Ancients (v0.0.6): its own island, so ocean traversal, plus
        # the Keystone. The Keystone requirement is REAL, not just logical — nothing
        # in the temple's scripts checks for it (vanilla relies on the story), so the
        # client only starts the temple's state machine once the Keystone has been
        # received. Without it the altar is inert and the interior is unreachable.
        world_map.connect(sub_regions["Temple of the Ancients"]).access_rule = (
            lambda state: _ocean(state) and state.has("Keystone", player)
        )
        # Shinra Mansion basement: ocean + Basement Key. The mansion sits INSIDE
        # Nibelheim (no world-map entrance of its own), so under town gating it
        # also needs the Nibelheim Key — otherwise Destruct / Recruit Vincent show
        # in-logic with only the Basement Key.
        _tg_mansion = bool(self.options.town_gating)
        world_map.connect(sub_regions["Shinra Mansion Basement"]).access_rule = (
            lambda state: (_ocean(state) and state.has("Basement Key", player)
                           and (not _tg_mansion or state.has("Nibelheim Key", player)))
        )
        # Northern forests past Sleeping Forest need the Lunar Harp — and, with
        # town gating, the Bone Village Key: their only world entrances (the
        # Corral Valley strip, tbl#26/#57/#58) are sealed by Gold Saucer on the
        # Bone Village key bit, so the whole area is reached through Bone
        # Village -> Sleeping Forest.
        _tg = bool(self.options.town_gating)
        world_map.connect(sub_regions["Forgotten Capital"]).access_rule = (
            lambda state: _ocean(state) and state.has("Lunar Harp", player)
            and (not _tg or state.has("Bone Village Key", player))
        )
        world_map.connect(sub_regions["Corel Valley"]).access_rule = (
            lambda state: _ocean(state) and state.has("Lunar Harp", player)
            and (not _tg or state.has("Bone Village Key", player))
        )
        # Great Glacier does NOT share Icicle Inn's route, despite sitting behind
        # it. The Highwind can land at Icicle Inn but CANNOT reach the glacier on
        # its own, and a Green chocobo cannot cross the water to get there — so the
        # ways in are Black, Gold, or the Highwind FERRYING a Green, exactly the
        # Mime Cave pattern. Reported from play 2026-09-05.
        #
        # This was _can_reach_icicle, which accepts a bare Highwind (and Blue +
        # Lunar Harp). That is logic LOOSER than the game: fill could place
        # progression on the glacier's checks behind a Highwind the player cannot
        # actually get there with — the unbeatable-seed direction. Tightening it
        # can only delay items, never strand them.
        def _can_reach_glacier(state):
            return (_black(state) or _gold(state)
                    or (_highwind(state) and _green(state)))

        # NO Icicle Inn Key, even under town gating (removed by request
        # 2026-09-05). The key was here because the glacier sits behind the inn,
        # but the route above does not go through the town — and Gold Saucer does
        # not seal the glacier's own fields either: patchTownGates covers the
        # `snow` entries, and its source says outright that the `hyou` fields are
        # "gated in logic by Snowboard+Glacier Map, not here". So the key gated
        # nothing the player could feel, in either direction.
        world_map.connect(sub_regions["Great Glacier"]).access_rule = (
            lambda state: _can_reach_glacier(state)
            and state.has("Snowboard", player)
            and state.has("Glacier Map", player)
        )

        # --- Northern Crater interior: Highwind + All Characters + 4 Huge Materia ---
        world_map.connect(sub_regions["Whirlwind Maze"]).access_rule = _has("Highwind")
        world_map.connect(sub_regions["Northern Cave"]).access_rule = (
            lambda state: (
                state.has("Highwind", player)
                and state.has_all(_PARTY_MEMBER_ITEMS, player)
                and state.has_all(_GOAL_HUGE_MATERIA, player)
            )
        )

        # --- Underwater (Submarine): Underwater Reactor + sunken Gelnika ---
        # Underwater Reactor is entered on foot from Junon, so it needs the mountain
        # crossing — plus the Junon Key ONLY when town gating is on. The key was
        # required unconditionally, but it is not added to the pool with gating off
        # (the default), so the region and its 3 checks were permanently unreachable
        # in every default seed. Caught by test_default_all_state_can_reach_everything.
        _tg_junon = bool(self.options.town_gating)
        world_map.connect(sub_regions["Underwater Reactor"]).access_rule = (
            lambda state: (_mountain(state)
                           and (not _tg_junon or state.has("Junon Key", player))
                           and _squad(state))
        )
        # Red Submarine requires Submarine item to drive underwater
        world_map.connect(sub_regions["Red Submarine"]).access_rule = _underwater
        world_map.connect(sub_regions["Gelnika"]).access_rule = (
            lambda state: _underwater(state) and _squad(state))

        # --- Midgar return (Key to Sector 5) ---
        world_map.connect(sub_regions["Midgar Sector 5"]).access_rule = _has("Key to Sector 5")
        # The outskirts are OUTSIDE that gate: eastern continent, walkable from
        # Kalm, no rule at all. Only the Motor Ball chest lives here.
        world_map.connect(sub_regions["Midgar Outskirts"])

        # Resolve weapon-boss traversal tiers to predicates (used below).
        #
        # What "Ultimate Weapon Defeated" does and does NOT mean (v0.0.6):
        # it marks the point at which Ultimate becomes FIGHTABLE, not the kill.
        # Archipelago's fill only ever sees items and events, so no rule here can
        # require that a battle was actually won — the event's own rule is
        # _has("Highwind"), identical to Ultimate's, which makes _ruby equivalent
        # to plain Highwind. That is the honest ceiling for logic, and it is
        # correct: with the Highwind you can go and kill Ultimate, after which
        # Ruby spawns. The ORDERING is enforced where it can be — at runtime, by
        # the client: _resolve_weapon_battles refuses to latch Ruby's kill bit
        # unless Ultimate's is already set. Previously the docstring on
        # WeaponFightChecks promised sequencing this could never deliver.
        def _ruby(state):
            return (state.has("Highwind", player)
                    and state.has("Ultimate Weapon Defeated", player))

        _tier_rules = {"mountain": _mountain, "ocean": _ocean, "sub": _sub,
                       "underwater": _underwater, "highwind": _has("Highwind"),
                       "ruby": _ruby}

        # The event that grants "Ultimate Weapon Defeated". Created UNCONDITIONALLY
        # (it used to live inside the weapon_fight_checks block) because the
        # Ancient Forest rule references it too — with it inside the option, that
        # region's rule silently changed shape when the option was toggled off.
        # It costs nothing when unused: code=None plus a locked item, so
        # create_items' available-location count skips it (loc.item is not None)
        # and json_export skips it (address is None).
        uw_event = FF7Location(player, "Ultimate Weapon Defeated", None, world_map)
        # Same price as the Ultimate CHECK above. The event stands for "Ultimate is
        # dead", and Ruby's rule and the Ancient Forest both read it — so if the
        # kill needs a squad, so does the event, or logic would believe a solo
        # Cloud could produce it.
        uw_event.access_rule = (
            lambda state: _tier_rules["highwind"](state) and _squad(state))
        uw_event.place_locked_item(
            Item("Ultimate Weapon Defeated", ItemClassification.progression, None, player)
        )
        world_map.locations.append(uw_event)

        # Optionally drop every Gold Saucer check (and its shop slots) from the
        # pool — all those locations resolve to the "Gold Saucer Area" region.
        disable_gold_saucer = bool(self.options.disable_gold_saucer)
        ignore_fort_condor = bool(self.options.disable_fort_condor_checks)
        ignore_gil_dumps = bool(self.options.disable_gil_dump_checks)
        ignore_bone_digs = bool(self.options.disable_bone_village_digs)
        race_checks = bool(self.options.chocobo_race_checks)

        # Chocobo Square is inside the Gold Saucer, so disable_gold_saucer removes
        # the racing checks via the region filter below even when the player has
        # explicitly opted into them. That combination is almost always a mistake in
        # the YAML rather than an intent, and it fails SILENTLY — the locations just
        # never appear. Say so at generation time.
        if race_checks and disable_gold_saucer:
            logging.warning(
                "FF7 [player %d]: chocobo_race_checks is enabled but "
                "disable_gold_saucer is also enabled — the racing checks are inside "
                "the Gold Saucer, so they will NOT be included. Set "
                "disable_gold_saucer to false to use them.",
                self.player,
            )

        for location_data in ALL_LOCATION_TABLE.values():
            if location_data.name == self.victory_location_name:
                continue
            if location_data.code in _FREE_ROAM_DEAD_LOCATION_CODES:
                continue  # unobtainable at game moment 1997 — would soft-lock
            if ignore_fort_condor and location_data.code in _FORT_CONDOR_CHECK_CODES:
                continue  # YAML opt-out of the Fort Condor minigame checks (shop kept)
            if ignore_gil_dumps and location_data.code in _GIL_DUMP_CHECK_CODES:
                continue  # YAML opt-out of the buy-it-with-gil checks
            if ignore_bone_digs and location_data.code in _BONE_VILLAGE_DIG_CODES:
                continue  # YAML opt-out of the Bone Village excavation minigame
            if not race_checks and location_data.code in _CHOCOBO_RACE_CHECK_CODES:
                continue  # chocobo-racing checks are opt-in (grindy minigame)
            if location_data.code not in PLACEABLE_LOCATION_CODES:
                continue  # not a real field pickup -> Gold Saucer can't place/track it
            region_name = FREE_ROAM_REGION_MAP.get(location_data.map)
            if region_name is None:
                continue
            if disable_gold_saucer and region_name == "Gold Saucer Area":
                continue
            target_region = sub_regions[region_name]
            ff7_location = FF7Location(
                player,
                location_data.name,
                location_data.code,
                target_region,
            )
            # Kalm Traveler (House: 2f) trades require their rare-item input.
            gate_item = _FREE_ROAM_LOCATION_ITEM_GATES.get(location_data.code)
            if gate_item is not None:
                ff7_location.access_rule = (
                    lambda state, it=gate_item: state.has(it, player)
                )
            target_region.locations.append(ff7_location)

        # Shop-slot AP locations: placed in their shop's Free Roam region, so the
        # region's access rule gates reachability (e.g. Junon shops need Green
        # Chocobo). Shops whose region isn't created are skipped (unreachable).
        #
        # Gated on randomize_shops. The AP token slots are injected by Gold
        # Saucer's ShopRandomizer (loadApShops/applyApShops), and SimpleMainWindow
        # only calls randomizeShops() when Config::ShopRandomization is on — which
        # it sets from this very option in the .apff7. So with the option off the
        # slots are never written to the exe and every shop check is unobtainable
        # in-game, while AP happily filled them (progression included). Same
        # unbeatable-seed class as the parked Submarine. Fixed 2026-08-04.
        if self.options.randomize_shops:
            for shop_data in self._active_shop_slots():
                if shop_data.code in _FREE_ROAM_DEAD_LOCATION_CODES:
                    continue
                if shop_data.shop_id in _DEAD_SHOP_IDS:
                    continue
                if disable_gold_saucer and shop_data.region == "Gold Saucer Area":
                    continue
                target_region = sub_regions.get(shop_data.region)
                if target_region is None:
                    continue
                shop_loc = FF7Location(
                    player, shop_data.name, shop_data.code, target_region,
                )
                target_region.locations.append(shop_loc)

        # Weapon bosses are world-map encounters (not field maps), so wire them
        # directly onto World Map with their own access rules. Optional via
        # weapon_fight_checks (off = the Weapons aren't checks, just fightable).
        if self.options.weapon_fight_checks:
            for boss_name, tier in _FREE_ROAM_WEAPON_BOSSES.items():
                boss_data = ALL_LOCATION_TABLE.get(boss_name)
                if boss_data is None:
                    continue
                boss_loc = FF7Location(player, boss_name, boss_data.code, world_map)
                _rule = _tier_rules.get(tier, _ocean)
                # Every Weapon additionally wants a real squad (v0.0.6).
                if boss_name in _WEAPON_BOSSES_NEEDING_SQUAD:
                    _rule = (lambda state, r=_rule: r(state) and _squad(state))
                boss_loc.access_rule = _rule
                world_map.locations.append(boss_loc)

        victory_loc = FF7Location(player, self.victory_location_name, None, world_map)
        # Gate the goal so winning requires real endgame progression: the
        # Highwind (Northern Crater access), the full party (all 6 recruited),
        # and all 4 Huge Materia.
        victory_loc.access_rule = lambda state: (
            state.has("Highwind", player)
            and state.has_all(_PARTY_MEMBER_ITEMS, player)
            and state.has_all(_GOAL_HUGE_MATERIA, player)
        )
        victory_loc.place_locked_item(
            Item(self.victory_item_name, ItemClassification.progression, None, player)
        )
        world_map.locations.append(victory_loc)

    def create_items(self) -> None:
        free_roam = bool(self.options.free_roam)
        town_gating = free_roam and bool(self.options.town_gating)
        progressive_chocobos = free_roam and bool(self.options.progressive_chocobos)
        # Count only locations that still need an item. The victory location is
        # pre-filled with a locked item in create_regions; counting it here would
        # create one item too many for the available spots and break fill.
        available_locations = len([
            loc for loc in self.multiworld.get_locations(self.player)
            if loc.item is None
        ])
        # Build base pool, honoring per-item count (e.g. 3x Battery)
        pool_names: list[str] = []
        for name, data in ITEM_TABLE.items():
            if name == self.victory_item_name:
                continue
            if data.classification is ItemClassification.trap:
                continue  # traps only enter the pool via _build_trap_pool
            if name in _FREE_ROAM_ONLY_ITEMS and not free_roam:
                continue
            if name == "Key to Sector 5" and not free_roam:
                continue
            if free_roam and name in _FREE_ROAM_EXCLUDE_ITEMS:
                continue
            if name in _TOWN_KEY_ITEMS and not town_gating:
                continue  # town keys only when town_gating is enabled
            # Progressive chocobos: exactly ONE of the two models is in the pool.
            # Having both would double-count every traversal gate — a seed with
            # Green AND 4x Progressive satisfies mountain access twice over, and
            # the ladder stops meaning anything.
            if progressive_chocobos and name in _COLOUR_CHOCOBO_ITEMS:
                continue
            if not progressive_chocobos and name == "Progressive Chocobo":
                continue
            pool_names.extend([name] * data.count)

        # Classification with Free Roam downgrades applied (drives truncation).
        def _is_filler(n: str) -> bool:
            return self._effective_classification(n) is ItemClassification.filler

        if len(pool_names) < available_locations:
            filler_names = [
                n for n in ITEM_TABLE
                if _is_filler(n) and n != self.victory_item_name
                and not (free_roam and n in _FREE_ROAM_EXCLUDE_ITEMS)
            ]
            cycle = list(filler_names) if filler_names else [n for n in ITEM_TABLE if n != self.victory_item_name]
            idx = 0
            while len(pool_names) < available_locations:
                pool_names.append(cycle[idx % len(cycle)])
                idx += 1

        # If the base pool exceeds the location count it will be truncated below.
        # Sort filler last (stable) so truncation can only ever drop filler — never
        # a progression/useful item, which would make the seed unwinnable. Guard
        # loudly if progression+useful alone already overflow the locations.

        pool_names.sort(key=_is_filler)
        non_filler = sum(1 for n in pool_names if not _is_filler(n))
        if non_filler > available_locations:
            raise Exception(
                f"FF7 [player {self.player}]: {non_filler} progression/useful items "
                f"exceed {available_locations} available locations — cannot place "
                f"all required items. Add locations or reduce required items."
            )

        placed = pool_names[:available_locations]

        # swap a fraction of the placed filler slots for traps. positions come from
        # the pre-swap filler entries so progression/useful items are never displaced.
        trap_pool = self._build_trap_pool()
        trap_pct = int(self.options.trap_fill_percent)
        if trap_pool and trap_pct > 0:
            filler_positions = [i for i, n in enumerate(placed) if _is_filler(n)]
            n_traps = round(trap_pct / 100 * len(filler_positions))
            self.random.shuffle(filler_positions)
            for i in filler_positions[:n_traps]:
                placed[i] = self.random.choice(trap_pool)

        for name in placed:
            self.multiworld.itempool.append(self.create_item(name))

    def _build_trap_pool(self) -> list[str]:
        """weighted list of trap item names from the enabled trap options.
        each trap appears weight times so random.choice picks it proportionally,
        and weight 0 disables it. add new traps here."""
        weights = {
            "Frog Trap": int(self.options.frog_trap_weight),
            "Confusion Trap": int(self.options.confusion_trap_weight),
            "Frozen Trap": int(self.options.frozen_trap_weight),
            "Slowness Trap": int(self.options.slowness_trap_weight),
            "Slow Trap": int(self.options.slow_trap_weight),
            "Instant Death Trap": int(self.options.instant_death_trap_weight),
            "Double Damage": int(self.options.double_damage_weight),
            "Poison Trap": int(self.options.poison_trap_weight),
            "Tiny Trap": int(self.options.tiny_trap_weight),
            "Instant Crystal Trap": int(self.options.instant_crystal_trap_weight),
            "Sleep Trap": int(self.options.sleep_trap_weight),
            "Mana Drain Trap": int(self.options.mana_drain_trap_weight),
            "Market Crash Trap": int(self.options.market_crash_trap_weight),
            "Depression Trap": int(self.options.depression_trap_weight),
            "Curse Trap": int(self.options.curse_trap_weight),
            "Bomb Trap": int(self.options.bomb_trap_weight),
        }
        pool: list[str] = []
        for name, weight in weights.items():
            if weight > 0:
                pool.extend([name] * weight)
        return pool

    def _active_shop_slots(self):
        """The shop slots this seed actually uses, in stable per-shop order.

        The full grid is pre-declared up to the exe's ten-slot ceiling because
        location ids are static and cannot be minted per seed. A seed takes a
        PREFIX of each shop's list, so raising shop_slots_per_shop only ever adds
        slots and never renumbers existing ones.

        0 means "leave the hand-authored counts alone" — the shipped 1-6 per shop.
        That cannot be expressed as a single number, which is why 0 is a mode
        rather than an amount.
        """
        want = int(self.options.shop_slots_per_shop)
        if want <= 0:
            return [d for d in SHOP_LOCATION_TABLE.values()
                    if d.code in SHIPPED_SHOP_CODES]
        active = []
        for _shop_id, slots in sorted(SHOP_SLOTS_BY_SHOP.items()):
            active.extend(slots[:want])
        return active

    def _effective_classification(self, name: str) -> ItemClassification:
        """Item classification, applying Free Roam downgrades (linear unchanged)."""
        data = ITEM_TABLE.get(name)
        base = data.classification if data else ItemClassification.filler
        if self.options.free_roam:
            if name in _FREE_ROAM_USEFUL_ITEMS:
                return ItemClassification.useful
            if name in _FREE_ROAM_FILLER_ITEMS:
                return ItemClassification.filler
        return base

    def create_item(self, name: str):
        item = create_ff7_item(name, self.player)
        item.classification = self._effective_classification(name)
        return item

    def get_filler_item_name(self) -> str:
        """Return a random filler item name (AP core uses this for plando,
        item-links, and any extra slots it needs to fill)."""
        filler = [
            n for n, d in ITEM_TABLE.items()
            if d.classification is ItemClassification.filler and n != self.victory_item_name
        ]
        if not filler:
            return self.victory_item_name
        return self.random.choice(filler)

    def set_rules(self) -> None:
        apply_rules(self)

    def fill_hook(self, progitempool, usefulitempool, filleritempool, fill_locations):
        """
        Prioritize critical early-game progression items by moving them to front of pool.
        In Free Roam mode, prioritize vehicles and Key to Sector 5 instead.
        """
        if self.options.free_roam:
            # NOTE: Gold Chocobo is deliberately NOT prioritized early — it's a
            # do-everything traversal item; keeping it off this list (plus the
            # early-region item_rule in Rules.py) stops it landing in sphere 1.
            # Highwind, Submarine and Green Chocobo are likewise NOT force-
            # prioritized early (they place naturally per logic).
            # No forced-early items in Free Roam (Gold Ticket removed from the
            # pool 2026-07-09; Key to Sector 5 dropped from forced-early the
            # same day — it places naturally per logic).
            early_priority_items = []
        else:
            early_priority_items = [
                "Battery", "Cotton Dress", "Satin Dress", "Silk Dress",
                "Wig", "Dyed Wig", "Blonde Wig",
                "Keycard 60", "Keycard 62", "Keycard 65", "Keycard 66", "Keycard 68",
            ]

        # Items that must be placed FIRST, i.e. last in the pool. fill_restrictive
        # pops from the END, so the FRONT of the pool is placed last — when almost
        # nothing is collected and only sphere-0 locations are reachable. The Gold
        # Chocobo is barred from every sphere-0 region by Rules.py
        # (_GOLD_CHOCOBO_EARLY_REGIONS), so landing at the front makes it
        # unplaceable: "No more spots to place 1 items". Shop slots used to pad
        # the pool enough to hide this; with randomize_shops off it fails outright
        # on ~1% of seeds. Placing it first (state at its fullest, any late region
        # legal) removes the conflict without weakening the early-region ban.
        #
        # Not needed under progressive_chocobos: there is no "Gold Chocobo" item
        # then, and the 4th Progressive Chocobo is inherently late because the
        # first three gate it. Rules.py's early-region ban is likewise skipped —
        # item_rule sees one item, not a count, so it cannot bar "the 4th copy"
        # and barring ALL copies would push every chocobo out of sphere 0.
        late_placement_items = (["Gold Chocobo"]
                                if self.options.free_roam
                                and not self.options.progressive_chocobos else [])

        # Reorder progression pool: priority items first
        priority_items = []
        other_items = []
        deferred_items = []

        for item in progitempool:
            if item.player != self.player:
                other_items.append(item)
            elif item.name in early_priority_items:
                priority_items.append(item)
            elif item.name in late_placement_items:
                deferred_items.append(item)
            else:
                other_items.append(item)

        # Clear and rebuild: priority (placed last) -> rest -> deferred (placed first)
        progitempool.clear()
        progitempool.extend(priority_items)
        progitempool.extend(other_items)
        progitempool.extend(deferred_items)

    @classmethod
    def _get_ff7_option_names(cls) -> tuple[str, ...]:
        if cls._ff7_option_names is None:
            generic = set(PerGameCommonOptions.type_hints.keys())
            cls._ff7_option_names = tuple(
                name for name in cls.options_dataclass.type_hints.keys() if name not in generic
            )
        return cls._ff7_option_names

    def _serialize_ff7_options(self) -> dict:
        option_names = self._get_ff7_option_names()
        return self.options.as_dict(*option_names, toggles_as_bools=True)

    def _serialize_common_options(self) -> dict:
        common_names = tuple(PerGameCommonOptions.type_hints.keys())
        return self.options.as_dict(*common_names, toggles_as_bools=True)

    def fill_slot_data(self) -> dict:
        exporter = FF7JSONExporter(self)
        return {
            "player": self.multiworld.get_player_name(self.player),
            "game": self.game,
            "seed_name": self.multiworld.seed_name,
            "options": self._serialize_ff7_options(),
            "common_options": self._serialize_common_options(),
            "biton_map": exporter.build_biton_map_dict(),
            "shops": exporter._serialize_shops(),
            "victory_condition": self.options.victory_condition.value,
            "free_roam": bool(self.options.free_roam),
            "exp_multiplier": int(self.options.exp_multiplier.value),
            "gil_multiplier": int(self.options.gil_multiplier.value),
            "ap_multiplier": int(self.options.ap_multiplier.value),
        }

    def generate_output(self, output_directory: str) -> None:
        exporter = FF7JSONExporter(self)
        exporter.write_file(output_directory)
