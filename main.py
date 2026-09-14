"""Exercises CompetitionRegistrar and DatabaseManager against this repo's Database/ folder.

Wipes Database/ on every run, rebuilds it from scratch, then submits burner.json
into 2026/2026curie and shows how schema drift routes records into version tables.
"""

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from CompetitionRegistrar import (
    CompetitionExists,
    CompetitionNotFound,
    CompetitionRegistrar,
    RegistrarError,
)
from DatabaseManager import DatabaseManager
from Utils.JSONConverter import RecordError, load_record

ROOT = Path(__file__).parent / "Database"
BURNER = Path(__file__).parent / "burner.json"
YEAR, COMPETITION = 2026, "2026curie"


def section(title):
    print(f"\n=== {title} ===")


def show_tree(root):
    for path in sorted(root.rglob("*")):
        depth = len(path.relative_to(root).parts) - 1
        print(f"  {'  ' * depth}{path.name}{'/' if path.is_dir() else ''}")


def clear_database(root):
    """Delete everything under Database/ so each run starts from nothing."""
    if not root.exists():
        print("  nothing to clear")
        return
    for entry in sorted(root.iterdir()):
        print(f"  removing {entry.relative_to(root.parent)}")
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()


def variant_of(record, **changes):
    """A copy of the burner record with fields changed, for the drift demo."""
    return {**record, **changes}


def main():
    registrar = CompetitionRegistrar(ROOT)
    manager = DatabaseManager(registrar)
    print(f"database root: {ROOT}")

    section("clear Database/")
    clear_database(ROOT)

    section("create competitions")
    registrar.create(YEAR, COMPETITION)
    registrar.create(YEAR, "2026dcmp")
    registrar.create(2027, "2027worlds")
    print(f"  created {YEAR}/{COMPETITION}, {YEAR}/2026dcmp, 2027/2027worlds")

    section("path resolution")
    comp = registrar.resolve(YEAR, COMPETITION)
    print(f"  competition: {comp.directory.relative_to(ROOT)}")
    print(f"  db_path:     {comp.db_path.relative_to(ROOT)}")
    print(f"  schemas:     {comp.schemas_dir.relative_to(ROOT)}")

    section("discovery")
    print(f"  list_years:        {registrar.list_years()}")
    print(f"  list_competitions: {[(c.year, c.slug) for c in registrar.list_competitions()]}")
    print(f"  exists({COMPETITION}):  {registrar.exists(YEAR, COMPETITION)}")
    print(f"  exists(missing):   {registrar.exists(YEAR, 'missing')}")
    latest = registrar.latest()
    print(f"  latest:            {latest.year}/{latest.slug}")

    section("identifier validation")
    for bad in ["../etc/passwd", "2026; DROP TABLE students", "has space", ""]:
        try:
            registrar.db_path(YEAR, bad)
            print(f"  LEAKED: {bad!r}")
        except ValueError as err:
            print(f"  rejected {bad!r}: {err}")

    section("error guards")
    try:
        registrar.create(YEAR, COMPETITION)
    except CompetitionExists as err:
        print(f"  duplicate create:   {err}")
    try:
        registrar.delete(YEAR, "2026dcmp")
    except RegistrarError as err:
        print(f"  unconfirmed delete: {err}")
    try:
        registrar.delete(YEAR, "ghost", confirm=True)
    except CompetitionNotFound as err:
        print(f"  missing delete:     {err}")
    try:
        manager.submit(YEAR, "ghost", "scouting", BURNER)
    except CompetitionNotFound as err:
        print(f"  submit to missing:  {err}")

    section(f"submit burner.json into {YEAR}/{COMPETITION}")
    result = manager.submit(YEAR, COMPETITION, "scouting", BURNER)
    print(f"  burner.json -> {result}")

    section("schema drift: three more submissions")
    record = load_record(BURNER)
    drifted = [
        ("same shape, different team", variant_of(record, team_number=4414, match_number=12)),
        ("scout left two fields blank", variant_of(record, match_number=13, auto_climb="", defense_qata="")),
        ("app added auto_pickup", variant_of(record, match_number=14, auto_pickup=3)),
        ("older app, original shape", variant_of(record, team_number=1234, match_number=15)),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for label, rec in drifted:
            path = Path(tmp) / f"match_{rec['match_number']}.json"
            path.write_text(json.dumps(rec))
            print(f"  {label:<28} -> {manager.submit(YEAR, COMPETITION, 'scouting', path)}")

    section("bad submissions are refused")
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in [
            ("broken.json", "{not json at all}"),
            ("two_records.json", '[{"a": 1}, {"a": 2}]'),
            ("scalar.json", '"just a string"'),
        ]:
            path = Path(tmp) / name
            path.write_text(text)
            try:
                manager.submit(YEAR, COMPETITION, "scouting", path)
                print(f"  LEAKED: {name}")
            except RecordError as err:
                print(f"  rejected {name}: {err}")

    section("what landed in the database")
    for table, count in manager.tables(YEAR, COMPETITION).items():
        print(f"  {table}: {count} rows")

    conn = sqlite3.connect(comp.db_path)
    try:
        print("\n  scouting_v1 (match_number, team_number, auto_climb, defense_qata):")
        for row in conn.execute(
            "SELECT match_number, team_number, auto_climb, defense_qata FROM scouting_v1"
        ):
            print(f"    {row}")
        print("\n  scouting_v2 (match_number, team_number, auto_pickup):")
        for row in conn.execute(
            "SELECT match_number, team_number, auto_pickup FROM scouting_v2"
        ):
            print(f"    {row}")
        print(f"\n  journal_mode: {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
    finally:
        conn.close()

    section("stored schema file")
    schema_file = comp.schemas_dir / "scouting.sql"
    for line in schema_file.read_text().splitlines():
        if line.startswith("--") or line.startswith("CREATE"):
            print(f"  {line}")

    section("delete a competition")
    registrar.delete(2027, "2027worlds", confirm=True)
    print(f"  remaining: {[(c.year, c.slug) for c in registrar.list_competitions()]}")

    section("final tree (left on disk)")
    show_tree(ROOT)


if __name__ == "__main__":
    main()
