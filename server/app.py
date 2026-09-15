"""HTTP front door for the competition databases.

One endpoint matters: POST /api/v1/submit. Everything else exists so a scout with
no screen to look at can answer "did mine go in?" from a terminal.

Records are stored one at a time, each with its own outcome in the response, so a
single malformed record can never cost the other five robots their match.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from CompetitionRegistrar import CompetitionNotFound, CompetitionRegistrar
from DatabaseManager import DatabaseManager
from Utils.JSONConverter import RecordError

#: Cap on one batch. A scout back from a long outage still flushes quickly, but no
#: single request can hold the write lock while the other five robots wait.
MAX_BATCH = 50


class RecordIn(BaseModel):
    submission_id: str = Field(min_length=1, max_length=200)
    data: dict


class SubmitIn(BaseModel):
    year: int
    competition: str
    base_name: str = "scouting"
    records: list[RecordIn]


class ResultOut(BaseModel):
    submission_id: str
    status: str
    table: str | None = None
    row_id: int | None = None
    error: str | None = None


class SubmitOut(BaseModel):
    results: list[ResultOut]


def create_app(root: Path | str, token: str) -> FastAPI:
    """Build the app over one Database/ root, authenticated by one shared token."""
    registrar = CompetitionRegistrar(Path(root))
    manager = DatabaseManager(registrar)

    def require_token(authorization: str = Header(default="")) -> None:
        supplied = authorization.removeprefix("Bearer ").strip()
        if not supplied or supplied != token:
            raise HTTPException(401, "bad or missing event token")

    app = FastAPI(title="Scouting Upload", version="1.0")
    api = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])

    @app.get("/api/v1/health")
    def health() -> dict:
        """Unauthenticated on purpose: a scout must be able to tell 'the Pi is down'
        from 'my token is wrong' without already having a working token."""
        comps = [{"year": c.year, "slug": c.slug} for c in registrar.list_competitions()]
        return {"status": "ok", "competitions": comps}

    @api.get("/competitions")
    def competitions() -> dict:
        return {
            "competitions": [
                {"year": c.year, "slug": c.slug} for c in registrar.list_competitions()
            ]
        }

    @api.get("/tables/{year}/{slug}")
    def tables(year: int, slug: str) -> dict:
        try:
            return {"year": year, "competition": slug, "tables": manager.tables(year, slug)}
        except CompetitionNotFound as err:
            raise HTTPException(404, str(err)) from None
        except ValueError as err:
            raise HTTPException(400, str(err)) from None

    @api.post("/submit", response_model=SubmitOut)
    def submit(body: SubmitIn) -> SubmitOut:
        if len(body.records) > MAX_BATCH:
            raise HTTPException(413, f"batch of {len(body.records)} exceeds {MAX_BATCH}")

        # A missing competition is a property of the request, not of any one record:
        # fail the whole batch so the uploader holds everything and alerts, rather
        # than marking 50 records individually rejected for one config mistake.
        if not registrar.exists(body.year, body.competition):
            raise HTTPException(404, f"{body.year}/{body.competition} does not exist")

        results = []
        for item in body.records:
            try:
                outcome = manager.submit_record(
                    body.year,
                    body.competition,
                    body.base_name,
                    item.data,
                    submission_id=item.submission_id,
                )
            except (RecordError, ValueError) as err:
                # The record itself is malformed. Retrying will never fix it.
                results.append(
                    ResultOut(submission_id=item.submission_id, status="rejected", error=str(err))
                )
                continue
            except sqlite3.OperationalError as err:
                # Lock held past the busy timeout. Everything after this would queue
                # behind the same contention, so stop and let the client back off.
                raise HTTPException(503, f"database busy: {err}") from None

            results.append(
                ResultOut(
                    submission_id=item.submission_id,
                    status="duplicate" if outcome.duplicate else "stored",
                    table=outcome.table_name,
                    row_id=outcome.row_id,
                )
            )
        return SubmitOut(results=results)

    app.include_router(api)
    return app
