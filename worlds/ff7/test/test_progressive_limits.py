"""Progressive Limits: pool shape, and the savemap bit math behind it."""
from .bases import FF7TestBase

CHARACTERS = ("Cloud", "Barret", "Tifa", "Aerith", "Red XIII",
              "Yuffie", "Cait Sith", "Vincent", "Cid")
LIMIT_ITEMS = tuple(f"Progressive Limit ({c})" for c in CHARACTERS)
STEPS = 5   # 1-2, 2-1, 2-2, 3-1, 3-2 -- level 4 keeps its manual item


class TestProgressiveLimitsOn(FF7TestBase):
    """Default. Five copies per character, all nine characters."""

    options = {"free_roam": 1, "progressive_limits": 1}

    def test_five_copies_of_each(self) -> None:
        names = [i.name for i in self.multiworld.itempool]
        for item in LIMIT_ITEMS:
            with self.subTest(item):
                self.assertEqual(names.count(item), STEPS)

    def test_they_are_useful_not_progression(self) -> None:
        """Deliberate: limits win fights, they do not open regions. Making 45
        items progression would tighten fill for no gain in reachability."""
        for i in self.multiworld.itempool:
            if i.name in LIMIT_ITEMS:
                with self.subTest(i.name):
                    self.assertFalse(i.advancement,
                                     f"{i.name} should not be progression")

    def test_level_four_manuals_are_untouched(self) -> None:
        """Level 4 is NOT one of the steps -- it still comes from its manual,
        which is a normal pool item that already worked."""
        names = [i.name for i in self.multiworld.itempool]
        self.assertIn("Omnislash", names)

    def test_slot_data_tells_the_client_to_suppress(self) -> None:
        """Without this flag the client cannot tell an option-off seed from an
        option-on seed that has not sent any limits yet."""
        self.assertTrue(self.world.fill_slot_data()["progressive_limits"])


class TestProgressiveLimitsOff(FF7TestBase):
    """Option off: limits unlock the vanilla way and the items are absent."""

    options = {"free_roam": 1, "progressive_limits": 0}

    def test_no_limit_items_in_the_pool(self) -> None:
        names = [i.name for i in self.multiworld.itempool]
        for item in LIMIT_ITEMS:
            with self.subTest(item):
                self.assertEqual(names.count(item), 0)

    def test_slot_data_flag_is_false(self) -> None:
        """The client must leave the game's own limit teaching alone."""
        self.assertFalse(self.world.fill_slot_data()["progressive_limits"])


class TestLimitBitMath(FF7TestBase):
    """The savemap side, tested without a running game.

    FF7 gives each limit level three bits: L1 = 0-2, L2 = 3-5, L3 = 6-8, and
    L4 = bit 9. The five steps set bits 1, 3, 4, 6, 7.
    """

    options = {}

    @staticmethod
    def _mask_and_level(granted, current=0):
        from ..FF7Client import _limit_mask_and_level
        return _limit_mask_and_level(granted, current)

    def test_nothing_granted_still_leaves_a_working_limit(self) -> None:
        mask, level = self._mask_and_level(0)
        self.assertEqual(mask, 0x0001, "level 1-1 must always stay learned")
        self.assertEqual(level, 1)

    def test_each_step_in_order(self) -> None:
        expected = [
            (0, 0b0000000001, 1),   # 1-1 only
            (1, 0b0000000011, 1),   # +1-2
            (2, 0b0000001011, 2),   # +2-1  -> level 2 selectable
            (3, 0b0000011011, 2),   # +2-2
            (4, 0b0001011011, 3),   # +3-1  -> level 3 selectable
            (5, 0b0011011011, 3),   # +3-2
        ]
        for granted, mask, level in expected:
            with self.subTest(granted=granted):
                self.assertEqual(self._mask_and_level(granted), (mask, level))

    def test_extra_copies_do_not_reach_level_four(self) -> None:
        """A sixth copy cannot exist, but if one arrived it must not hand out
        the level 4 limit -- that belongs to the manual item."""
        mask, level = self._mask_and_level(9)
        self.assertEqual(mask, 0b0011011011)
        self.assertEqual(level, 3)

    def test_a_manual_taught_level_four_is_preserved(self) -> None:
        """Clearing bit 9 would confiscate an Omnislash the player owns."""
        mask, level = self._mask_and_level(0, current=0x0200)
        self.assertTrue(mask & 0x0200, "level 4 must survive")
        self.assertEqual(level, 4)

    def test_unearned_bits_are_taken_back(self) -> None:
        """The point of the feature: whatever the game taught off kill counts is
        removed unless Archipelago granted it."""
        mask, _ = self._mask_and_level(1, current=0b0011011011)
        self.assertEqual(mask, 0b0000000011)
