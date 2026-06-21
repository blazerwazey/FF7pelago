from .bases import FF7TestBase
from .. import _FREE_ROAM_DEAD_LOCATION_CODES


class TestFreeRoam(FF7TestBase):
    """Free Roam with the Gold Saucer checks enabled (the default world)."""

    options = {
        "free_roam": True,
        "disable_gold_saucer": False,
    }

    def test_dead_locations_excluded(self) -> None:
        """No code in the Free Roam dead set (Cargo Ship, Underwater Reactor,
        Shinra building, Temple of the Ancients, etc.) may reach the pool."""
        placed = {
            loc.address for loc in self.multiworld.get_locations(self.player)
            if loc.address is not None
        }
        leaked = _FREE_ROAM_DEAD_LOCATION_CODES & placed
        self.assertFalse(leaked, f"excluded codes leaked into the pool: {sorted(leaked)}")

    def test_gold_saucer_region_populated(self) -> None:
        region = self.multiworld.get_region("Gold Saucer Area", self.player)
        self.assertGreater(
            len(region.locations), 0,
            "Gold Saucer Area should contain checks when disable_gold_saucer is off")

    def test_recovered_locations_present(self) -> None:
        """The Gongaga Titan and Chocobo Sage checks were re-added by mapping
        their maps (gnmk, zz3) to regions; make sure they stay in the pool."""
        names = {loc.name for loc in self.multiworld.get_locations(self.player)}
        for expected in (
            "Gongaga Reactor - Titan",
            "Chocobo Sage's house - Enemy Skill",
        ):
            with self.subTest(expected):
                self.assertIn(expected, names)


class TestFreeRoamNoGoldSaucer(FF7TestBase):
    """Free Roam with disable_gold_saucer on — every Gold Saucer check drops."""

    options = {
        "free_roam": True,
        "disable_gold_saucer": True,
    }

    def test_gold_saucer_region_empty(self) -> None:
        region = self.multiworld.get_region("Gold Saucer Area", self.player)
        self.assertEqual(
            len(region.locations), 0,
            "Gold Saucer Area should be empty when disable_gold_saucer is on")

    def test_dead_locations_excluded(self) -> None:
        placed = {
            loc.address for loc in self.multiworld.get_locations(self.player)
            if loc.address is not None
        }
        self.assertFalse(_FREE_ROAM_DEAD_LOCATION_CODES & placed)
