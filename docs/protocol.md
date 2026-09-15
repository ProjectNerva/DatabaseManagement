# Upload protocol

How a match record travels from a scout's laptop to a table in `2026curie.db`.

The transport is HTTP over a wired LAN — the Pi and the scouting machines on a
dedicated switch, an isolated segment with no route to the internet. This is
reliable, but it is not infallible: a cable gets kicked, a switch loses power, a
laptop sleeps mid-request. The protocol is built around one consequence of that.

> **The guarantee.** A client that sends a record and receives no response cannot
> tell whether the write landed. Retrying must therefore be *provably harmless* —
> and it is, because every record carries an id the server remembers.

---

## 1. Outbox file format

The scouting app writes one file per record into `outbox/`. The uploader reads it,
sends it, and moves it to `sent/` or `rejected/`. It is never edited in place.

```json
{
  "submission_id": "9f2c1d7a-4e55-4b21-9d3f-6a1e0c87b442",
  "year": 2026,
  "competition": "2026curie",
  "base_name": "scouting",
  "captured_at": "2026-03-14T10:42:07-04:00",
  "data": {
    "match_number": 12,
    "team_number": 4414,
    "auto_climb": true,
    "defense_qata": 3
  }
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `submission_id` | yes | UUID4, **minted at capture time** |
| `year` | yes | Competition year |
| `competition` | yes | Competition slug, e.g. `2026curie` |
| `data` | yes | The record itself; must be a JSON object |
| `base_name` | no | Table family, defaults to `scouting` |
| `captured_at` | no | Local ISO timestamp, informational only |

Required fields are enforced by `uploader.outbox.REQUIRED_FIELDS`. A file missing
any of them, or holding invalid JSON, is moved to `rejected/` on the first read
rather than retried forever.

**`submission_id` must be generated when the record is captured, not when it is
sent.** Minting it at send time produces a fresh id on every retry and defeats the
entire deduplication mechanism. `Outbox.add()` does this correctly; a scouting app
writing files directly must do the same.

Files are written to a `.tmp` name and renamed into place, so the uploader can
never read a file the scouting app is still halfway through writing.

---

## 2. Endpoints

Base path `/api/v1`. All endpoints except `/health` require:

```
Authorization: Bearer <shared-event-token>
```

One token per event, shared by every machine. The segment is isolated and has no
internet route, so the threat model is a mis-pointed client, not an attacker.

### `POST /submit`

```http
POST /api/v1/submit
Authorization: Bearer <token>
Content-Type: application/json

