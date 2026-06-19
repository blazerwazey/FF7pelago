from .bases import FF7TestBase

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
