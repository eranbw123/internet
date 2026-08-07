# internet

A small watchlist price alerter: pulls quotes from Yahoo Finance and pushes a
digest to your phone via [ntfy](https://ntfy.sh).

Stdlib only — no `pip install` needed.

## Quick start

```bash
cp watchlist.example.json watchlist.json   # edit your tickers
cp .env.example .env                       # set NTFY_TOPIC
python watch.py --schedule daily
```

Try it without any config at all:

```bash
python watch.py --ticker NBIS --schedule weekly --dry-run
```

```
NBIS      184.84 USD  -2.92% (-5.57 / 1w)
```

## Watchlist

`watchlist.json` (gitignored — `watchlist.example.json` is the template):

```json
{
  "default_schedule": "daily",
  "default_min_change_pct": 0,
  "tickers": [
    { "ticker": "NBIS", "schedule": "weekly" },
    { "ticker": "NVDA", "schedule": "daily", "min_change_pct": 3 },
    "AAPL"
  ]
}
```

A bare string inherits the file-level defaults; an object overrides them.

| Field | Meaning |
| --- | --- |
| `schedule` | `hourly`, `daily`, or `weekly` — the comparison window |
| `min_change_pct` | Only alert if the move is at least this big, in either direction. `0` = always alert |

Comparison windows are counted in **trading bars, not calendar time**, so
`weekly` on a Monday compares against the previous Monday's close rather than
against a weekend with no bar.

## Running it

`--schedule` selects which watchlist entries to process, so you point one
scheduled job at each bucket:

```bash
python watch.py --schedule hourly
python watch.py --schedule daily
python watch.py --schedule weekly
```

Omit `--schedule` to process every entry in one go.

### Windows Task Scheduler

```powershell
$py   = (Get-Command python).Source
$repo = "C:\github\internet"

Register-ScheduledTask -TaskName "watchlist-daily" -Force `
  -Action  (New-ScheduledTaskAction -Execute $py -Argument "watch.py --schedule daily" -WorkingDirectory $repo) `
  -Trigger (New-ScheduledTaskTrigger -Daily -At 4:30pm)

Register-ScheduledTask -TaskName "watchlist-weekly" -Force `
  -Action  (New-ScheduledTaskAction -Execute $py -Argument "watch.py --schedule weekly" -WorkingDirectory $repo) `
  -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At 4:30pm)
```

### cron

```cron
30 16 * * 1-5  cd /path/to/internet && python watch.py --schedule daily
30 16 * * 5    cd /path/to/internet && python watch.py --schedule weekly
```

## All options

```
--schedule {hourly,daily,weekly}  Only process entries with this schedule
--watchlist PATH                  Watchlist file (default: watchlist.json)
--ticker SYM                      Check this ticker instead of the watchlist (repeatable)
--min-change-pct N                Override every entry's threshold
--dry-run                         Print the notification instead of sending it
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `NTFY_TOPIC` | *(none)* | Topic to push to. **Required** — no push without it |
| `NTFY_BASE` | `https://ntfy.sh` | Override for a self-hosted ntfy server |

Read from the process environment first, then `.env`. Environment wins, so CI
secrets override the local file.

> **Your ntfy topic is a secret.** Anyone who knows the topic string can read
> your notifications and send to them. `.env` is gitignored; keep it that way,
> and use repository secrets rather than a committed value in CI.

## Behaviour notes

- **One digest per run**, not one push per ticker — the title names the biggest
  mover and the body lists everything that crossed its threshold.
- **A bad ticker doesn't abort the run.** It's reported to stderr and appended
  to the notification body as `Failed: …`; the rest still go out.
- **A failed ntfy push doesn't fail the run** — it's logged to stderr.
- **The live quote wins over the last bar** when Yahoo provides one, since
  intraday the final bar can lag the current print by up to one interval.

## Tests

```bash
python test_watch.py
```

20 tests, network fully stubbed — they never hit Yahoo or ntfy.
