"""Zip-safe access to the bundled ``data/*.json`` files.

When this world is installed as a packaged ``.apworld`` it is imported from
inside a zip archive, so ``Path(__file__).parent / "data" / "x.json"`` and
``Path.read_text()`` / ``Path.exists()`` do NOT work (there is no real file on
disk). ``pkgutil.get_data`` goes through the import loader instead, so it works
both for a folder world (development) and a zipped ``.apworld`` (distribution).

All module-level data reads MUST go through these helpers — a raw filesystem
read at import time throws inside an ``.apworld`` and aborts the world load,
which (among other things) stops the client from registering in the Launcher.
"""
import json
import pkgutil
from typing import Any


def load_json(resource: str) -> Any:
    """Load a REQUIRED bundled JSON resource, e.g. ``"data/locations.json"``."""
    raw = pkgutil.get_data(__package__, resource)
    if raw is None:
        raise FileNotFoundError(f"FF7 bundled data resource not found: {resource}")
    return json.loads(raw.decode("utf-8"))


def try_load_json(resource: str, default: Any) -> Any:
    """Load an OPTIONAL bundled JSON resource, returning ``default`` if it is
    missing or unreadable."""
    try:
        raw = pkgutil.get_data(__package__, resource)
    except (FileNotFoundError, OSError):
        return default
    if raw is None:
        return default
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return default
