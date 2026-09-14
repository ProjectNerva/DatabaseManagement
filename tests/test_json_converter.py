"""Tests for Utils.JSONConverter: type inference and schema compatibility."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from Utils.JSONConverter import (
    UNKNOWN,
    RecordError,
    compatible,
    create_table_sql,
    describe_mismatch,
    infer_columns,
    choose_table,
    load_record,
    parse_schema_sql,
    render_schema_file,
)


# --- infer_columns --------------------------------------------------

def test_infers_a_type_for_every_scalar_kind():
    columns = infer_columns(
        {"name": "Dhruv", "team": 8020, "accuracy": 0.9, "climbed": True}
    )
    assert columns == {
        "name": "TEXT",
        "team": "INTEGER",
        "accuracy": "REAL",
        "climbed": "INTEGER",
    }


def test_null_infers_as_unknown_not_a_concrete_type():
    assert infer_columns({"defense_qata": None}) == {"defense_qata": UNKNOWN}


def test_nested_values_are_stored_as_text():
    columns = infer_columns({"endgame": {"climb": 3}, "penalties": [1, 2]})
    assert columns == {"endgame": "TEXT", "penalties": "TEXT"}


def test_empty_record_has_no_columns():
    assert infer_columns({}) == {}


def test_rejects_a_column_name_that_could_break_out_of_the_ddl():
    with pytest.raises(ValueError, match="invalid column name"):
        infer_columns({'bad", x INTEGER); DROP TABLE scouting; --': 1})


# --- compatible -----------------------------------------------------

def test_identical_schemas_are_compatible():
    columns = {"team": "INTEGER", "notes": "TEXT"}
    assert compatible(columns, dict(columns)) is True


def test_blank_field_matches_the_same_field_filled_in():
    """The case that would otherwise fork a table every time a scout skips a field."""
    blank = {"team": "INTEGER", "auto_climb": UNKNOWN}
    filled = {"team": "INTEGER", "auto_climb": "INTEGER"}
    assert compatible(blank, filled) is True
    assert compatible(filled, blank) is True


def test_two_unknowns_are_compatible():
    assert compatible({"x": UNKNOWN}, {"x": UNKNOWN}) is True


def test_column_order_does_not_matter():
    assert compatible(
        {"team": "INTEGER", "notes": "TEXT"},
        {"notes": "TEXT", "team": "INTEGER"},
    ) is True


def test_an_added_field_is_not_compatible():
    assert compatible({"team": "INTEGER"}, {"team": "INTEGER", "extra": "TEXT"}) is False


def test_a_missing_field_is_not_compatible():
    assert compatible({"team": "INTEGER", "extra": "TEXT"}, {"team": "INTEGER"}) is False


def test_a_real_type_conflict_is_not_compatible():
    assert compatible({"accuracy": "REAL"}, {"accuracy": "TEXT"}) is False


# --- create_table_sql -----------------------------------------------

def test_builds_create_table_with_an_id_primary_key():
    sql = create_table_sql("scouting_v1", {"team": "INTEGER", "notes": "TEXT"})
    assert sql == (
        'CREATE TABLE IF NOT EXISTS "scouting_v1" (\n'
        "    id INTEGER PRIMARY KEY,\n"
        '    "team" INTEGER,\n'
        '    "notes" TEXT\n'
        ");"
    )


def test_unknown_never_reaches_sqlite_as_a_type():
    """UNKNOWN is a matching sentinel, not a SQL type - it must never reach SQLite."""
    sql = create_table_sql("scouting_v1", {"defense_qata": UNKNOWN})
    assert '"defense_qata" BLOB' in sql
    assert UNKNOWN not in sql


def test_rejects_a_table_name_that_could_break_out_of_the_ddl():
    with pytest.raises(ValueError, match="invalid table name"):
        create_table_sql('x"; DROP TABLE scouting; --', {"team": "INTEGER"})


# --- blank values ---------------------------------------------------

def test_empty_string_is_treated_as_blank_not_as_text():
    """The scouting app sends "" for a field left blank, so "" carries no type information."""
    assert infer_columns({"defense_qata": ""}) == {"defense_qata": UNKNOWN}


def test_a_blank_numeric_field_still_matches_a_filled_one():
    """auto_climb: "" must not fork a new table away from auto_climb: 3."""
    blank = infer_columns({"auto_climb": ""})
    filled = infer_columns({"auto_climb": 3})
    assert compatible(blank, filled) is True


def test_whitespace_only_is_also_blank():
    assert infer_columns({"notes": "   "}) == {"notes": UNKNOWN}


def test_a_blank_field_is_declared_without_affinity():
    assert '"defense_qata" BLOB' in create_table_sql("t", infer_columns({"defense_qata": ""}))


# --- load_record ----------------------------------------------------

def test_loads_a_bare_json_object(tmp_path):
    path = tmp_path / "sub.json"
    path.write_text('{"team_number": 8020}')
    assert load_record(path) == {"team_number": 8020}


def test_loads_a_single_element_array(tmp_path):
    """burner.json is wrapped in an array; one record either way."""
    path = tmp_path / "sub.json"
    path.write_text('[{"team_number": 8020}]')
    assert load_record(path) == {"team_number": 8020}


def test_rejects_a_file_holding_more_than_one_record(tmp_path):
    path = tmp_path / "sub.json"
    path.write_text('[{"team_number": 8020}, {"team_number": 4414}]')
    with pytest.raises(RecordError, match="2 records"):
        load_record(path)


def test_rejects_an_empty_array(tmp_path):
    path = tmp_path / "sub.json"
    path.write_text("[]")
    with pytest.raises(RecordError, match="no records"):
        load_record(path)


def test_rejects_a_top_level_scalar(tmp_path):
    path = tmp_path / "sub.json"
    path.write_text('"just a string"')
    with pytest.raises(RecordError, match="object"):
        load_record(path)


def test_malformed_json_names_the_file(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json at all}")
    with pytest.raises(RecordError, match="broken.json"):
        load_record(path)


def test_missing_file_names_the_file(tmp_path):
    with pytest.raises(RecordError, match="ghost.json"):
        load_record(tmp_path / "ghost.json")


# --- describe_mismatch ----------------------------------------------

def test_compatible_schemas_have_nothing_to_describe():
    columns = {"team": "INTEGER"}
    assert describe_mismatch(columns, dict(columns)) == []


def test_describes_an_added_field():
    reasons = describe_mismatch({"team": "INTEGER"}, {"team": "INTEGER", "pickup": "TEXT"})
    assert reasons == ["added field: pickup"]


def test_describes_a_removed_field():
    reasons = describe_mismatch({"team": "INTEGER", "pickup": "TEXT"}, {"team": "INTEGER"})
    assert reasons == ["missing field: pickup"]


def test_describes_a_type_conflict():
    reasons = describe_mismatch({"accuracy": "REAL"}, {"accuracy": "TEXT"})
    assert reasons == ["accuracy: REAL vs TEXT"]


def test_a_blank_field_is_never_reported_as_a_conflict():
    assert describe_mismatch({"auto_climb": UNKNOWN}, {"auto_climb": "INTEGER"}) == []


# --- parse_schema_sql -----------------------------------------------

def test_parses_back_the_ddl_we_generate():
    sql = create_table_sql("scouting_v1", {"team": "INTEGER", "notes": "TEXT"})
    assert parse_schema_sql(sql) == {"scouting_v1": {"team": "INTEGER", "notes": "TEXT"}}


def test_parses_the_existing_unquoted_style_with_constraints():
    """Matches the hand-written Database/2026/Schemas/.../students.sql already in the repo."""
    sql = "CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
    assert parse_schema_sql(sql) == {"students": {"name": "TEXT"}}


def test_parses_several_tables_from_one_file():
    sql = (
        create_table_sql("scouting_v1", {"team": "INTEGER"})
        + "\n\n"
        + create_table_sql("scouting_v2", {"team": "INTEGER", "pickup": "TEXT"})
    )
    assert parse_schema_sql(sql) == {
        "scouting_v1": {"team": "INTEGER"},
        "scouting_v2": {"team": "INTEGER", "pickup": "TEXT"},
    }


def test_empty_sql_text_has_no_tables():
    assert parse_schema_sql("") == {}


# --- choose_table ---------------------------------------------------

def test_names_the_first_version_when_nothing_exists():
    result = choose_table({"team": "INTEGER"}, {}, "scouting")
    assert (result.table_name, result.created) == ("scouting_v1", True)


def test_a_matching_shape_reuses_the_existing_table():
    existing = {"scouting_v1": {"team": "INTEGER"}}
    result = choose_table({"team": "INTEGER"}, existing, "scouting")
    assert (result.table_name, result.created) == ("scouting_v1", False)


def test_a_new_field_names_the_next_version():
    existing = {"scouting_v1": {"team": "INTEGER"}}
    result = choose_table({"team": "INTEGER", "pickup": "TEXT"}, existing, "scouting")
    assert (result.table_name, result.created) == ("scouting_v2", True)
    assert result.reasons == ["added field: pickup"]


def test_a_blank_field_does_not_name_a_new_version():
    existing = {"scouting_v1": {"auto_climb": "INTEGER"}}
    result = choose_table({"auto_climb": UNKNOWN}, existing, "scouting")
    assert (result.table_name, result.created) == ("scouting_v1", False)


def test_an_older_version_is_reused_rather_than_duplicated():
    existing = {
        "scouting_v1": {"team": "INTEGER"},
        "scouting_v2": {"team": "INTEGER", "pickup": "TEXT"},
    }
    result = choose_table({"team": "INTEGER"}, existing, "scouting")
    assert (result.table_name, result.created) == ("scouting_v1", False)


def test_unrelated_tables_are_ignored_when_numbering():
    existing = {"students": {"name": "TEXT"}, "scouting_v1": {"team": "INTEGER"}}
    result = choose_table({"team": "INTEGER", "pickup": "TEXT"}, existing, "scouting")
    assert result.table_name == "scouting_v2"


# --- render_schema_file ---------------------------------------------

def test_renders_every_version_in_order_with_its_reason():
    tables = {
        "scouting_v2": {"team": "INTEGER", "pickup": "TEXT"},
        "scouting_v1": {"team": "INTEGER"},
    }
    text = render_schema_file(tables, "scouting")
    assert text.index("scouting_v1") < text.index("scouting_v2")
    assert "-- scouting_v1: first version" in text
    assert "-- scouting_v2: added field: pickup" in text


def test_the_rendered_file_parses_back_to_the_same_tables():
    tables = {"scouting_v1": {"team": "INTEGER"}, "scouting_v2": {"team": "REAL"}}
    assert parse_schema_sql(render_schema_file(tables, "scouting")) == tables


def test_rendering_nothing_produces_an_empty_file():
    assert render_schema_file({}, "scouting") == ""


# --- numeric widening -----------------------------------------------

def test_integer_and_real_are_the_same_kind_of_field():
    """JSON has one number type: tele_accuracy 1 and 0.9 are the same field."""
    assert compatible({"accuracy": "INTEGER"}, {"accuracy": "REAL"}) is True
    assert compatible({"accuracy": "REAL"}, {"accuracy": "INTEGER"}) is True


def test_widening_does_not_make_numbers_match_text():
    assert compatible({"accuracy": "REAL"}, {"accuracy": "TEXT"}) is False
    assert compatible({"accuracy": "INTEGER"}, {"accuracy": "TEXT"}) is False


def test_a_numeric_difference_is_not_reported_as_a_conflict():
    assert describe_mismatch({"accuracy": "INTEGER"}, {"accuracy": "REAL"}) == []


# --- blank columns keep their values' own types ----------------------

def test_a_blank_column_is_declared_without_affinity():
    """TEXT affinity would rewrite a later number as a string - BLOB stores as given."""
    sql = create_table_sql("t", {"auto_climb": UNKNOWN})
    assert '"auto_climb" BLOB' in sql
    assert '"auto_climb" TEXT' not in sql


def test_a_blank_column_reads_back_as_blank_not_as_text():
    sql = create_table_sql("t", {"auto_climb": UNKNOWN})
    assert parse_schema_sql(sql) == {"t": {"auto_climb": UNKNOWN}}


def test_a_genuine_text_column_stays_text():
    sql = create_table_sql("t", {"notes": "TEXT"})
    assert '"notes" TEXT' in sql
    assert parse_schema_sql(sql) == {"t": {"notes": "TEXT"}}
