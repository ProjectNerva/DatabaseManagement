"""Simultaneous submissions must never lose a record.

Each thread opens its own connection, so these exercise real SQLite lock
contention between writers, not just Python-level interleaving.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from CompetitionRegistrar import CompetitionRegistrar
from DatabaseManager import DatabaseManager
from Utils.JSONConverter import parse_schema_sql

WRITERS = 6


@pytest.fixture
def setup(tmp_path):
    registrar = CompetitionRegistrar(tmp_path / "Database")
    registrar.create(2026, "2026curie")
    return registrar, DatabaseManager(registrar), tmp_path


def submit_all(registrar, tmp_path, records):
    """Fire every record at the same competition at once, one thread each."""
    paths = []
    for i, record in enumerate(records):
        path = tmp_path / f"rec_{i}.json"
        path.write_text(json.dumps(record))
        paths.append(path)

    def run(path):
        manager = DatabaseManager(CompetitionRegistrar(registrar.root))
        return manager.submit(2026, "2026curie", "scouting", path)

    with ThreadPoolExecutor(max_workers=len(paths)) as pool:
        return list(pool.map(run, paths))


def test_simultaneous_identical_submissions_all_land_in_one_table(setup):
    registrar, manager, tmp_path = setup
    records = [{"team": 1000 + i, "notes": "x"} for i in range(WRITERS)]

    results = submit_all(registrar, tmp_path, records)

    assert {r.table_name for r in results} == {"scouting_v1"}
    assert manager.tables(2026, "2026curie") == {"scouting_v1": WRITERS}


def test_simultaneous_identical_submissions_declare_the_version_once(setup):
    registrar, manager, tmp_path = setup
    records = [{"team": 1000 + i, "notes": "x"} for i in range(WRITERS)]

    submit_all(registrar, tmp_path, records)

    sql = (registrar.competition_schemas_dir(2026, "2026curie") / "scouting.sql").read_text()
    assert list(parse_schema_sql(sql)) == ["scouting_v1"]
    assert sql.count("CREATE TABLE") == 1


def test_simultaneous_different_shapes_lose_no_records(setup):
    """Six scouting-app versions submitting at once - every record must be stored."""
    registrar, manager, tmp_path = setup
    records = [{"team": 1000 + i, f"field_{i}": i} for i in range(WRITERS)]

    results = submit_all(registrar, tmp_path, records)

    tables = manager.tables(2026, "2026curie")
    assert sum(tables.values()) == WRITERS, f"records lost: {tables}"
    assert len(tables) == WRITERS, f"expected one table per shape, got {tables}"
    assert len({r.table_name for r in results}) == WRITERS


def test_simultaneous_mixed_shapes_lose_no_records(setup):
    """Three of one shape, three of another, all at once."""
    registrar, manager, tmp_path = setup
    records = [
        {"team": i, "notes": "x"} if i % 2 else {"team": i, "notes": "x", "pickup": 1}
        for i in range(WRITERS)
    ]

    submit_all(registrar, tmp_path, records)

    tables = manager.tables(2026, "2026curie")
    assert sum(tables.values()) == WRITERS, f"records lost: {tables}"
    assert len(tables) == 2, f"expected exactly two shapes, got {tables}"


def test_the_schema_file_matches_the_database_afterwards(setup):
    registrar, manager, tmp_path = setup
    records = [{"team": 1000 + i, f"field_{i}": i} for i in range(WRITERS)]

    submit_all(registrar, tmp_path, records)

    sql = (registrar.competition_schemas_dir(2026, "2026curie") / "scouting.sql").read_text()
    assert set(parse_schema_sql(sql)) == set(manager.tables(2026, "2026curie"))


def test_a_lost_schema_file_is_rebuilt_from_the_database(setup):
    """The database is the authority, so a missing .sql must not break submissions."""
    registrar, manager, tmp_path = setup
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"team": 1, "notes": "x"}))
    manager.submit(2026, "2026curie", "scouting", path)

    sql_path = registrar.competition_schemas_dir(2026, "2026curie") / "scouting.sql"
    sql_path.unlink()

    other = tmp_path / "b.json"
    other.write_text(json.dumps({"team": 2, "notes": "x", "pickup": 3}))
    result = manager.submit(2026, "2026curie", "scouting", other)

    assert result.table_name == "scouting_v2"
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1, "scouting_v2": 1}
    assert set(parse_schema_sql(sql_path.read_text())) == {"scouting_v1", "scouting_v2"}
