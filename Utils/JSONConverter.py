"""Infer a SQLite schema from one JSON record, and decide whether two schemas match.

A submission is one JSON record. Its field types are inferred, then compared against
the tables already in the database to decide whether the record belongs in an
existing table or needs a new one.
"""

import json
import re
from pathlib import Path

#: A field the scout left blank - null, "" or whitespace. Its real type is unknown,
#: so it matches any concrete type. This keeps a blank field from forking a new
#: table: auto_climb "" and auto_climb 3 belong together. See compatible().
UNKNOWN = "UNKNOWN"

#: Rendered in DDL wherever a column's type is still UNKNOWN. BLOB is the one
#: SQLite declaration with no affinity: values are stored exactly as given, so a
#: number arriving later stays a number. TEXT would rewrite it as a string.
DEFAULT_TYPE = "BLOB"

#: JSON has a single number type, so 1 and 0.9 are the same field written two
#: ways. These are interchangeable when matching schemas.
NUMERIC_TYPES = frozenset({"INTEGER", "REAL"})

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RecordError(Exception):
    """Raised when a submission file isn't one readable JSON record."""


def _sql_type(value) -> str:
    """The SQLite type for one JSON value. bool is checked first - it is a subclass of int."""
    if value is None:
        return UNKNOWN
    if isinstance(value, str) and not value.strip():
        return UNKNOWN  # blank field: no type information to go on
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "TEXT"  # strings, and nested dicts/lists stored as JSON text


def infer_columns(record: dict) -> dict[str, str]:
    """Map one JSON record to {column: type}, rejecting names unsafe to put in DDL."""
    columns = {}
    for key, value in record.items():
        if not IDENTIFIER_PATTERN.match(key):
            raise ValueError(f"invalid column name: {key!r}")
        columns[key] = _sql_type(value)
    return columns


def same_kind(a: str, b: str) -> bool:
    """True when two column types can live in one column.

    A blank matches anything, and the two numeric types match each other.
    """
    if a == b or UNKNOWN in (a, b):
        return True
    return a in NUMERIC_TYPES and b in NUMERIC_TYPES


def compatible(a: dict[str, str], b: dict[str, str]) -> bool:
    """True when two schemas can share a table.

    Same field names, and every field agrees wherever both types are known.
    Column order is ignored, since JSON key order carries no meaning.
    """
    if a.keys() != b.keys():
        return False
    return all(same_kind(a[k], b[k]) for k in a)


def create_table_sql(table_name: str, columns: dict[str, str]) -> str:
    """Build the CREATE TABLE for one inferred schema, with our own id primary key."""
    if not IDENTIFIER_PATTERN.match(table_name):
        raise ValueError(f"invalid table name: {table_name!r}")

    lines = ["id INTEGER PRIMARY KEY"]
    for name, sql_type in columns.items():
        lines.append(f'"{name}" {DEFAULT_TYPE if sql_type == UNKNOWN else sql_type}')

    body = ",\n    ".join(lines)
    return f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n    {body}\n);'


def load_record(path) -> dict:
    """Read a submission file and return its single record.

    Accepts a bare JSON object or a one-element array, since exports wrap records
    either way. Anything else is refused with a message naming the file.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise RecordError(f"no such submission file: {path}") from None
    except json.JSONDecodeError as err:
        raise RecordError(f"{path.name} is not valid JSON: {err}") from None

    if isinstance(data, list):
        if not data:
            raise RecordError(f"{path.name} holds no records")
        if len(data) > 1:
            raise RecordError(
                f"{path.name} holds {len(data)} records; one submission is one record"
            )
        data = data[0]

    if not isinstance(data, dict):
        raise RecordError(f"{path.name} must hold a JSON object, got {type(data).__name__}")
    return data


def describe_mismatch(a: dict[str, str], b: dict[str, str]) -> list[str]:
    """Explain why two schemas can't share a table. Empty when they can.

    Says which fields appeared, which went missing, and which types genuinely
    conflict - so a new version table can record why it was created.
    """
    reasons = []
    for key in b.keys() - a.keys():
        reasons.append(f"added field: {key}")
    for key in a.keys() - b.keys():
        reasons.append(f"missing field: {key}")
    for key in a.keys() & b.keys():
        if not same_kind(a[key], b[key]):
            reasons.append(f"{key}: {a[key]} vs {b[key]}")
    return sorted(reasons)


#: One CREATE TABLE statement, capturing the table name and the column list.
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`]?(\w+)[\"`]?\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)


def parse_schema_sql(sql_text: str) -> dict[str, dict[str, str]]:
    """Read stored .sql text back into {table: {column: type}}.

    Our own id primary key is dropped, so the result lines up with what
    infer_columns produces for a submission.
    """
    tables = {}
    for table_name, body in _CREATE_TABLE.findall(sql_text):
        columns = {}
        for part in body.split(","):
            tokens = part.strip().split()
            if len(tokens) < 2:
                continue
            name = tokens[0].strip('"`')
            if "PRIMARY" in (t.upper() for t in tokens[1:]):
                continue  # our own id column, not part of the submission
            declared = tokens[1].upper()
            columns[name] = UNKNOWN if declared == DEFAULT_TYPE else declared
        tables[table_name] = columns
    return tables


class SchemaResult:
    """Which table a submission belongs in, and whether it had to be created."""

    def __init__(self, table_name: str, created: bool, reasons: list[str]):
        self.table_name = table_name
        self.created = created
        self.reasons = reasons

    def __repr__(self) -> str:
        verb = "created" if self.created else "matched"
        return f"<SchemaResult {verb} {self.table_name}>"


def version_order(tables, base_name: str) -> list[str]:
    """The <base_name>_vN tables among `tables`, in version order."""
    return sorted(
        (name for name in tables if re.fullmatch(rf"{re.escape(base_name)}_v\d+", name)),
        key=lambda name: int(name.rsplit("_v", 1)[1]),
    )


def choose_table(columns: dict[str, str], existing: dict, base_name: str) -> SchemaResult:
    """Pick the table these columns belong in, naming a new version if none fits.

    Pure: `existing` is {table: {column: type}} read from wherever the caller
    considers authoritative. Decides only - creates nothing.
    """
    if not IDENTIFIER_PATTERN.match(base_name):
        raise ValueError(f"invalid table name: {base_name!r}")

    versions = version_order(existing, base_name)
    for name in versions:
        if compatible(columns, existing[name]):
            return SchemaResult(name, created=False, reasons=[])

    reasons = describe_mismatch(existing[versions[-1]], columns) if versions else []
    return SchemaResult(f"{base_name}_v{len(versions) + 1}", created=True, reasons=reasons)


def render_schema_file(tables: dict[str, dict[str, str]], base_name: str) -> str:
    """Rebuild the whole .sql file from the tables that actually exist.

    Derived entirely from its argument, so the file can be regenerated at any
    time from the database rather than being appended to and drifting.
    """
    parts = []
    previous = None
    for name in version_order(tables, base_name):
        columns = tables[name]
        reasons = describe_mismatch(previous, columns) if previous is not None else []
        why = "; ".join(reasons) if reasons else ("first version" if previous is None else "no change")
        parts.append(f"-- {name}: {why}\n{create_table_sql(name, columns)}")
        previous = columns
    return "\n\n".join(parts) + ("\n" if parts else "")
