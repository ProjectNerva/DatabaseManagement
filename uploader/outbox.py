"""The durable queue on a scouting machine.

A directory of JSON files, deliberately: the scouting app needs one open() to add
work, and a record survives a crash, a reboot, or the uploader never having been
started. A record is safe once it is here - not when the server acknowledges it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: Written by the scouting app, read by the uploader, never modified in place.
REQUIRED_FIELDS = ("submission_id", "year", "competition", "data")


class OutboxError(Exception):
    """A queued file is not a submission we can send."""


@dataclass(frozen=True)
class Queued:
    """One pending submission, and the file it came from."""

    path: Path
    submission_id: str
    year: int
    competition: str
    base_name: str
    data: dict

    @property
    def group(self) -> tuple[int, str, str]:
        """Records batch together only when bound for the same table of the same db."""
        return (self.year, self.competition, self.base_name)


class Outbox:
    """pending / sent / rejected, as three directories."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.pending_dir = self.root / "outbox"
        self.sent_dir = self.root / "sent"
        self.rejected_dir = self.root / "rejected"
        for d in (self.pending_dir, self.sent_dir, self.rejected_dir):
            d.mkdir(parents=True, exist_ok=True)

    # --- writing (the scouting app's side) -------------------------------

    def add(
        self,
        year: int,
        competition: str,
        data: dict,
        base_name: str = "scouting",
        submission_id: str | None = None,
    ) -> Path:
        """Queue one record. The id is minted here, at capture time, on purpose.

        Minting it at send time instead would produce a fresh id on every retry and
        defeat the server's duplicate check entirely.
        """
        submission_id = submission_id or str(uuid.uuid4())
        payload = {
            "submission_id": submission_id,
            "year": year,
            "competition": competition,
            "base_name": base_name,
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "data": data,
        }
        match = data.get("match_number", "x")
        team = data.get("team_number", "x")
        path = self.pending_dir / f"{competition}-q{match}-{team}-{submission_id[:8]}.json"

        # Write to a temp file and rename, so the uploader can never pick up a file
        # the scouting app is still halfway through writing.
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2))
        temp.replace(path)
        return path

    # --- reading (the uploader's side) -----------------------------------

    def pending(self) -> list[Queued]:
        """Everything waiting, oldest first. Unreadable files are moved aside, not skipped
        forever: a file that can never be parsed would otherwise be retried until the
        end of the event."""
        out = []
        for path in sorted(self.pending_dir.glob("*.json")):
            try:
                out.append(self._load(path))
            except OutboxError:
                self.reject(path)
        return out

    @staticmethod
    def _load(path: Path) -> Queued:
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as err:
            raise OutboxError(f"{path.name} is not valid JSON: {err}") from None
        if not isinstance(payload, dict):
            raise OutboxError(f"{path.name} must hold a JSON object")
        missing = [f for f in REQUIRED_FIELDS if f not in payload]
        if missing:
            raise OutboxError(f"{path.name} is missing {', '.join(missing)}")
        if not isinstance(payload["data"], dict):
            raise OutboxError(f"{path.name}: data must be a JSON object")
        return Queued(
            path=path,
            submission_id=str(payload["submission_id"]),
            year=int(payload["year"]),
            competition=str(payload["competition"]),
            base_name=str(payload.get("base_name", "scouting")),
            data=payload["data"],
        )

    # --- outcomes ---------------------------------------------------------

    def accept(self, path: Path) -> None:
        """Stored or duplicate - both mean the Pi has it."""
        self._move(path, self.sent_dir)

    def reject(self, path: Path) -> None:
        """Malformed beyond retrying. Kept, never deleted: a human decides."""
        self._move(path, self.rejected_dir)

    def requeue_rejected(self) -> int:
        """Send everything in rejected/ back to the queue, after someone fixed it."""
        moved = 0
        for path in sorted(self.rejected_dir.glob("*.json")):
            self._move(path, self.pending_dir)
            moved += 1
        return moved

    @staticmethod
    def _move(path: Path, into: Path) -> None:
        into.mkdir(parents=True, exist_ok=True)
        path.replace(into / path.name)

    def counts(self) -> dict[str, int]:
        return {
            "pending": len(list(self.pending_dir.glob("*.json"))),
            "sent": len(list(self.sent_dir.glob("*.json"))),
            "rejected": len(list(self.rejected_dir.glob("*.json"))),
        }
