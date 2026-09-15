"""The uploader, including the failure it exists for.

The case worth building carefully: the server commits, the link drops before the
response arrives, and the uploader retries. Exactly one row must exist afterwards.
A single scouting laptop will never reproduce this by accident, so it is forced here.
"""

import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from CompetitionRegistrar import CompetitionRegistrar
from DatabaseManager import DatabaseManager
from server.app import create_app
from uploader.outbox import Outbox
from uploader.sender import Config, Sender

TOKEN = "event-token"


class DropAfterCommit(httpx.BaseTransport):
    """Lets the request reach the app, then destroys the response.

    This is precisely the ambiguous case: the write happened, and the client has no
    way to know it. A transport that failed *before* delegating would test nothing.
    """

    def __init__(self, inner, drops=1):
        self.inner = inner
        self.drops = drops
        self.seen = 0

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        if request.url.path.endswith("/submit"):
            self.seen += 1
            if self.drops > 0:
                self.drops -= 1
                response.read()  # the app has now really run and committed
                raise httpx.ReadTimeout("link dropped after commit", request=request)
        return response


@pytest.fixture
def rig(tmp_path):
    root = tmp_path / "Database"
    registrar = CompetitionRegistrar(root)
    registrar.create(2026, "2026curie")
    app = create_app(root, TOKEN)
    manager = DatabaseManager(registrar)

    def build(drops=0):
        inner = TestClient(app)._transport
        transport = DropAfterCommit(inner, drops=drops)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        config = Config(server="http://testserver", token=TOKEN, outbox=str(tmp_path / "queue"))
        return Sender(config, client=client), transport

    return build, manager, Outbox(tmp_path / "queue")


def add(outbox, **data):
    return outbox.add(2026, "2026curie", data or {"team_number": 4414})


# --- outbox -----------------------------------------------------------------

def test_queued_record_survives_being_reread(rig):
    _, _, outbox = rig
    path = add(outbox, team_number=4414, match_number=12)
    [queued] = outbox.pending()
    assert queued.data == {"team_number": 4414, "match_number": 12}
    assert queued.submission_id
    assert queued.path == path


def test_submission_id_is_stable_across_reads(rig):
    """Minted at capture, not at send - otherwise every retry is a new record."""
    _, _, outbox = rig
    add(outbox)
    first = outbox.pending()[0].submission_id
    assert outbox.pending()[0].submission_id == first


def test_unparseable_file_is_rejected_not_retried_forever(rig):
    _, _, outbox = rig
    (outbox.pending_dir / "garbage.json").write_text("{not json")
    assert outbox.pending() == []
    assert outbox.counts()["rejected"] == 1


def test_requeue_rejected(rig):
    _, _, outbox = rig
    (outbox.pending_dir / "garbage.json").write_text("{not json")
    outbox.pending()
    assert outbox.requeue_rejected() == 1
    assert outbox.counts()["pending"] == 1


# --- happy path -------------------------------------------------------------

def test_drain_sends_and_files_the_record(rig):
    build, manager, outbox = rig
    sender, _ = build()
    add(outbox, team_number=4414)

    attempt = sender.drain_once()

    assert attempt.stored == 1
    assert outbox.counts() == {"pending": 0, "sent": 1, "rejected": 0}
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1}


def test_drain_batches_many_records(rig):
    build, manager, outbox = rig
    sender, transport = build()
    for i in range(12):
        add(outbox, team_number=i)

    attempt = sender.drain_once()

    assert attempt.stored == 12
    assert transport.seen == 1, "twelve records should travel as one request"
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 12}


# --- the failure this all exists for ----------------------------------------

def test_lost_response_then_retry_stores_exactly_one_row(rig):
    build, manager, outbox = rig
    sender, transport = build(drops=1)
    add(outbox, team_number=4414, match_number=12)

    first = sender.drain_once()
    assert first.unreachable is True, "the dropped response must look like a network failure"
    assert outbox.counts()["pending"] == 1, "record stays queued when no ack arrives"
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1}, "but the write did land"

    second = sender.drain_once()

    assert second.duplicate == 1, "the retry is recognised, not re-stored"
    assert second.stored == 0
    assert outbox.counts() == {"pending": 0, "sent": 1, "rejected": 0}
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1}, "still exactly one row"


def test_repeated_drops_still_converge_on_one_row(rig):
    """Three consecutive failures, as if someone kept standing on the cable."""
    build, manager, outbox = rig
    sender, _ = build(drops=3)
    add(outbox, team_number=4414)

    for _ in range(4):
        sender.drain_once()

    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1}
    assert outbox.counts() == {"pending": 0, "sent": 1, "rejected": 0}


# --- error handling ---------------------------------------------------------

def test_rejected_record_is_filed_not_requeued(rig):
    build, _, outbox = rig
    sender, _ = build()
    outbox.add(2026, "2026curie", {"bad name!": 1})

    attempt = sender.drain_once()

    assert attempt.rejected == 1
    assert outbox.counts() == {"pending": 0, "sent": 0, "rejected": 1}


def test_unreachable_server_keeps_everything_queued(rig, tmp_path):
    _, manager, outbox = rig
    add(outbox)
    config = Config(server="http://127.0.0.1:9", token=TOKEN, outbox=str(tmp_path / "queue"))
    sender = Sender(config, client=httpx.Client(timeout=0.3))

    attempt = sender.drain_once()

    assert attempt.unreachable is True
    assert outbox.counts()["pending"] == 1
    assert manager.tables(2026, "2026curie") == {}


def test_bad_token_holds_records_rather_than_rejecting_them(rig, tmp_path):
    """A wrong token is a config error. Discarding a day of scouting over it would
    be far worse than holding the queue and complaining."""
    build, _, outbox = rig
    sender, _ = build()
    sender.config.token = "wrong"
    add(outbox)

    attempt = sender.drain_once()

    assert attempt.error and "401" in attempt.error
    assert outbox.counts() == {"pending": 1, "sent": 0, "rejected": 0}


def test_oversized_batch_is_split_and_still_lands(rig):
    build, manager, outbox = rig
    sender, _ = build()
    for i in range(60):  # over the server's MAX_BATCH of 50
        add(outbox, team_number=i)

    attempt = sender.drain_once()

    assert attempt.stored == 60
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 60}


def test_run_once_returns(rig):
    build, _, outbox = rig
    sender, _ = build()
    add(outbox)
    sender.run(once=True, sleep=lambda _: None)
    assert outbox.counts()["sent"] == 1
