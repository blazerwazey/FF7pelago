"""Final Fantasy VII IronMog Archipelago world implementation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import settings

from BaseClasses import Item, ItemClassification, MultiWorld, Region, Tutorial
from Options import OptionGroup, PerGameCommonOptions
from worlds.AutoWorld import WebWorld, World
from worlds.LauncherComponents import Component, Type, components, launch

from .Items import ITEM_TABLE, create_ff7_item, item_name_groups, item_name_to_id
from .Locations import (
    ALL_LOCATION_TABLE, FF7Location, PLACEABLE_LOCATION_CODES,
    SHOP_LOCATION_TABLE, location_name_groups, location_name_to_id,
)
from .Options import FF7Options
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
    "mtcrl_0":    "Mt. Corel",
    "mtcrl_1":    "Mt. Corel",
    "mtcrl_2":    "Mt. Corel",
    "mtcrl_3":    "Mt. Corel",
    "mtcrl_4":    "Mt. Corel",
    "mtcrl_5":    "Mt. Corel",
    "mtcrl_6":    "Mt. Corel",
    "mtcrl_7":    "Mt. Corel",
    "mtcrl_8":    "Mt. Corel",
    "mtcrl_9":    "Mt. Corel",
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
    "mds5_5":     "Midgar Sector 5",
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
    "gninn":      "Gongaga",
    "goson":      "Gongaga",
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
    "sininb42":   "Shinra Mansion Basement",
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
    # --- Cave of the Gi: NOT mapped. Its checks (Spirit Source, ambush trap
    #     chests, etc.) are tied to the Cosmo Canyon story visit and don't
    #     fire/aren't reachable at Free Roam moment 1603, so dropping the
    #     gidun_* maps here drops all of their locations.
    "anfrst_1":   "Ancient Forest",
    "anfrst_2":   "Ancient Forest",
    "anfrst_3":   "Ancient Forest",
    "anfrst_5":   "Ancient Forest",

    # --- Wutai (extended-tier chocobo) ---
    "datiao_8":   "Wutai",
    "tower5":     "Wutai",
    "yufy1":      "Wutai",
    "utapb":      "Wutai",

    # --- Temple of the Ancients: NOT mapped. The temple collapses at the end
    #     of the Keystone/Black Materia sequence (~moment 1000), so at Free Roam
    #     moment 1603 the kuro_*/jtmpin1 fields are unreachable and every check
    #     in them is dead. Dropping the maps here drops all their locations.

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
    #     1603 the gaiin_*/holu_1 fields are dead, so dropping the maps here
    #     drops all of their locations.
    "trnad_1":    "Whirlwind Maze",
    "trnad_2":    "Whirlwind Maze",
    "trnad_3":    "Whirlwind Maze",
    "trnad_4":    "Whirlwind Maze",
    "woa_1":      "Whirlwind Maze",
    # --- Northern Cave (interior, las*): NOT mapped. These are the final-dungeon
    #     fields, reached only via the end-game party-split descent; their checks
    #     are dead at Free Roam moment 1603, so dropping the maps here drops all
    #     of their locations.

    # --- Mideel (Highwind) ---
    "itown1b":    "Mideel",
    "itown_w":    "Mideel",
    "itown_i":    "Mideel",
    "itown_m":    "Mideel",
    "itmin2":     "Mideel",

    # --- Underwater Reactor (Submarine; reached via nearest-tier Junon access) ---
    "semkin_6":   "Underwater Reactor",
    "semkin_7":   "Underwater Reactor",
    "subin_1a":   "Underwater Reactor",

    # --- Sunken Gelnika (Submarine only) ---
    "q_1":        "Gelnika",
    "q_2":        "Gelnika",
    "q_3":        "Gelnika",
    "q_4":        "Gelnika",
}

# Items that only belong in the pool when Free Roam is enabled (world-map
# traversal unlocks: the Highwind + the layered colour chocobos).
_FREE_ROAM_ONLY_ITEMS = frozenset({
    "Highwind", "Submarine",
    "Green Chocobo", "Blue Chocobo", "Black Chocobo", "Gold Chocobo",
    # Party members — in linear mode they join via story, so they are only AP
    # items in Free Roam.
    "Barret", "Tifa", "Aerith", "Red XIII", "Cait Sith", "Cid",
})

# Optional party members (progression in Free Roam) and how many the goal
# requires — winning needs the Highwind plus a real squad, not just Cloud.
_PARTY_MEMBER_ITEMS = ["Barret", "Tifa", "Aerith", "Red XIII", "Cait Sith", "Cid"]
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
_FREE_ROAM_USEFUL_ITEMS = frozenset()
_FREE_ROAM_FILLER_ITEMS = frozenset({
    "Battery",
    "Cotton Dress", "Satin Dress", "Silk Dress",
    "Wig", "Dyed Wig", "Blonde Wig",
    "Key to Ancients", "Black Materia", "Keystone", "PHS",
    "Keycard 60", "Keycard 62", "Keycard 65", "Keycard 66", "Keycard 68",
    "Midgar Parts 1", "Midgar Parts 2", "Midgar Parts 3",
    "Midgar Parts 4", "Midgar Parts 5",
})
# Items never placed as Archipelago items in Free Roam. (The Submarine is now a
# real AP vehicle — it gates North Corel/Gold Saucer + underwater spots — so it
# is no longer excluded.)
_FREE_ROAM_EXCLUDE_ITEMS = frozenset()

# Locations that cannot be obtained in Free Roam (game moment 1603), so they
# must not receive items or they soft-lock the seed:
#   300062  Chocobo farm - Chocobo Lure — bought via a dialogue scene that the
#           late game state skips, so the pickup flag is never set.
#   300061  Chocobo farm - Kujata — bogus entry (no longer in the dataset).
#   310038  Fort Condor - Super Ball (convil_2) — a Fort Condor minigame reward
#           that the Free Roam state can't reach, so its flag never sets.
#   310014  Kalm - KeyItem: PHS (elminn_1) — the PHS hand-over only runs in the
#           post-flashback script (~moment 100); it never fires at 1603, and
#           the PHS is an AP-sent item in Free Roam anyway.
#   310010, 310020-310035  Wall Market Don Corneo dress-quest chain (Member's
#           Card, Colognes, Pharmacy Coupon, Wigs, Dresses, Disinfectant trio,
#           Tiaras) — disc-1-only events (~moment 300-400); the NPCs/scripts
#           are replaced on the disc-2 Midgar return, so none fire at 1603.
#   200018  Chocobo farm - Choco/Mog (farm) — the "talk to the chocobo" scene
#           that grants the summon doesn't fire at 1603.
#   310071  Nibelheim - Played piano during flashback (niv_ti2) — only set
#           inside the Kalm flashback (~moment 70); never fires at 1603.
# (frcyo "Chocobo Ranch" locations are dropped via FREE_ROAM_REGION_MAP, and
#  the whole Temple of the Ancients is dropped the same way — it has collapsed
#  by moment 1603.)
_FREE_ROAM_DEAD_LOCATION_CODES = frozenset({
    300061, 300062, 310038, 310014, 200018, 310071,
    # Wall Market dress-quest chain:
    310010, 310020, 310021, 310022, 310023, 310024, 310025, 310026,
    310027, 310028, 310029, 310030, 310031, 310032, 310033, 310034, 310035,
    # Removed by request: Sewer (Midgar, colne_b1) + Whirlwind Maze (trnad_*).
    300031, 300032,                                  # Sewer
    300248, 300249, 300398, 310016, 310074, 310075,  # Whirlwind Maze
})

# Weapon boss locations (detected by their savemap defeat flag) and the traversal
# tier needed to reach/fight each in Free Roam (see the chocobo tiers in
# _create_free_roam_regions). Reward items obey these gates.
_FREE_ROAM_WEAPON_BOSSES = {
    "Defeat Ultimate Weapon": "ocean",      # roams the world map (chase by Gold/Highwind)
    "Defeat Emerald Weapon":  "underwater", # deep underwater — Submarine
    "Defeat Ruby Weapon":     "ocean",      # Gold Saucer desert (western continent)
}

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
}


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

    option_groups = [
        OptionGroup(
            "Randomizers",
            [
                "randomize_field_items",
                "field_items_mode",
                "field_items_keep_type",
                "randomize_shops",
                "randomize_starting_equipment",
                "starting_equipment_tier",
            ],
        ),
        OptionGroup(
            "World",
            [
                "free_roam",
            ],
        ),
        OptionGroup(
            "Goal",
            [
                "victory_condition",
                "death_link",
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
    required_client_version: ClassVar[tuple[int, int, int]] = (0, 5, 1)
    web: ClassVar[FF7Web] = FF7Web()

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

        # Free Roam: force the early traversal keys into sphere-1 (foot-reachable)
        # locations so the world opens up. Green Chocobo reaches Junon; the
        # Submarine reaches North Corel + the Gold Saucer. (Gold Chocobo / Highwind
        # for the rest are then found within that expanded sphere.)
        if self.options.free_roam:
            self.multiworld.early_items[self.player]["Green Chocobo"] = 1
            self.multiworld.early_items[self.player]["Submarine"] = 1

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
        for shop_data in SHOP_LOCATION_TABLE.values():
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
            "Fort Condor",
            "Junon Lower",
            "Junon Upper",
            "Costa del Sol",
            "Corel",
            "Mt. Corel",
            "Gold Saucer Area",
            "Gongaga",
            "Cosmo Canyon",
            "Nibelheim",
            "Shinra Mansion Basement",
            "Mt. Nibel",
            "Rocket Town",
            "Ancient Forest",
            "Wutai",
            "Bone Village",
            "Sleeping Forest",
            "Forgotten Capital",
            "Corel Valley",
            "Icicle Inn",
            "Great Glacier",
            "Whirlwind Maze",
            "Mideel",
            "Underwater Reactor",
            "Gelnika",
            "Midgar Sector 5",
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
        # only the Gold Chocobo and the Highwind cross open ocean to other
        # continents; Green/Black cross the Junon mountain; the Submarine reaches
        # North Corel + the Gold Saucer (and underwater spots) but can't land you
        # on the other continents.
        def _mountain(state):    # Junon (mountain crossing)
            return (state.has("Green Chocobo", player) or state.has("Black Chocobo", player)
                    or state.has("Gold Chocobo", player) or state.has("Highwind", player))

        def _ocean(state):       # open-ocean continents
            return state.has("Gold Chocobo", player) or state.has("Highwind", player)

        def _sub(state):         # North Corel + Gold Saucer (Submarine), or full ocean
            return (state.has("Submarine", player) or state.has("Gold Chocobo", player)
                    or state.has("Highwind", player))

        def _underwater(state):  # underwater only (Submarine)
            return state.has("Submarine", player)

        # --- Eastern continent, foot-reachable (no gate) ---
        world_map.connect(sub_regions["Kalm"])
        world_map.connect(sub_regions["Mythril Mines"])
        world_map.connect(sub_regions["Chocobo Farm"])
        world_map.connect(sub_regions["Fort Condor"])

        # --- Junon (mountain crossing) ---
        world_map.connect(sub_regions["Junon Lower"]).access_rule = _mountain
        world_map.connect(sub_regions["Junon Upper"]).access_rule = _mountain

        # --- North Corel + Gold Saucer (Submarine reaches these; or Gold/Highwind) ---
        world_map.connect(sub_regions["Corel"]).access_rule = _sub
        world_map.connect(sub_regions["Gold Saucer Area"]).access_rule = (
            lambda state: _sub(state) and state.has("Gold Ticket", player)
        )

        # --- Open-ocean continents (Gold Chocobo or Highwind) ---
        for _name in ("Costa del Sol", "Mt. Corel", "Gongaga", "Cosmo Canyon",
                      "Nibelheim", "Mt. Nibel", "Rocket Town", "Ancient Forest",
                      "Wutai", "Bone Village", "Sleeping Forest", "Icicle Inn",
                      "Mideel"):
            world_map.connect(sub_regions[_name]).access_rule = _ocean
        # Shinra Mansion basement: ocean + Basement Key.
        world_map.connect(sub_regions["Shinra Mansion Basement"]).access_rule = (
            lambda state: _ocean(state) and state.has("Basement Key", player)
        )
        # Northern forests past Sleeping Forest need the Lunar Harp.
        world_map.connect(sub_regions["Forgotten Capital"]).access_rule = (
            lambda state: _ocean(state) and state.has("Lunar Harp", player)
        )
        world_map.connect(sub_regions["Corel Valley"]).access_rule = (
            lambda state: _ocean(state) and state.has("Lunar Harp", player)
        )
        world_map.connect(sub_regions["Great Glacier"]).access_rule = (
            lambda state: _ocean(state) and state.has("Snowboard", player)
        )

        # --- Northern Crater interior: Highwind only ---
        world_map.connect(sub_regions["Whirlwind Maze"]).access_rule = _has("Highwind")

        # --- Underwater (Submarine): Underwater Reactor + sunken Gelnika ---
        world_map.connect(sub_regions["Underwater Reactor"]).access_rule = _underwater
        world_map.connect(sub_regions["Gelnika"]).access_rule = _underwater

        # --- Midgar return (Key to Sector 5) ---
        world_map.connect(sub_regions["Midgar Sector 5"]).access_rule = _has("Key to Sector 5")

        # Resolve weapon-boss traversal tiers to predicates (used below).
        _tier_rules = {"mountain": _mountain, "ocean": _ocean, "sub": _sub,
                       "underwater": _underwater, "highwind": _has("Highwind")}

        for location_data in ALL_LOCATION_TABLE.values():
            if location_data.name == self.victory_location_name:
                continue
            if location_data.code in _FREE_ROAM_DEAD_LOCATION_CODES:
                continue  # unobtainable at game moment 1603 — would soft-lock
            if location_data.code not in PLACEABLE_LOCATION_CODES:
                continue  # not a real field pickup -> Gold Saucer can't place/track it
            region_name = FREE_ROAM_REGION_MAP.get(location_data.map)
            if region_name is None:
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
        for shop_data in SHOP_LOCATION_TABLE.values():
            target_region = sub_regions.get(shop_data.region)
            if target_region is None:
                continue
            shop_loc = FF7Location(
                player, shop_data.name, shop_data.code, target_region,
            )
            target_region.locations.append(shop_loc)

        # Weapon bosses are world-map encounters (not field maps), so wire them
        # directly onto World Map with their own access rules.
        for boss_name, tier in _FREE_ROAM_WEAPON_BOSSES.items():
            boss_data = ALL_LOCATION_TABLE.get(boss_name)
            if boss_data is None:
                continue
            boss_loc = FF7Location(player, boss_name, boss_data.code, world_map)
            boss_loc.access_rule = _tier_rules.get(tier, _ocean)
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
            if name in _FREE_ROAM_ONLY_ITEMS and not free_roam:
                continue
            if name == "Key to Sector 5" and not free_roam:
                continue
            if free_roam and name in _FREE_ROAM_EXCLUDE_ITEMS:
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

        for name in pool_names[:available_locations]:
            self.multiworld.itempool.append(self.create_item(name))

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
            early_priority_items = [
                "Green Chocobo", "Submarine", "Gold Chocobo", "Highwind",
                "Gold Ticket", "Key to Sector 5",
            ]
        else:
            early_priority_items = [
                "Battery", "Cotton Dress", "Satin Dress", "Silk Dress",
                "Wig", "Dyed Wig", "Blonde Wig",
                "Keycard 60", "Keycard 62", "Keycard 65", "Keycard 66", "Keycard 68",
            ]

        # Reorder progression pool: priority items first
        priority_items = []
        other_items = []

        for item in progitempool:
            if item.name in early_priority_items and item.player == self.player:
                priority_items.append(item)
            else:
                other_items.append(item)

        # Clear and rebuild progitempool with priority items first
        progitempool.clear()
        progitempool.extend(priority_items)
        progitempool.extend(other_items)

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
        }

    def generate_output(self, output_directory: str) -> None:
        exporter = FF7JSONExporter(self)
        exporter.write_file(output_directory)
