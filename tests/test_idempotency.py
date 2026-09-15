"""A retried submission must never produce a second row.

These test the guarantee at the DatabaseManager level, below HTTP: the ledger write
shares a transaction with the record, so the two can only land together.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from CompetitionRegistrar import CompetitionRegistrar
from DatabaseManager import LEDGER_TABLE, DatabaseManager


@pytest.fixture
def manager(tmp_path):
    registrar = CompetitionRegistrar(tmp_path / "Database")
    registrar.create(2026, "2026curie")
    return DatabaseManager(registrar)


def submit(manager, record, submission_id=None):
    return manager.submit_record(2026, "2026curie", "scouting", record, submission_id=submission_id)


def test_same_id_twice_stores_one_row(manager):
    first = submit(manager, {"team": 4414, "notes": "x"}, "id-1")
    second = submit(manager, {"team": 4414, "notes": "x"}, "id-1")

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.row_id == first.row_id
    assert second.table_name == first.table_name
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1}


def test_duplicate_reported_even_when_payload_differs(manager):
    """The id is the identity, not the content. A client that retries a record it
    edited in between still must not double-write - the first one already landed."""
    first = submit(manager, {"team": 4414, "notes": "x"}, "id-1")
    second = submit(manager, {"team": 9999, "notes": "totally different"}, "id-1")

    assert second.duplicate is True
    assert second.row_id == first.row_id
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1}


def test_different_ids_store_separately(manager):
    submit(manager, {"team": 1, "notes": "x"}, "id-1")
    submit(manager, {"team": 2, "notes": "x"}, "id-2")
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 2}


def test_no_id_means_no_deduplication(manager):
    """Submissions without an id keep the old behaviour exactly: two identical
    records are two rows, because nothing claims they are the same event."""
    submit(manager, {"team": 4414, "notes": "x"})
    submit(manager, {"team": 4414, "notes": "x"})
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 2}


def test_ledger_is_not_created_until_an_id_is_used(manager, tmp_path):
    submit(manager, {"team": 4414})
    db = tmp_path / "Database/2026/Data/2026curie/2026curie.db"
    names = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert LEDGER_TABLE not in names


def test_ledger_is_never_offered_as_a_record_shape(manager):
    """The regression this guards: _table_columns feeding the ledger to choose_table,
    which would let a scouting record be matched against bookkeeping columns."""
    submit(manager, {"submission_id": "looks-like-the-ledger", "table_name": "x", "row_id": 1}, "a")
    submit(manager, {"submission_id": "looks-like-the-ledger", "table_name": "y", "row_id": 2}, "b")

    tables = manager.tables(2026, "2026curie")
    assert LEDGER_TABLE not in tables
    # Both records shared a shape, so they belong in one version table - not split
    # apart, and not merged into the ledger.
    assert tables == {"scouting_v1": 2}


def test_ledger_hidden_from_table_listing(manager):
    submit(manager, {"team": 4414}, "id-1")
    assert LEDGER_TABLE not in manager.tables(2026, "2026curie")


def test_ledger_records_where_the_row_landed(manager, tmp_path):
    result = submit(manager, {"team": 4414}, "id-1")
    db = tmp_path / "Database/2026/Data/2026curie/2026curie.db"
    row = sqlite3.connect(db).execute(
        f'SELECT submission_id, table_name, row_id FROM "{LEDGER_TABLE}"'
    ).fetchone()
    assert row == ("id-1", result.table_name, result.row_id)


def test_rejected_record_leaves_no_ledger_entry(manager, tmp_path):
    """A record refused for a bad column name must not burn its id - otherwise a
    corrected resubmission would be waved through as an already-stored duplicate."""
    with pytest.raises(ValueError):
        submit(manager, {"bad name!": 1}, "id-1")

    ok = submit(manager, {"good_name": 1}, "id-1")
    assert ok.duplicate is False
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1}


def test_duplicate_of_a_version_creating_record_creates_nothing(manager):
    submit(manager, {"team": 1, "notes": "x"}, "id-1")
    first = submit(manager, {"team": 1, "auto": 3}, "id-2")
    assert first.created is True

    again = submit(manager, {"team": 1, "auto": 3}, "id-2")
    assert again.duplicate is True
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1, "scouting_v2": 1}
