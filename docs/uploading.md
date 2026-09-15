# Uploading from a laptop to the Pi

Step by step, command line only. Addresses below assume the Pi is at
`10.42.0.1`; to try the whole thing on one machine, use `127.0.0.1` everywhere.

---

## Setup (once per machine)

**On the Pi**

```bash
pip install -r requirements-server.txt
python -c "from CompetitionRegistrar import CompetitionRegistrar as R; R().create(2026, '2026curie')"
```

**On each scout laptop**

```bash
pip install -r requirements-uploader.txt
cp uploader.example.json uploader.json
```

Edit `uploader.json` — every laptop gets a different `device_id`, the same token:

```json
{
  "server": "http://10.42.0.1:8000",
  "token": "curie-2026",
  "year": 2026,
  "competition": "2026curie",
  "device_id": "scout-3",
  "outbox": "."
}
```

---

## Every event

**1. Start the Pi.**

```bash
SCOUTING_TOKEN=curie-2026 python -m server --root Database --host 0.0.0.0
```

**2. Check each laptop can see it.** Do this before matches start, not during.

```bash
$ curl -s http://10.42.0.1:8000/api/v1/health
{"status":"ok","competitions":[{"year":2026,"slug":"2026curie"}]}
```

**3. Start the uploader on each laptop, and leave the window open.**

```bash
python -m uploader run
```

It prints a line per record and keeps running. The open window is how you notice
it has died — there is no other signal.

---

## Uploading

The scouting app drops JSON files into `outbox/`. Nothing else is needed; the
uploader picks them up on its own.

```bash
$ python -m uploader run --once
stored    2026curie-q12-1234-0567f125.json -> scouting_v1 row 1
stored    2026curie-q12-4414-8f7336cc.json -> scouting_v1 row 2
```

Check the queue at any point:

```bash
$ python -m uploader status

  device      scout-3
  pi          http://10.42.0.1:8000  reachable (HTTP 200)
  competition 2026 / 2026curie
  pending     0
  sent        2
  rejected    0
```

`pending 0` means everything is on the Pi. Confirm from the Pi's side:

```bash
$ python -m uploader verify

  2026 / 2026curie on http://10.42.0.1:8000
  scouting_v1              2 rows
```

`status` exits non-zero when anything is pending or rejected, so it works in a
loop: `watch -n5 python -m uploader status`.

---

## Testing it works

Four checks. Run them at home, before the event.

**1. A record uploads.** Queue one and send it.

```bash
$ python -c "from uploader.outbox import Outbox; Outbox('.').add(2026,'2026curie',{'match_number':12,'team_number':4414,'auto_climb':True})"
$ python -m uploader run --once
stored    2026curie-q12-4414-8f7336cc.json -> scouting_v1 row 2
```

**2. Sending the same record twice does not duplicate it.** The important one.

```bash
$ cp sent/*4414*.json outbox/
$ python -m uploader run --once
duplicate 2026curie-q12-4414-8f7336cc.json -> scouting_v1 row 2

$ python -m uploader verify
  scouting_v1              2 rows        # unchanged
```

**3. A bad record does not block a good one.**

```bash
$ python -m uploader run --once
stored    2026curie-q14-7777-8d647abe.json -> scouting_v1 row 3
rejected  2026curie-qx-x-e2c458c6.json -> invalid column name: 'bad name!'
```

The good record lands; the bad one moves to `rejected/`. Fix it and requeue:

```bash
$ python -m uploader retry --rejected
requeued 1 record(s)
```

**4. Unplug the Pi mid-upload.** Nothing should be lost.

```bash
$ python -m uploader run --once
! ConnectError: [Errno 61] Connection refused

$ python -m uploader status
  pi          http://10.42.0.1:8000  UNREACHABLE (ConnectError)
  pending     1
```

Plug it back in. The backlog flushes with no intervention:

```bash
$ python -m uploader run --once
stored    2026curie-q15-5555-5ec8d50e.json -> scouting_v1 row 4
```

---

## When something is wrong

| `status` says | Means | Do |
| --- | --- | --- |
| `UNREACHABLE (ConnectError)` | Pi down, cable out, or switch off | Check the cable, then the Pi |
| `pending` climbing | Uploader stopped, or Pi unreachable | Is the `run` window still open? |
| `rejected` non-zero | Malformed records | Read the filenames, fix, `retry --rejected` |
| `HTTP 401` | Wrong token | Compare `uploader.json` to the Pi's `SCOUTING_TOKEN` |
| `HTTP 404` | Wrong year or competition | Check the slug against `curl …/api/v1/competitions` |

Records are never deleted. Anything not on the Pi is still in `outbox/` or
`rejected/` — nothing is lost by stopping the uploader and restarting it.

Back up mid-event with a plain file copy:

```bash
cp Database/2026/Data/2026curie/2026curie.db /media/usb/
```
