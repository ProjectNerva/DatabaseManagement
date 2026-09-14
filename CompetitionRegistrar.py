from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DATA_DIRNAME = "Data"
SCHEMAS_DIRNAME = "Schemas"

SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

class RegistrarError(Exception):
    """Base class for every error this module raises."""


class CompetitionExists(RegistrarError):
    """Raised when creating a competition whose database file is already on disk."""


class CompetitionNotFound(RegistrarError):
    """Raised when a competition or schema file was expected on disk but is missing."""


@dataclass(frozen=True)
class Competition:
    """Every path belonging to one competition, resolved in one place."""

    year: int
    slug: str
    directory: Path
    db_path: Path
    schemas_dir: Path


class CompetitionRegistrar:
    """Owns the Database/ tree: resolves paths, creates and deletes competitions.

    Layout:
        Database/<year>/Data/<slug>/<slug>.db
        Database/<year>/Schemas/<slug>/*.sql
    """

    def __init__(self, root: Path | str = "Database") -> None:
        """Point the registrar at the Database/ root folder it should manage."""
        self.root = Path(root)

    # --- validation -----------------------------------------------------

    @staticmethod
    def _validate_slug(slug: str) -> str:
        """Reject any name that could escape the folder tree (no dots, slashes or spaces)."""
        if not SLUG_PATTERN.match(slug):
            raise ValueError(f"invalid competition name: {slug!r}")
        return slug

    # --- path resolution ------------------------------------------------

    def year_dir(self, year: int) -> Path:
        """Database/<year>/ — the folder holding one season's data and schemas."""
        return self.root / str(int(year))

    def data_dir(self, year: int) -> Path:
        """Database/<year>/Data/ — parent of every competition folder for that year."""
        return self.year_dir(year) / DATA_DIRNAME

    def schemas_dir(self, year: int) -> Path:
        """Database/<year>/Schemas/ — parent of every competition's schema folder."""
        return self.year_dir(year) / SCHEMAS_DIRNAME

    def competition_dir(self, year: int, slug: str) -> Path:
        """Database/<year>/Data/<slug>/ — one competition's own folder."""
        return self.data_dir(year) / self._validate_slug(slug)

    def db_path(self, year: int, slug: str) -> Path:
        """The SQLite file itself: Data/<slug>/<slug>.db."""
        slug = self._validate_slug(slug)
        return self.competition_dir(year, slug) / f"{slug}.db"

    def competition_schemas_dir(self, year: int, slug: str) -> Path:
        """Schemas/<slug>/ — where this competition's .sql files are kept."""
        return self.schemas_dir(year) / self._validate_slug(slug)

    def resolve(self, year: int, slug: str) -> Competition:
        """Bundle all of a competition's paths into a Competition. Computes only, touches no disk."""
        return Competition(
            year=int(year),
            slug=self._validate_slug(slug),
            directory=self.competition_dir(year, slug),
            db_path=self.db_path(year, slug),
            schemas_dir=self.competition_schemas_dir(year, slug),
        )

    # --- discovery ------------------------------------------------------

    def list_years(self) -> list[int]:
        """Every year folder currently under the root, ascending."""
        if not self.root.exists():
            return []
        years = [int(p.name) for p in self.root.iterdir() if p.is_dir() and p.name.isdigit()]
        return sorted(years)

    def list_competitions(self, year: int | None = None) -> list[Competition]:
        """Every competition found on disk, or just those in one year."""
        years = [year] if year is not None else self.list_years()
        found = []
        for y in years:
            data = self.data_dir(y)
            if not data.exists():
                continue
            for entry in sorted(data.iterdir()):
                if entry.is_dir() and SLUG_PATTERN.match(entry.name):
                    found.append(self.resolve(y, entry.name))
        return found

    def exists(self, year: int, slug: str) -> bool:
        """True when this competition's .db file is actually on disk."""
        return self.db_path(year, slug).exists()

    def latest(self) -> Competition | None:
        """Newest competition by year, then db mtime — the default to clone a schema from."""
        competitions = self.list_competitions()
        if not competitions:
            return None
        return max(competitions, key=lambda c: (c.year, c.db_path.stat().st_mtime))

    # --- lifecycle ------------------------------------------------------

    def create(self, year: int, slug: str, schemas: dict[str, str] | None = None) -> Competition:
        """Make both folders and an empty WAL-mode db, saving any schemas as files beside it.

        Schemas are stored, not executed — see apply_schemas.
        """
        comp = self.resolve(year, slug)
        if comp.db_path.exists():
            raise CompetitionExists(f"{comp.year}/{comp.slug} already exists")

        comp.directory.mkdir(parents=True, exist_ok=True)
        comp.schemas_dir.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(comp.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()

        for name, sql in (schemas or {}).items():
            self.write_schema(comp.year, comp.slug, name, sql)

        return comp

    def delete(self, year: int, slug: str, confirm: bool = False) -> None:
        """Remove a competition's data and schema folders. Irreversible, so confirm=True is required."""
        comp = self.resolve(year, slug)
        if not comp.directory.exists():
            raise CompetitionNotFound(f"{comp.year}/{comp.slug} does not exist")
        if not confirm:
            raise RegistrarError("refusing to delete without confirm=True")

        shutil.rmtree(comp.directory)
        if comp.schemas_dir.exists():
            shutil.rmtree(comp.schemas_dir)

    # --- schemas --------------------------------------------------------
    def write_schema(self, year: int, slug: str, name: str, sql: str) -> Path:
        """Save one table's SQL as Schemas/<slug>/<name>.sql, overwriting any existing file."""
        comp = self.resolve(year, slug)
        self._validate_slug(name)
        comp.schemas_dir.mkdir(parents=True, exist_ok=True)
        path = comp.schemas_dir / f"{name}.sql"
        path.write_text(sql)
        return path

    def list_schemas(self, year: int, slug: str) -> list[Path]:
        """Paths of every .sql file this competition has, sorted by name."""
        schemas_dir = self.competition_schemas_dir(year, slug)
        if not schemas_dir.exists():
            return []
        return sorted(schemas_dir.glob("*.sql"))

    def read_schema(self, year: int, slug: str, name: str) -> str:
        """Return the raw SQL text of one schema file."""
        path = self.competition_schemas_dir(year, slug) / f"{self._validate_slug(name)}.sql"
        if not path.exists():
            raise CompetitionNotFound(f"no schema {name!r} for {year}/{slug}")
        return path.read_text()
