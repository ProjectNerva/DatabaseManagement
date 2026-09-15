"""Drains the outbox into the Pi, and decides what a response means.

The retry policy is the whole point of this module. On a wired switch a dead link
is dead for a human reason - a foot on the cable, a switch unplugged - so the
backoff ceiling is short: when someone fixes it, the backlog should flush in
seconds rather than wait out a long timer.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from itertools import groupby

import httpx

from uploader.outbox import Outbox, Queued

#: 1s, 2s, 4s, then every 5s. Polling a wired peer this often costs nothing.
BACKOFF = (1.0, 2.0, 4.0, 5.0)

#: Must not exceed the server's MAX_BATCH.
BATCH_SIZE = 50


@dataclass
class Config:
    server: str
    token: str
    outbox: str
    device_id: str = "scout"
    timeout: float = 10.0


@dataclass
class Attempt:
    """What one pass over the queue achieved, for logging and for status."""

    stored: int = 0
    duplicate: int = 0
    rejected: int = 0
    unreachable: bool = False
    error: str | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def progressed(self) -> bool:
        return bool(self.stored or self.duplicate or self.rejected)


class Sender:
    def __init__(self, config: Config, client: httpx.Client | None = None) -> None:
        self.config = config
        self.outbox = Outbox(config.outbox)
        self._client = client or httpx.Client(timeout=config.timeout)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.config.token}"}

    def reachable(self) -> tuple[bool, str]:
        try:
            r = self._client.get(f"{self.config.server}/api/v1/health")
            return (r.status_code == 200, f"HTTP {r.status_code}")
        except httpx.HTTPError as err:
            return (False, type(err).__name__)

    def tables(self, year: int, competition: str) -> dict:
        r = self._client.get(
            f"{self.config.server}/api/v1/tables/{year}/{competition}", headers=self._headers
        )
        r.raise_for_status()
        return r.json()["tables"]

    def drain_once(self) -> Attempt:
        """One pass: send every pending record we can, grouped by destination."""
        attempt = Attempt()
        pending = self.outbox.pending()
        if not pending:
            return attempt

        for _, items in groupby(sorted(pending, key=lambda q: q.group), key=lambda q: q.group):
            batch = list(items)
            for i in range(0, len(batch), BATCH_SIZE):
                if not self._send(batch[i : i + BATCH_SIZE], attempt):
                    return attempt  # unreachable or server-side stop; back off
        return attempt

    def _send(self, batch: list[Queued], attempt: Attempt) -> bool:
        """Send one batch. False means stop this pass and back off."""
        first = batch[0]
        body = {
            "year": first.year,
            "competition": first.competition,
            "base_name": first.base_name,
            "records": [{"submission_id": q.submission_id, "data": q.data} for q in batch],
        }
        try:
            r = self._client.post(
                f"{self.config.server}/api/v1/submit", json=body, headers=self._headers
            )
        except httpx.HTTPError as err:
            attempt.unreachable = True
            attempt.error = f"{type(err).__name__}: {err}"
            return False

        if r.status_code == 413:
            # Batch too large for this server. Halving and retrying beats giving up.
            if len(batch) == 1:
                self.outbox.reject(first.path)
                attempt.rejected += 1
                return True
            half = len(batch) // 2
            return self._send(batch[:half], attempt) and self._send(batch[half:], attempt)

        if r.status_code in (401, 404):
            # Config is wrong, not the network. Hold everything and say so loudly;
            # retrying forever would bury the one message that explains the problem.
            attempt.error = f"HTTP {r.status_code}: {r.text[:200]}"
            return False

        if r.status_code != 200:
            attempt.error = f"HTTP {r.status_code}: {r.text[:200]}"
            return False

        by_id = {q.submission_id: q for q in batch}
        for result in r.json()["results"]:
            queued = by_id.get(result["submission_id"])
            if queued is None:
                continue
            status = result["status"]
            if status in ("stored", "duplicate"):
                self.outbox.accept(queued.path)
                setattr(attempt, status, getattr(attempt, status) + 1)
                where = f"{result['table']} row {result['row_id']}"
                attempt.lines.append(f"{status:9} {queued.path.name} -> {where}")
            else:
                self.outbox.reject(queued.path)
                attempt.rejected += 1
                attempt.lines.append(f"rejected  {queued.path.name} -> {result.get('error')}")
        return True

    def run(self, once: bool = False, sleep=time.sleep) -> None:
        """Watch the outbox until interrupted, backing off when the Pi is unreachable."""
        misses = 0
        while True:
            attempt = self.drain_once()
            for line in attempt.lines:
                print(line, flush=True)
            if attempt.error:
                print(f"! {attempt.error}", flush=True)

            if attempt.unreachable or attempt.error:
                misses += 1
            else:
                misses = 0

            if once:
                return

            delay = BACKOFF[min(misses, len(BACKOFF) - 1)] if misses else BACKOFF[0]
            # Jitter keeps six uploaders that lost the same switch from re-converging
            # on the Pi in lockstep the instant it comes back.
            sleep(delay * random.uniform(0.8, 1.2))
