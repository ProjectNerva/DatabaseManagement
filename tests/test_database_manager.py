"""Tests for DatabaseManager: routing one submission into the right version table."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from CompetitionRegistrar import CompetitionNotFound, CompetitionRegistrar
from DatabaseManager import DatabaseManager


@pytest.fixture
def manager(tmp_path):
    registrar = CompetitionRegistrar(tmp_path / "Database")
    registrar.create(2026, "2026curie")
    return DatabaseManager(registrar)


def write_json(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


def rows(manager, table):
    conn = sqlite3.connect(manager.registrar.db_path(2026, "2026curie"))
    try:
        return conn.execute(f'SELECT * FROM "{table}"').fetchall()
    finally:
        conn.close()


def test_submit_creates_the_table_and_inserts_the_record(manager, tmp_path):
    path = write_json(tmp_path, "m1.json", '{"team_number": 8020, "scout_id": "Dhruv"}')

    result = manager.submit(2026, "2026curie", "scouting", path)

    assert result.table_name == "scouting_v1"
    assert result.created is True
    assert rows(manager, "scouting_v1") == [(1, 8020, "Dhruv")]


def test_a_second_matching_submission_lands_in_the_same_table(manager, tmp_path):
    a = write_json(tmp_path, "m1.json", '{"team_number": 8020, "scout_id": "Dhruv"}')
    b = write_json(tmp_path, "m2.json", '{"team_number": 4414, "scout_id": "Alex"}')

    manager.submit(2026, "2026curie", "scouting", a)
    result = manager.submit(2026, "2026curie", "scouting", b)

    assert result.table_name == "scouting_v1"
    assert result.created is False
    assert len(rows(manager, "scouting_v1")) == 2


def test_a_new_field_routes_into_a_second_table(manager, tmp_path):
    a = write_json(tmp_path, "m1.json", '{"team_number": 8020}')
    b = write_json(tmp_path, "m2.json", '{"team_number": 4414, "auto_pickup": 3}')

    manager.submit(2026, "2026curie", "scouting", a)
    result = manager.submit(2026, "2026curie", "scouting", b)

    assert result.table_name == "scouting_v2"
    assert result.reasons == ["added field: auto_pickup"]
    assert len(rows(manager, "scouting_v1")) == 1
    assert len(rows(manager, "scouting_v2")) == 1


def test_a_blank_field_does_not_create_a_second_table(manager, tmp_path):
    a = write_json(tmp_path, "m1.json", '{"team_number": 8020, "auto_climb": 3}')
    b = write_json(tmp_path, "m2.json", '{"team_number": 4414, "auto_climb": ""}')

    manager.submit(2026, "2026curie", "scouting", a)
    result = manager.submit(2026, "2026curie", "scouting", b)

    assert result.table_name == "scouting_v1"
    assert len(rows(manager, "scouting_v1")) == 2


def test_a_blank_value_is_stored_as_null_not_as_an_empty_string(manager, tmp_path):
    path = write_json(tmp_path, "m1.json", '{"team_number": 8020, "defense_qata": ""}')

    manager.submit(2026, "2026curie", "scouting", path)

    assert rows(manager, "scouting_v1") == [(1, 8020, None)]


def test_nested_values_are_stored_as_json_text(manager, tmp_path):
    path = write_json(tmp_path, "m1.json", '{"endgame": {"climb": 3}}')

    manager.submit(2026, "2026curie", "scouting", path)

    assert rows(manager, "scouting_v1") == [(1, '{"climb": 3}')]


def test_the_schema_file_records_every_version(manager, tmp_path):
    a = write_json(tmp_path, "m1.json", '{"team_number": 8020}')
    b = write_json(tmp_path, "m2.json", '{"team_number": 4414, "auto_pickup": 3}')

    manager.submit(2026, "2026curie", "scouting", a)
    manager.submit(2026, "2026curie", "scouting", b)

    text = (manager.registrar.competition_schemas_dir(2026, "2026curie") / "scouting.sql").read_text()
    assert "scouting_v1" in text
    assert "scouting_v2" in text


def test_submitting_to_a_competition_that_does_not_exist_is_refused(manager, tmp_path):
    path = write_json(tmp_path, "m1.json", '{"team_number": 8020}')

    with pytest.raises(CompetitionNotFound):
        manager.submit(2026, "ghost", "scouting", path)


def test_accuracy_of_exactly_one_stays_in_the_same_table(manager, tmp_path):
    """0.9 then 1 then 0.75 is one field, not three schemas."""
    for i, value in enumerate([0.9, 1, 0.75]):
        path = write_json(tmp_path, f"m{i}.json", f'{{"team": {i}, "accuracy": {value}}}')
        result = manager.submit(2026, "2026curie", "scouting", path)
        assert result.table_name == "scouting_v1", f"{value} forked a table"
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 3}


def test_a_decimal_survives_a_column_first_seen_as_an_integer(manager, tmp_path):
    a = write_json(tmp_path, "a.json", '{"accuracy": 1}')
    b = write_json(tmp_path, "b.json", '{"accuracy": 0.85}')
    manager.submit(2026, "2026curie", "scouting", a)
    manager.submit(2026, "2026curie", "scouting", b)

    conn = sqlite3.connect(manager.registrar.db_path(2026, "2026curie"))
    stored = conn.execute("SELECT accuracy, typeof(accuracy) FROM scouting_v1").fetchall()
    assert stored == [(1, "integer"), (0.85, "real")]


def test_a_number_after_a_blank_is_stored_as_a_number(manager, tmp_path):
    """The blank comes first, so the column is created before any type is known."""
    blank = write_json(tmp_path, "blank.json", '{"team": 1, "auto_climb": ""}')
    filled = write_json(tmp_path, "filled.json", '{"team": 2, "auto_climb": 3}')

    manager.submit(2026, "2026curie", "scouting", blank)
    result = manager.submit(2026, "2026curie", "scouting", filled)

    assert result.table_name == "scouting_v1"
    conn = sqlite3.connect(manager.registrar.db_path(2026, "2026curie"))
    assert conn.execute(
        "SELECT auto_climb, typeof(auto_climb) FROM scouting_v1 WHERE team = 2"
    ).fetchone() == (3, "integer")


def test_text_after_a_blank_is_still_stored_as_text(manager, tmp_path):
    blank = write_json(tmp_path, "blank.json", '{"team": 1, "notes": ""}')
    filled = write_json(tmp_path, "filled.json", '{"team": 2, "notes": "pushed bot"}')

    manager.submit(2026, "2026curie", "scouting", blank)
    result = manager.submit(2026, "2026curie", "scouting", filled)

    assert result.table_name == "scouting_v1"
    conn = sqlite3.connect(manager.registrar.db_path(2026, "2026curie"))
    assert conn.execute(
        "SELECT notes, typeof(notes) FROM scouting_v1 WHERE team = 2"
    ).fetchone() == ("pushed bot", "text")
