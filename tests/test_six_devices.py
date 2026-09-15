"""Six scouting machines against one Pi, over real sockets.

This is the configuration that runs at an event and the one a single test laptop
cannot reproduce: six uploaders contending for the same write lock at the moment a
match ends. Everything here would pass trivially with one client, which is exactly
why it is worth running.
"""

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).parent.parent))

from CompetitionRegistrar import CompetitionRegistrar
from DatabaseManager import DatabaseManager
from server.app import create_app
from uploader.outbox import Outbox
from uploader.sender import Config, Sender

TOKEN = "event-token"
DEVICES = 6


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def pi(tmp_path):
    """A real uvicorn server on a real port, as the Pi actually runs it."""
    root = tmp_path / "Database"
    registrar = CompetitionRegistrar(root)
    registrar.create(2026, "2026curie")

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(root, TOKEN), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/api/v1/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        pytest.fail("server did not start")

    yield base, DatabaseManager(registrar), tmp_path

    server.should_exit = True
    thread.join(timeout=5)


def device(tmp_path, base, n) -> tuple[Sender, Outbox]:
    queue = tmp_path / f"device-{n}"
    config = Config(server=base, token=TOKEN, outbox=str(queue), device_id=f"scout-{n}")
    return Sender(config), Outbox(queue)


def drain_together(senders):
    """Every device uploads at once, as they do when a match ends."""
    results, errors = [], []

    def work(s):
        try:
            results.append(s.drain_once())
        except Exception as err:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(err)

    threads = [threading.Thread(target=work, args=(s,)) for s in senders]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, f"uploader raised under contention: {errors}"
    return results


def test_six_devices_one_match_every_record_lands(pi, tmp_path):
    base, manager, _ = pi
    senders = []
    for n in range(DEVICES):
        sender, outbox = device(tmp_path, base, n)
        outbox.add(2026, "2026curie", {"match_number": 12, "team_number": 4000 + n, "notes": "x"})
        senders.append(sender)

    results = drain_together(senders)

    assert sum(r.stored for r in results) == DEVICES
    assert sum(r.duplicate for r in results) == 0
    assert manager.tables(2026, "2026curie") == {"scouting_v1": DEVICES}


def test_six_devices_with_drifting_shapes_split_into_versions_once(pi, tmp_path):
    """Three scouts on an updated app, three on the old one, all submitting together.

    The race this guards: two devices both finding no matching table and both
    creating scouting_v2.
    """
    base, manager, _ = pi
    senders = []
    for n in range(DEVICES):
        sender, outbox = device(tmp_path, base, n)
        record = {"match_number": 12, "team_number": 4000 + n}
        if n % 2:
            record["auto_pickup"] = 3  # the newer app's extra field
        else:
            record["notes"] = "x"
        outbox.add(2026, "2026curie", record)
        senders.append(sender)

    drain_together(senders)

    tables = manager.tables(2026, "2026curie")
    assert sorted(tables) == ["scouting_v1", "scouting_v2"], tables
    assert sum(tables.values()) == DEVICES
    assert set(tables.values()) == {3}


def test_a_full_match_sequence_of_bursts(pi, tmp_path):
    """Ten matches, six scouts each - an hour of qualifications, compressed."""
    base, manager, _ = pi
    devices = [device(tmp_path, base, n) for n in range(DEVICES)]

    for match in range(1, 11):
        for n, (_, outbox) in enumerate(devices):
            outbox.add(2026, "2026curie", {"match_number": match, "team_number": 4000 + n})
        drain_together([s for s, _ in devices])

    assert manager.tables(2026, "2026curie") == {"scouting_v1": 10 * DEVICES}
    for _, outbox in devices:
        assert outbox.counts() == {"pending": 0, "sent": 10, "rejected": 0}


def test_same_id_from_two_devices_stores_one_row(pi, tmp_path):
    """A shared id should never happen with UUID4, but if a scouting app is ever
    cloned wholesale onto a second laptop it will - and the ledger must hold the
    line even when both requests are in flight at once."""
    base, manager, _ = pi
    senders = []
    for n in range(2):
        sender, outbox = device(tmp_path, base, n)
        outbox.add(
            2026, "2026curie", {"team_number": 4414}, submission_id="collision-1"
        )
        senders.append(sender)

    results = drain_together(senders)

    assert sum(r.stored for r in results) == 1
    assert sum(r.duplicate for r in results) == 1
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 1}


def test_one_device_offline_catches_up_without_disturbing_the_others(pi, tmp_path):
    """A scout whose cable was out for eight matches reconnects mid-event."""
    base, manager, _ = pi
    online = [device(tmp_path, base, n) for n in range(DEVICES - 1)]
    late_sender, late_outbox = device(tmp_path, base, 99)

    for match in range(1, 9):
        for n, (_, outbox) in enumerate(online):
            outbox.add(2026, "2026curie", {"match_number": match, "team_number": 4000 + n})
        late_outbox.add(2026, "2026curie", {"match_number": match, "team_number": 4099})
        drain_together([s for s, _ in online])

    assert late_outbox.counts()["pending"] == 8
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 8 * (DEVICES - 1)}

    late_sender.drain_once()  # cable goes back in

    assert late_outbox.counts() == {"pending": 0, "sent": 8, "rejected": 0}
    assert manager.tables(2026, "2026curie") == {"scouting_v1": 8 * DEVICES}