{
  "year": 2026,
  "competition": "2026curie",
  "base_name": "scouting",
  "records": [
    {"submission_id": "9f2c1d7a-…", "data": {"match_number": 12, "team_number": 4414}},
    {"submission_id": "a7b104e9-…", "data": {"match_number": 12, "team_number": 1234}}
  ]
}
```

Maximum **50 records** per request (`server.app.MAX_BATCH`). Batching exists for
offline catch-up: a scout cut off for ten matches uploads ten records at once when
the link returns.

**Response — always per record:**

```json
{
  "results": [
    {"submission_id": "9f2c1d7a-…", "status": "stored",    "table": "scouting_v1", "row_id": 12},
    {"submission_id": "a7b104e9-…", "status": "duplicate", "table": "scouting_v1", "row_id": 7},
    {"submission_id": "c31d77af-…", "status": "rejected",  "error": "invalid column name: 'bad name!'"}
  ]
}
```

| `status` | Meaning |
| --- | --- |
| `stored` | A new row was written |
| `duplicate` | This id was already applied; `row_id` points at the original |
| `rejected` | The record is malformed. Retrying will never help |

The HTTP status describes *the request*; each record carries its own outcome in
the body. One scout's malformed record must never cost the other five robots their
match, so a batch containing junk still returns `200` with the good records stored.

### `GET /health`

**Unauthenticated, on purpose.** A scout with a mistyped token must still be able
to tell "the Pi is down" from "my config is wrong".

```json
{"status": "ok", "competitions": [{"year": 2026, "slug": "2026curie"}]}
```

### `GET /competitions`

```json
{"competitions": [{"year": 2026, "slug": "2026curie"}]}
```

### `GET /tables/{year}/{slug}`

Row counts as the Pi actually holds them — what `uploadctl verify` prints, and the
best answer to "did my match save?".

```json
{"year": 2026, "competition": "2026curie", "tables": {"scouting_v1": 4, "scouting_v2": 1}}
```

The `_submissions` ledger is never listed; it is bookkeeping, not scouting data.

---

## 3. Status codes

| Code | Cause | Client action | Retry |
| --- | --- | --- | --- |
| `200` | Request understood; see per-record `status` | File each record by its outcome | — |
| `401` | Bad or missing token | Hold the queue, alert loudly | no |
| `404` | Competition does not exist | Hold the queue, alert | no |
| `413` | Batch above `MAX_BATCH` | Split the batch and resend | immediately |
| `422` | Payload fails schema validation | Reject the record | never |
| `503` | Write lock held past the busy timeout | Keep queued, back off | yes |
| timeout / refused | Pi or link unreachable | Keep queued, back off | yes |

`401` and `404` are configuration errors, not network ones. The uploader holds
every record rather than discarding it — losing a day of scouting to a typo in
`uploader.json` would be far worse than a stalled queue and a loud message.

---

## 4. Idempotency

The server keeps a `_submissions` table (`DatabaseManager.LEDGER_TABLE`) whose
`submission_id` is the primary key, and writes to it **inside the same transaction
as the record**:

```python
conn.execute("BEGIN IMMEDIATE")
# choose_table, CREATE TABLE if the shape drifted, INSERT the record
conn.execute('INSERT INTO "_submissions" (submission_id, table_name, row_id, received_at) …')
conn.execute("COMMIT")
```

Both land or neither does. If the ledger write happened after `COMMIT`, a power
cut between the two would leave a stored row with no ledger entry — and the retry
would duplicate it.

On a repeat id the server rolls back and reports where the first copy went. The
read-before-write is race-free because `BEGIN IMMEDIATE` already holds the write
lock, so no other submission can insert the same id in between.

Three properties worth knowing:

- **The id is the identity, not the content.** A client that edits a record and
  resends it under the same id gets `duplicate` — the first version already landed.
- **A rejected record does not burn its id.** The transaction rolls back entirely,
  so a corrected resubmission under the same id is stored normally.
- **Submissions without an id are not deduplicated.** Omitting `submission_id`
  preserves the original behaviour exactly: two identical records are two rows.

### The ledger is hidden from schema matching

`DatabaseManager._table_columns()` excludes `_submissions`. Without that filter the
ledger would be offered to `choose_table` as a candidate record shape, and scouting
records could be matched against bookkeeping columns. `tables()` excludes it too,
so it never appears in `uploadctl verify`.

This is the single most dangerous place to make a change, because it fails
silently — records land in the wrong version table and nothing complains.
`tests/test_idempotency.py::test_ledger_is_never_offered_as_a_record_shape` guards it.

---

## 5. Retry and backoff

```
BACKOFF = (1.0, 2.0, 4.0, 5.0)   # uploader/sender.py
```

`1s, 2s, 4s, then every 5s indefinitely`, with ±20% jitter.

The short ceiling is deliberate and is the opposite of what wifi would want. A
dead wired link stays dead until a person fixes it; the moment they replug the
cable, the backlog should flush in seconds rather than wait out a long timer.
Polling a wired peer every 5s costs nothing.

Jitter matters once more than one machine is on the switch: six uploaders that all
lost the same switch otherwise re-converge on the Pi in lockstep the instant it
returns.

---

## 6. Concurrency

Six robots on a field means six scouts finishing within seconds of each other, so
six simultaneous submissions is the normal shape of every match, not a stress case.

No special handling is needed. `DatabaseManager.submit_record` opens its own
connection per call and takes the write lock with `BEGIN IMMEDIATE` **before**
reading `sqlite_master`, so the "which version table does this shape belong in"
decision happens under the lock. Two devices with the same new shape cannot both
create `scouting_v2`. Contention is absorbed by `BUSY_TIMEOUT_SECONDS = 30`.

Measured on this code, six concurrent submissions to one competition complete in
roughly 45 ms, scaling linearly to about 8 ms per submission at 48 writers —
against a seven-minute match cadence. Throughput is not a concern; the link is the
only real adversary.
