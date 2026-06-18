from test.bases import WorldTestBase

from .. import FF7World


class FF7TestBase(WorldTestBase):
    """Shared base for FF7 world tests.

    Subclassing WorldTestBase with a populated ``options`` dict automatically
    runs the generic world tests for that configuration:
      * every location is reachable when all items are collected,
      * sphere 1 is non-empty (something is reachable with no items),
      * the world fills without crashing.
    Individual test_*.py modules add FF7-specific assertions on top.
    """

    game = "Final Fantasy VII"
    world: FF7World
