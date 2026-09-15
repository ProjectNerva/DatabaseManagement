# DatabaseManagement

Scouting data collection for FRC competitions. Scouts capture one JSON record per
robot per match; those records travel over a wired LAN to a Raspberry Pi 5, which
stores them in a per-competition SQLite database and versions the tables as the
scouting app's record shape drifts mid-event.

| Document | What it covers |
| --- | --- |
| [docs/uploading.md](docs/uploading.md) | Step-by-step: uploading from a laptop to the Pi, and how to test it |
| [docs/protocol.md](docs/protocol.md) | The wire protocol: endpoints, payloads, status codes, idempotency, retry rules |
| [docs/testing.md](docs/testing.md) | How the test suite is structured and what each layer proves |

## The two halves

The repo runs on both machines, but each uses a disjoint half of it. `uploader/`
imports nothing from the database code, so a scout laptop never needs SQLite,
FastAPI, or any of the schema logic.

| | Raspberry Pi 5 | Scout laptop (×6) |
| --- | --- | --- |
| Code used | `server/`, `DatabaseManager`, `CompetitionRegistrar`, `Utils/` | `uploader/` |
| Install | `requirements-server.txt` | `requirements-uploader.txt` |
| Run | `python -m server --root Database --token <token>` | `python -m uploader run` |
| Holds | the live `Database/` | `outbox/`, `sent/`, `rejected/` |

`Database/` is gitignored and never syncs: the Pi's copy is the only real one.
`uploader.json` is gitignored too — it carries the event token and each laptop's
own `device_id`.

## Quick start — the Pi

```bash
pip install -r requirements-server.txt
python -c "from CompetitionRegistrar import CompetitionRegistrar as R; R().create(2026, '2026curie')"
SCOUTING_TOKEN=some-event-token python -m server --root Database --host 0.0.0.0
```

## Quick start — a scout laptop

```bash
pip install -r requirements-uploader.txt
cp uploader.example.json uploader.json      # then set device_id and the token
python -m uploader run
```

The scouting app's only obligation is to drop a JSON file into `outbox/`. It needs
no networking code and no retry logic; a record is safe the moment it lands there.

```python
from uploader.outbox import Outbox

Outbox(".").add(2026, "2026curie", {"match_number": 12, "team_number": 4414})
```

### uploadctl

There is no GUI. These four commands are the whole interface.

```
uploadctl run                 watch outbox/, POST, back off, repeat
uploadctl status              queue depth and Pi reachability
uploadctl verify              row counts as the Pi actually has them
uploadctl retry --rejected    requeue after fixing a bad record
```

`status` exits non-zero whenever anything is pending or rejected, so it drops into
a shell prompt or `watch -n5 python -m uploader status`.

## How records become tables

A submission is one JSON object. Its shape decides where it lands: a record
matching a table already declared for the competition is inserted there; a record
with a genuinely new shape gets a new `<base_name>_vN` table, created in the
database and appended to the schema file. This means a scouting app updated
mid-event does not break anything — the new fields simply open a new version.

The stored `.sql` file under `Schemas/` is derived from the database, never the
reverse. SQLite is always the authority on what exists.

## Databases

Each competition is a single self-contained `.db` file in `journal_mode=DELETE`.
There are no `-wal` or `-shm` sidecars to lose, so backing up a competition
mid-event is a plain file copy:

```bash
cp Database/2026/Data/2026curie/2026curie.db /media/usb/
```

A `-journal` file appears only while a write is in flight and is gone at commit.

## Tests

```bash
pip install -r requirements.txt
pytest -q
```

See [docs/testing.md](docs/testing.md) — in particular for why the suite simulates
six devices even though you will usually be testing with one.
