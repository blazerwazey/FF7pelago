from .bases import FF7TestBase
from .. import _TOWN_GATE_KEYS

# Each class here just exercises a different option combination. Defining the
# class is enough: the inherited generic tests re-run reachability + fill for
# that configuration, so these catch "this combo fails to generate" regressions.


class TestLinear(FF7TestBase):
    """Non-Free-Roam (classic linear) generation."""

    options = {"free_roam": False}


class TestRandomFieldItems(FF7TestBase):
    options = {
        "free_roam": True,
        "randomize_field_items": True,
        "field_items_mode": "replace",
        "field_items_keep_type": True,
    }


class TestShopsRandomized(FF7TestBase):
    options = {
        "free_roam": True,
        "randomize_shops": True,
    }


class TestRewardMultipliers(FF7TestBase):
    """Multipliers are emitted into slot_data; make sure extreme values still
    generate."""

    options = {
        "free_roam": True,
        "exp_multiplier": 50,
        "gil_multiplier": 25,
        "ap_multiplier": 10,
    }

    def test_multipliers_in_slot_data(self) -> None:
        slot_data = self.world.fill_slot_data()
        self.assertEqual(slot_data["exp_multiplier"], 50)
        self.assertEqual(slot_data["gil_multiplier"], 25)
        self.assertEqual(slot_data["ap_multiplier"], 10)


_WEAPON_BOSSES = (
    "Defeat Ultimate Weapon",
    "Defeat Ruby Weapon",
    "Defeat Emerald Weapon",
)


class TestWeaponFightChecksOn(FF7TestBase):
    options = {"free_roam": True, "weapon_fight_checks": True}

    def test_weapon_bosses_present(self) -> None:
        names = {loc.name for loc in self.multiworld.get_locations(self.player)}
        for boss in _WEAPON_BOSSES:
            with self.subTest(boss):
                self.assertIn(boss, names)


class TestWeaponFightChecksOff(FF7TestBase):
    options = {"free_roam": True, "weapon_fight_checks": False}

    def test_weapon_bosses_absent(self) -> None:
        names = {loc.name for loc in self.multiworld.get_locations(self.player)}
        for boss in _WEAPON_BOSSES:
            with self.subTest(boss):
                self.assertNotIn(boss, names)


# Derived from the gate map rather than hand-listed: the hard-coded copy went
# stale when Fort Condor was ungated (2026-07-31, its key is excluded from the
# pool now) and it had never been updated for Costa del Sol Key either.
_TOWN_KEYS = tuple(sorted(set(_TOWN_GATE_KEYS.values())))


class TestTownGatingOn(FF7TestBase):
    """Free Roam + town gating: the town keys are progression items that gate
    their world-map entrances. The inherited reachability + fill tests confirm
    the seed still completes with the keys in the pool."""

    options = {"free_roam": True, "town_gating": True}

    def test_town_keys_in_pool(self) -> None:
        names = [it.name for it in self.multiworld.itempool]
        for key in _TOWN_KEYS:
            with self.subTest(key):
                self.assertIn(key, names)


class TestTownGatingOff(FF7TestBase):
    options = {"free_roam": True, "town_gating": False}

    def test_town_keys_absent(self) -> None:
        names = {it.name for it in self.multiworld.itempool}
        for key in _TOWN_KEYS:
            with self.subTest(key):
                self.assertNotIn(key, names)


class TestEveryOptionIsGrouped(FF7TestBase):
    """Every option FF7 declares must appear in exactly one option group.

    An ungrouped option is not dropped, which is what makes this easy to miss:
    Archipelago sweeps the leftovers into a synthetic "Game Options" group and the
    YAML renderer emits that group FIRST. The option lands at the top of the
    generated template, far from whatever it relates to.

    That has now caused two reports. ChocoboRaceChecks was missing from the groups
    and never appeared in the WebHost Options Creator; then the five v0.0.6 options
    templated ~110 lines above the settings they modify, and shop_slots_per_shop was
    reported missing from generated templates when it was simply somewhere else.

    Scoped to FF7's OWN options on purpose: Archipelago appends its own
    "Item & Location Options" group for the core ones (accessibility,
    progression_balancing, start_inventory_from_pool...), and those are its business.
    """
    options = {"free_roam": True}

    @staticmethod
    def _own_options():
        from Options import PerGameCommonOptions
        from ..Options import FF7Options
        core = set(PerGameCommonOptions.type_hints)
        return {name: opt for name, opt in FF7Options.type_hints.items()
                if name not in core}

    @staticmethod
    def _grouped():
        from .. import FF7Web
        return [opt for group in FF7Web.option_groups for opt in group.options]

    def test_no_option_is_ungrouped(self) -> None:
        grouped = self._grouped()
        ungrouped = sorted(name for name, opt in self._own_options().items()
                           if opt not in grouped)
        self.assertEqual([], ungrouped,
                         "these would be swept into a Game Options block at the top "
                         "of the generated YAML, away from what they relate to")

    def test_no_option_is_grouped_twice(self) -> None:
        grouped = self._grouped()
        own = set(self._own_options().values())
        dupes = sorted({o.__name__ for o in grouped
                        if o in own and grouped.count(o) > 1})
        self.assertEqual([], dupes)

    def test_removed_options_are_not_left_in_a_group(self) -> None:
        """A group entry for an option no longer in the dataclass is silently
        ignored by Archipelago, so it would linger forever. Start with Chocobo Lure
        was removed on 2026-09-02 and had to come out of the Gameplay group too."""
        from ..Options import FF7Options
        from Options import PerGameCommonOptions
        known = set(FF7Options.type_hints.values())
        core_names = {"StartInventoryPool"} | {
            o.__name__ for o in PerGameCommonOptions.type_hints.values()}
        stray = sorted({opt.__name__ for opt in self._grouped()
                        if opt not in known and opt.__name__ not in core_names})
        self.assertEqual([], stray)
