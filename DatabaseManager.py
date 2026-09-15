"""Loads JSON submissions into a competition's database.

One submission is one JSON record. Its shape decides which table it lands in:
a record matching a table already declared for this competition is inserted
there and nothing else changes; a record with a genuinely new shape gets a new
<base_name>_vN table, created in the database and appended to the schema file.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from CompetitionRegistrar import CompetitionNotFound, CompetitionRegistrar
from Utils.JSONConverter import (
    DEFAULT_TYPE,
    UNKNOWN,
    choose_table,
    create_table_sql,
    infer_columns,
    load_record,
    render_schema_file,
)

#: How long a submission waits for another writer's lock before giving up.
BUSY_TIMEOUT_SECONDS = 30.0

#: Ledger of already-applied submission ids. Not scouting data: it is hidden from
#: table discovery so it can never be offered to choose_table as a record shape.
LEDGER_TABLE = "_submissions"

_CREATE_LEDGER = f"""
CREATE TABLE IF NOT EXISTS "{LEDGER_TABLE}" (
    submission_id TEXT PRIMARY KEY,
    table_name    TEXT NOT NULL,
    row_id        INTEGER NOT NULL,
    received_at   TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Submission:
    """What happened to one submitted record."""

    table_name: str
    created: bool
    row_id: int
    reasons: list[str] = field(default_factory=list)
    duplicate: bool = False

    def __str__(self) -> str:
        if self.duplicate:
            return f"already stored in {self.table_name}, row {self.row_id}"
        verb = "created" if self.created else "matched"
        why = f" ({'; '.join(self.reasons)})" if self.reasons else ""
        return f"{verb} {self.table_name}, row {self.row_id}{why}"


def _now() -> str:
    """UTC timestamp for the ledger. The Pi's clock is often wrong; UTC at least makes
    two rows comparable to each other."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _storable(value):
    """Convert one JSON value to something SQLite can store.

    Blank fields become NULL rather than "", so a blank never lands as text in a
    numeric column. Nested values are stored as JSON text.
    """
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, str) and not value.strip():
        return None
    return value


class DatabaseManager:
    """Applies schemas and inserts records for the competitions a registrar owns."""

    def __init__(self, registrar: CompetitionRegistrar) -> None:
        self.registrar = registrar

    def submit(
        self,
        year: int,
        slug: str,
        base_name: str,
        json_path: Path | str,
        submission_id: str | None = None,
    ) -> Submission:
        """Load one JSON record from a file and store it. See submit_record."""
        return self.submit_record(
            year, slug, base_name, load_record(json_path), submission_id=submission_id
        )

    def submit_record(
        self,
        year: int,
        slug: str,
        base_name: str,
        record: dict,
        submission_id: str | None = None,
    ) -> Submission:
        """Store one already-parsed record in the right version table, creating it if needed.

        Pass submission_id to make the write idempotent: a second call with an id
        already applied stores nothing and reports where the first one landed. The
        id is recorded in the same transaction as the record, so a crash can never
        leave a stored row that a retry would duplicate.
        """
        comp = self.registrar.resolve(year, slug)
        if not comp.db_path.exists():
            raise CompetitionNotFound(f"{comp.year}/{comp.slug} does not exist")

        columns = infer_columns(record)
        comp.schemas_dir.mkdir(parents=True, exist_ok=True)

        conn = self._connect(comp.db_path)
        try:
            # BEGIN IMMEDIATE takes the write lock before we look at anything, so
            # concurrent submissions queue up here instead of racing: each one sees
            # the tables every earlier submission committed.
            conn.execute("BEGIN IMMEDIATE")

            if submission_id is not None:
                conn.execute(_CREATE_LEDGER)
                # Safe to read before writing: we already hold the write lock, so no
                # other submission can insert this id between the check and the insert.
                prior = conn.execute(
                    f'SELECT table_name, row_id FROM "{LEDGER_TABLE}" WHERE submission_id = ?',
                    (submission_id,),
                ).fetchone()
                if prior is not None:
                    conn.execute("ROLLBACK")
                    return Submission(prior[0], False, prior[1], duplicate=True)

            existing = self._table_columns(conn)
            result = choose_table(columns, existing, base_name)

            if result.created:
                conn.execute(create_table_sql(result.table_name, columns))
                existing[result.table_name] = columns

            names = list(record)
            placeholders = ", ".join("?" for _ in names)
            quoted = ", ".join(f'"{name}"' for name in names)
            cursor = conn.execute(
                f'INSERT INTO "{result.table_name}" ({quoted}) VALUES ({placeholders})',
                [_storable(record[name]) for name in names],
            )
            row_id = cursor.lastrowid

            if submission_id is not None:
                # Same transaction as the record above: either both land or neither
                # does, so a crash here cannot produce a row a retry would duplicate.
                conn.execute(
                    f'INSERT INTO "{LEDGER_TABLE}" '
                    "(submission_id, table_name, row_id, received_at) VALUES (?, ?, ?, ?)",
                    (submission_id, result.table_name, row_id, _now()),
                )

            # Written under the same lock, so the file can't be reordered by a
            # slower writer. It is derived from the database, never the reverse.
            self._write_schema_file(comp.schemas_dir / f"{base_name}.sql", existing, base_name)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return Submission(result.table_name, result.created, row_id, result.reasons)

    @staticmethod
    def _connect(db_path: Path) -> sqlite3.Connection:
        """A connection that waits for the write lock instead of failing on contention."""
        conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
        return conn

    @staticmethod
    def _table_columns(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
        """Every table in the database as {table: {column: type}}, straight from SQLite.

        This is the authority on what exists - not the stored .sql file, which is a
        derived artifact and may be missing or stale.

        The submission ledger is excluded: it is bookkeeping, and offering it to
        choose_table would let it be matched as a record shape.
        """
        tables = {}
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != ?",
                (LEDGER_TABLE,),
            )
        ]
        for name in names:
            columns = {}
            for row in conn.execute(f'PRAGMA table_info("{name}")'):
                if row[5]:  # row[5] is pk; our own id column is not part of the record
                    continue
                declared = row[2].upper()
                # A no-affinity column was created from a blank: still no type known.
                columns[row[1]] = UNKNOWN if declared == DEFAULT_TYPE else declared
            tables[name] = columns
        return tables

    @staticmethod
    def _write_schema_file(path: Path, tables: dict, base_name: str) -> None:
        """Rebuild the .sql file atomically, so a reader never sees a half-written file."""
        text = render_schema_file(tables, base_name)
        temp = path.with_suffix(".sql.tmp")
        temp.write_text(text)
        temp.replace(path)

    def tables(self, year: int, slug: str) -> dict[str, int]:
        """Every table in this competition's database and its row count."""
        comp = self.registrar.resolve(year, slug)
        if not comp.db_path.exists():
            raise CompetitionNotFound(f"{comp.year}/{comp.slug} does not exist")

        conn = sqlite3.connect(comp.db_path)
        try:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name != ? ORDER BY name",
                    (LEDGER_TABLE,),
                )
            ]
            return {
                name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                for name in names
            }
        finally:
            conn.close()
