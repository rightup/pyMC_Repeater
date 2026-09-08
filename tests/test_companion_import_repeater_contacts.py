"""Regression tests for bulk 'import repeater contacts'.

``adverts.contact_type`` stores the *display* name written through
``handler_helpers.discovery.NODE_TYPE_NAMES`` -- "Chat Node", "Repeater",
"Room Server", "Sensor". The import API accepts MeshCore's names -- "companion",
"repeater", "room_server", "sensor" (validated in
``companion_endpoints.import_repeater_contacts``).

``companion_import_repeater_contacts`` compared the two directly:

    query += f" AND contact_type IN ({placeholders})"   # API names
                                                        # vs stored display names

so every filtered import matched zero rows and reported success having written
nothing. Separately the adv_type lookup normalised "Chat Node" to "chat_node",
which was absent from its map, so chat contacts imported as adv_type 0 rather
than 1 even on an unfiltered import.

Both derived the mapping independently; they now share one table.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repeater.data_acquisition.sqlite_handler import SQLiteHandler  # noqa: E402

# Exactly the forms the repeater writes.
SEEDS = [
    ("aa" * 32, "RepeaterOne", "Repeater", 2),
    ("bb" * 32, "ChatOne", "Chat Node", 1),
    ("cc" * 32, "RoomOne", "Room Server", 3),
    ("dd" * 32, "SensorOne", "Sensor", 4),
]


@pytest.fixture
def handler():
    d = tempfile.mkdtemp()
    h = SQLiteHandler(Path(d))
    con = sqlite3.connect(os.path.join(d, "repeater.db"))
    now = time.time()
    for pk, name, ctype, _ in SEEDS:
        con.execute(
            "INSERT INTO adverts"
            " (timestamp, pubkey, node_name, is_repeater, contact_type, latitude,"
            "  longitude, first_seen, last_seen, advert_count, is_new_neighbor, zero_hop)"
            " VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, 1, 0, 0)",
            (now, pk, name, 1 if ctype == "Repeater" else 0, ctype, now, now),
        )
    con.commit()
    con.close()
    yield h, d
    shutil.rmtree(d, ignore_errors=True)


def _imported(d, companion="0x01"):
    con = sqlite3.connect(os.path.join(d, "repeater.db"))
    try:
        return {
            (r[0].hex() if isinstance(r[0], (bytes, bytearray)) else r[0]): r[1]
            for r in con.execute(
                "select pubkey, adv_type from companion_contacts where companion_hash=?",
                (companion,),
            )
        }
    finally:
        con.close()


def test_unfiltered_import_takes_everything(handler):
    h, d = handler
    n = h.companion_import_repeater_contacts("0x01")
    assert n == len(SEEDS)
    assert len(_imported(d)) == len(SEEDS)


@pytest.mark.parametrize(
    "api_name,expected_pubkey",
    [
        ("repeater", "aa" * 32),
        ("companion", "bb" * 32),
        ("room_server", "cc" * 32),
        ("sensor", "dd" * 32),
    ],
)
def test_each_contact_type_filter_matches_its_stored_display_name(
    handler, api_name, expected_pubkey
):
    """The bug: every one of these returned 0."""
    h, d = handler
    n = h.companion_import_repeater_contacts("0x01", contact_types=[api_name])
    assert n == 1, "contact_types=[%r] imported nothing" % api_name
    assert set(_imported(d)) == {expected_pubkey}


def test_all_four_filters_together(handler):
    h, d = handler
    n = h.companion_import_repeater_contacts(
        "0x01", contact_types=["companion", "repeater", "room_server", "sensor"]
    )
    assert n == len(SEEDS)


def test_adv_type_is_correct_for_every_stored_name(handler):
    """'Chat Node' must import as adv_type 1, not 0."""
    h, d = handler
    h.companion_import_repeater_contacts("0x01")
    got = _imported(d)
    for pk, _name, stored, expected in SEEDS:
        assert got[pk] == expected, "%r imported as adv_type %s, expected %s" % (
            stored,
            got[pk],
            expected,
        )
    assert 0 not in got.values(), "some contact imported with adv_type 0 (unset)"


def test_unknown_contact_type_still_imports_nothing(handler):
    """A type the API would reject must not silently widen the query."""
    h, _ = handler
    assert h.companion_import_repeater_contacts("0x01", contact_types=["nonsense"]) == 0
