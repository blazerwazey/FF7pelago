from .bases import FF7TestBase


class TestDefaultOptions(FF7TestBase):
    """Default options. Generation, reachability and fill are covered by the
    inherited generic WorldTestBase tests."""

    options = {}


class TestDataPackageIntegrity(FF7TestBase):
    """Guards the datapackage the server/WebHost ingests. Malformed ids are a
    common cause of 'Could not load multidata' failures on archipelago.gg."""

    options = {}

    def test_ids_are_unique_positive_ints(self) -> None:
        for table_name, mapping in (
            ("item_name_to_id", self.world.item_name_to_id),
            ("location_name_to_id", self.world.location_name_to_id),
        ):
            with self.subTest(table_name):
                for name, code in mapping.items():
                    self.assertIsInstance(
                        code, int, f"{table_name}[{name!r}] = {code!r} is not an int")
                    self.assertGreater(
                        code, 0, f"{table_name}[{name!r}] = {code} is not a positive id")
                ids = list(mapping.values())
                self.assertEqual(
                    len(ids), len(set(ids)), f"{table_name} contains duplicate ids")

    def test_output_filename_has_player_prefix(self) -> None:
        # The WebHost (archipelago.gg) parses unrecognised seed files as
        # AP_<seed>_P<n>_<name> and does int(slot_id[1:]) to strip the "P".
        # A name without that prefix yields int("") and the upload is rejected,
        # so the .apff7 MUST come from get_out_file_name_base. See
        # json_export.FF7JSONExporter.write_file.
        base = self.multiworld.get_out_file_name_base(self.player)
        self.assertIn(f"_P{self.player}_", base)


class TestItemGroups(FF7TestBase):
    """Item groups are hint targets (`!hint <group>`) and are also usable in
    YAML for local_items / non_local_items / item links, so their membership is
    part of the world's public surface."""

    options = {}

    CHARACTERS = {
        "Barret", "Tifa", "Aerith", "Red XIII",
        "Cait Sith", "Cid", "Vincent", "Yuffie",
    }

    def test_characters_group_is_the_full_roster(self) -> None:
        """Exactly the eight recruitables. Cloud is NOT one: he starts in the
        party and has no item, so a group containing him would hint nothing."""
        group = self.world.item_name_groups["Characters"]
        self.assertEqual(set(group), self.CHARACTERS)
        self.assertNotIn("Cloud", group)

    def test_characters_group_members_are_real_items(self) -> None:
        """A group naming an item that does not exist is silently skipped by the
        server's hint expansion, which would make the group quietly incomplete."""
        for name in self.world.item_name_groups["Characters"]:
            with self.subTest(name):
                self.assertIn(name, self.world.item_name_to_id)
