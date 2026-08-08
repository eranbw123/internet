# internet

Two small tools:

- **`watch.py`** — a watchlist price alerter: pulls quotes from Yahoo Finance
  and pushes a digest to your phone via [ntfy](https://ntfy.sh). Stdlib only.
- **`discovery/`** — a personal internet discovery engine: collects candidate
  content, scores it against your interests with an LLM, and pushes only the
  high scorers to Telegram. See [Discovery engine](#discovery-engine).

The rest of this README covers `watch.py`.

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

---

# Discovery engine

Finds internet content likely to be genuinely interesting to you, scores it,
and pushes only the good stuff to a Telegram bot.

```bash
pip install -r requirements.txt      # needs `anthropic`
cp .env.example .env                 # set ANTHROPIC_API_KEY (+ Telegram, optional)
$EDITOR interests.json               # what you care about
python -m app init                   # create discovery.db, load interests
python -m app run-once --dry-run     # one cycle, prints pushes instead of sending
python -m app run                    # loop forever (DISCOVERY_INTERVAL, default 1h)
```

Run it from the repo root — the `stocks` collector imports `watch.py`.
`python -m app` and `python -m discovery` are the same CLI.

## How a cycle works

Every candidate goes through the same explicit stages:

```
collect -> normalize -> dedup -> persist -> interest matching
        -> cheap pre-filter -> LLM scoring -> threshold -> notification
```

Each stage's verdict is persisted in `discovery.db`, so nothing is collected,
filtered, scored, or pushed twice — a cycle that dies halfway resumes on the
next one instead of re-paying for the same LLM calls. A dead collector, an
unscoreable item, or a failed push is logged and skipped; the cycle continues.

Only the last two stages cost money. **Dedup** catches the same story three
ways — canonical URL, headline, and body text — and the **pre-filter** drops
anything with nothing to read or no interest to read it against, before any
model sees it.

## Scoring

The model rates, the code ranks. Scoring returns six 0–1 dimensions and the
final score is computed here, from `models.WEIGHTS`:

| Dimension | Weight | What it rates |
| --- | --- | --- |
| `personal_relevance` | 0.35 | How squarely it lands on the interest as you described it |
| `novelty` | 0.20 | New, versus a restatement of what you already know |
| `depth` | 0.15 | Primary data, mechanism, methods — versus commentary |
| `importance` | 0.15 | How much it changes what is true or what you should do |
| `surprise` | 0.15 | How much it cuts against the consensus |
| `specificity` | — | How concrete it is. Scored and stored, deliberately unweighted |

Computing the total in code means the model can't inflate its own verdict, and
the ranking formula can change without re-scoring anything — which is why
`specificity` is collected but sits outside the weights for now.

The model also picks which interest the item is most relevant to, and returns
a `reason`, a `why_better_than_generic` line, and its own `confidence` (shown
in the push when it's below 0.5).

## Trying one item

```bash
python -m app score --url https://example.com/x --title "..." --text "..."
python -m app score --item-id 42 --force        # re-score something already stored
```

Prints the full verdict as JSON — which stage it reached, which interests it
matched and why, and every dimension. It runs the real `pipeline.ingest`, dedup
and pre-filter included, so it shows what the engine would actually do rather
than what the scorer says in isolation. Add `--notify` to run delivery too.

## interests.json

```json
{
  "defaults": { "min_score": 0.75, "sources": ["web_search"] },
  "interests": [
    {
      "key": "nbis-nebius",
      "title": "NBIS / Nebius",
      "description": "Free text. The scorer reads this verbatim, so say what you actually want.",
      "positive_signals": ["earnings and capacity disclosures"],
      "negative_signals": ["generic AI-stock roundups"],
      "min_score": 0.70,
      "sources": ["web_search", "stocks"],
      "source_config": {
        "stocks": { "tickers": ["NBIS"], "schedule": "daily", "min_change_pct": 4 }
      }
    }
  ]
}
```

`key` is the identity across runs — re-running `init` updates an interest in
place rather than duplicating it.

`min_score` is a **0–1** threshold on the final score. (It used to be 0–100;
anything above 1 is treated as the old scale and divided by 100, so a stale
`75` doesn't silently mean "never notify".)

## Collectors

| Source | Status | What it does |
| --- | --- | --- |
| `web_search` | working | The provider's server-side web search returns candidate articles |
| `stocks` | working | Notable price moves via `watch.py`'s Yahoo fetch |
| `youtube` | stub | Planned: recent uploads + transcripts (needs a YouTube API key) |

A collector is one function — `collect(interest, cfg, provider) -> list[CandidateItem]`
— registered in `discovery/collectors/__init__.py`. Collectors fetch and shape
only: they don't normalize, dedup, or judge relevance. One that raises is
logged and skipped; the rest of the cycle still runs.

## Providers

The pipeline only ever holds an `LLMProvider` (`complete_json` / `search_json`),
so swapping vendors is a config change:

```bash
DISCOVERY_PROVIDER=anthropic   # default, model claude-opus-5
DISCOVERY_PROVIDER=openai      # model gpt-5, needs OPENAI_API_KEY and `pip install openai`
```

No vendor SDK is imported outside `discovery/providers/`. A capability a
provider lacks — OpenAI has no equivalent of Claude's server-side web search —
raises `UnsupportedCapability` and that collector is skipped like any other
failure, so `web_search` needs the `anthropic` provider.

## Feedback

```bash
python -m app items --min-score 0.7          # ids and scores
python -m app feedback 42 down --interest-id 2 --note "listicle"
```

Recent verdicts for an interest are fed back into its scoring prompt as
worked examples.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | *(none)* | **Required** for the default provider — scoring and web search |
| `OPENAI_API_KEY` | *(none)* | Required only when `DISCOVERY_PROVIDER=openai` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(none)* | Unset ⇒ pushes print to stdout |
| `DISCOVERY_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `DISCOVERY_MODEL` | per provider | `claude-opus-5` / `gpt-5` |
| `DISCOVERY_DB` | `discovery.db` | SQLite file (gitignored; rebuildable) |
| `DISCOVERY_INTERESTS` | `interests.json` | Interests file |
| `DISCOVERY_MAX_ITEMS` | `8` | Items per source per cycle |
| `DISCOVERY_INTERVAL` | `3600` | Seconds between cycles in `run` |
| `DISCOVERY_MIN_MATCH` | `0.25` | Pre-filter: weakest interest match worth scoring |
| `DISCOVERY_MIN_TEXT_CHARS` | `120` | Pre-filter: least text worth sending to an LLM |

`--provider`, `--model` and `--db` override the environment for one run.

## Tests

```bash
python test_discovery.py
```

59 tests, network fully stubbed — they never hit an LLM API, Telegram, or
Yahoo. The provider seam is the whole stub: a fake object with `complete_json`
and `search_json`.
