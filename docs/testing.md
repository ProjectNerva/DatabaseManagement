# Test suite

```bash
pip install -r requirements.txt
pytest -q                          # 110 tests, ~2.5s
pytest tests/test_six_devices.py   # the slow ones: real sockets
```

Every test builds its own competition under pytest's `tmp_path`. Nothing touches
the real `Database/`.

---

## Layers

The suite is four layers deep, each proving something the layer below cannot.

| File | Tests | Proves |
| --- | --- | --- |
| `test_json_converter.py` | 50 | Shape inference, version selection, schema rendering |
| `test_database_manager.py` | 12 | Records land in the right table; bad input is refused |
| `test_concurrency.py` | 6 | Simultaneous submissions never lose a record |
| `test_idempotency.py` | 10 | A retried submission never produces a second row |
| `test_server.py` | 14 | The wire contract: status codes, per-record outcomes |
| `test_uploader.py` | 13 | The outbox, and the failure the protocol exists for |
| `test_six_devices.py` | 5 | Six machines over real sockets |

The first three predate the upload protocol and still pass unchanged — the
protocol work added parameters, never altered existing behaviour. A submission
without a `submission_id` behaves exactly as it always did, which
`test_idempotency.py::test_no_id_means_no_deduplication` pins down.

---

## The two tests that carry the most weight

### A lost response must not duplicate a row

`test_uploader.py::test_lost_response_then_retry_stores_exactly_one_row`

The ambiguous case cannot be produced by unplugging something at a convenient
moment, so it is forced. `DropAfterCommit` is an httpx transport that passes the
request through to the app, waits for the response to be fully read — the write
has genuinely committed by then — and only then raises `ReadTimeout`:

```python
def handle_request(self, request):
    response = self.inner.handle_request(request)
    if request.url.path.endswith("/submit"):
        self.seen += 1
        if self.drops > 0:
            self.drops -= 1
            response.read()  # the app has now really run and committed
            raise httpx.ReadTimeout("link dropped after commit", request=request)
    return response
```

(`seen` counts requests that reached the app, which
`test_drain_batches_many_records` uses to assert twelve records travelled as one
request rather than twelve.)

A transport that failed *before* delegating would test nothing — that is just an
unreachable server. The assertions then check the state that makes this hard:

```python
assert first.unreachable is True                              # client sees a failure
assert outbox.counts()["pending"] == 1                        # record stays queued
assert manager.tables(...) == {"scouting_v1": 1}              # but the write landed

second = sender.drain_once()
assert second.duplicate == 1                                  # retry is recognised
assert manager.tables(...) == {"scouting_v1": 1}              # still exactly one row
```

A companion test drops three responses in a row, as if someone kept standing on
the cable, and still converges on one row.

### Six devices, real sockets

`test_six_devices.py`

**You will usually be testing with one laptop, and one laptop cannot fail the way
six do.** Lock contention, interleaved version creation, and cross-client
duplicates all look perfect on a single device and then surface at an event. So
this file runs a real uvicorn server on a real port and drives it from six threads,
each with its own outbox — the closest thing to the venue that fits in a test.

| Test | The failure it would catch |
| --- | --- |
| `six_devices_one_match_every_record_lands` | A record dropped under contention |
| `six_devices_with_drifting_shapes_split_into_versions_once` | Two devices both creating `scouting_v2` |
| `a_full_match_sequence_of_bursts` | Drift or leakage across ten consecutive bursts |
| `same_id_from_two_devices_stores_one_row` | Dedup losing the race when both requests are in flight |
| `one_device_offline_catches_up_without_disturbing_the_others` | Catch-up corrupting or duplicating live data |

The drifting-shapes test is the sharpest: three scouts on an updated app and three
on the old one submit simultaneously, and the assertion is that exactly two version
tables exist holding three rows each. Both outcomes of the race — a lost record, or
a duplicate version table — fail it.

---

## Conventions

- **One competition per test**, created in `tmp_path` by a fixture.
- **Assert on `manager.tables()`**, not on internal state. It is what `uploadctl
  verify` shows a scout, so a test that passes while `tables()` is wrong is a test
  that lies.
- **Name the failure, not the mechanism.** `test_one_bad_record_does_not_sink_the_batch`
  says what breaks in the stands if it regresses.
- **Real sockets where contention is the subject**, `TestClient` everywhere else.
  `TestClient` is faster and sufficient for contract tests; it cannot demonstrate
  that six OS-level clients interleave safely.

## Adding tests

New wire behaviour belongs in `test_server.py`; new client behaviour in
`test_uploader.py`. Add to `test_six_devices.py` only when concurrency is the
subject — those tests cost real seconds.

If you touch `DatabaseManager._table_columns`, run `test_idempotency.py` first. The
ledger-visibility tests there are the only thing standing between a one-line change
and records silently landing in the wrong version table.
