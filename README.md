# internet

A personal internet discovery engine: collects candidate content, scores it
against your interests with an LLM, and pushes only the high scorers to
Telegram. See [Discovery engine](#discovery-engine).

`watch.py` is a small shared library inside this repo (Yahoo Finance chart
fetch + price-change-over-a-trading-bar-window), not a tool of its own — the
`stocks` collector calls it directly to check tickers as part of a discovery
cycle, and `discovery/config.py` reuses its `.env` loader. There's no separate
CLI, schedule, or notification channel for it; alerting on a price move goes
out the same Telegram flow as everything else discovery finds (see the
`stocks` row under [Collectors](#collectors)).

```bash
python test_watch.py
```

10 tests, network fully stubbed — they never hit Yahoo.

---

# Discovery engine

Finds internet content likely to be genuinely interesting to you, scores it,
and pushes only the good stuff to a Telegram bot.

```bash
pip install -r requirements.txt      # default path is stdlib-only
cp .env.example .env                 # set CLAUDE_ORG_ID (+ Telegram, optional)
# launch Chrome with --remote-debugging-port=9222 and log into claude.ai there
$EDITOR interests.json               # what you care about
python -m app init                          # create discovery.db, load interests
python -m app --dry-run run-once            # one cycle, prints pushes instead of sending
python -m app listen --drain                # one bounded pass: record feedback-button presses
python -m app stats                         # is it finding anything you care about?
python -m app health                        # is it actually alive right now?
```

There is no in-process scheduler loop — each command above is short-lived and
idempotent, meant to be fired on its own cadence by the OS scheduler (see
[Running it as an appliance](#running-it-as-an-appliance)) rather than run in
a long-lived session.

## Commands

| Command | What it does |
| --- | --- |
| `init` | Create/upgrade `discovery.db` and load `interests.json` |
| `run-once [--source X]` | One collect → score → notify cycle. Gated by a provider preflight — exits 3 without touching a collector/LLM if Chrome/CDP is down |
| `listen` | Long-polls Telegram for feedback buttons, blocking — interactive use |
| `listen --drain` | One bounded feedback pass instead of blocking — for a scheduled task |
| `digest` | Send the pending Discovery digest now |
| `discover <source>` | Run one collector across every interest and print what it found — never sends |
| `score` | Push one candidate through the real pipeline and print the verdict |
| `items` | List recently scored items |
| `feedback <id> <verdict>` | Rate an item from the CLI |
| `stats [--days N]` | Funnel, feedback rates, estimated cost and a HEALTH section — see [Stats](#stats) |
| `health [--notify]` | Job staleness, provider reachability, pending/abandoned sends; `--notify` alerts on degraded/recovery, rate-limited |
| `personal-state [--path]` | Print the sibling `ai` repo's personal-state artifact as this repo would read it — see [Personal-state contract](#personal-state-contract) |

Run it from the repo root — the `stocks` collector imports `watch.py`.
`python -m app` and `python -m discovery` are the same CLI. Global flags
(`--dry-run`, `--db`, `--provider`, `--model`) go **before** the subcommand.

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
model sees it. A cycle scores at most `DISCOVERY_MAX_SCORES` items; the rest
are stored unscored and picked up next cycle, so a collector that suddenly
returns hundreds of candidates costs one cycle's budget rather than hundreds
of LLM calls. `python -m app stats` shows how the funnel is actually behaving.

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
        "stocks": {
          "tickers": [{ "ticker": "NBIS", "daily_percent_move": 6, "weekly_percent_move": 12 }]
        }
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

## Personal-state contract

`discovery/personal_state.py` can read a derived "personal state" artifact
produced by the sibling `ai` repo (a JSON file of topics the owner has been
talking about, per `ai`'s `PERSONAL_STATE_CONTRACT.md`, contract version 1).
It's the only place in this repo that knows that schema; unknown top-level or
per-topic keys are ignored rather than rejected, so `ai` can add fields
without a version bump.

```bash
python -m discovery personal-state              # human-checkable probe: version, age, top topics
python -m discovery personal-state --path X.json
```

The artifact path comes from `DISCOVERY_PERSONAL_STATE` (default
`personal_state.json` at the repo root — gitignored, inbound only, never
committed here). An interest can opt in to appending the artifact's top N
topic keys to its `positive_signals` via a `"personal_state_top_terms": N`
key in `interests.json` — but `init` doesn't pass the loaded state into that
path yet, so the key has no effect even if set. For now, `personal-state`
above is the only way to see what this repo would read.

## Collectors

| Source | Status | What it does |
| --- | --- | --- |
| `web_search` | working | The provider's server-side web search returns candidate articles |
| `stocks` | working | Notable price moves via `watch.py`'s Yahoo fetch |
| `youtube` | working | Recent uploads/search results; transcript available → one candidate **per segment**, otherwise one **video-level** candidate (title + description) |

A collector is one function — `collect(interest, cfg, provider, conn=None) -> list[CandidateItem]`
— registered in `discovery/collectors/__init__.py`. Collectors fetch and shape
only: they don't normalize, dedup, or judge relevance. One that raises is
logged and skipped; the rest of the cycle still runs. `conn` is read-only and
exists for one job: checking what's already stored so a collector can skip work
it would otherwise *pay* for (`stocks`' catalyst explanation, `youtube`'s
transcript fetch) only for dedup to discard it a moment later.

### `youtube`

Needs `YOUTUBE_API_KEY` (video verification/metadata) and, for transcripts,
`pip install youtube-transcript-api`. A video without an available transcript
in the requested language is logged and skipped, not an error — captions are
off for plenty of videos, and the unofficial transcript endpoint also
soft-blocks bursting IPs from time to time.

Videos are found by the LLM provider, not by keyword search: one web-search
conversation per interest turns up candidate video URLs (with a relevance
estimate each), then a single batched `videos.list` call verifies the ids
actually exist and are recent — 1 quota unit instead of 100 per
`search.list` query. Transcript fetches are the scarce resource, so only the
top `max_transcript_fetches` videos by estimate are fetched each cycle,
paced a couple of seconds apart.

Because a 90-minute video can hide one great 6-minute discussion, a video
with a fetched transcript isn't scored whole: the transcript is sliced into
a fixed, overlapping time window, and one `CandidateItem` per window is
emitted, each with its own start/end time and deep link
(`...watch?v=ID&t=<seconds>`). A video whose transcript wasn't fetched this
cycle — no captions, the endpoint blocked, or simply ranked below the fetch
budget — degrades gracefully instead of being dropped: it's still emitted as
one video-level candidate (title + description, no deep link), so the
discovery + verification work already spent on it isn't wasted. Either way a
video is processed exactly once — a video-level item is never later
"upgraded" to segments if transcripts come back.

```json
"sources": ["youtube"],
"source_config": {
  "youtube": {
    "max_candidate_videos": 10,
    "max_transcript_fetches": 4,
    "recency_days": 14,
    "chunk_seconds": 360,
    "chunk_overlap_seconds": 60
  }
}
```

All knobs above are optional and shown at their defaults.

```bash
python -m app discover youtube
```

## Providers

The pipeline only ever holds an `LLMProvider` (`complete_json` / `search_json`),
so swapping vendors is a config change:

```bash
DISCOVERY_PROVIDER=claude_chat # default, model claude-opus-5 -- claude.ai session, no API key
DISCOVERY_PROVIDER=anthropic   # direct Anthropic API, needs ANTHROPIC_API_KEY
DISCOVERY_PROVIDER=openai      # model gpt-5, needs OPENAI_API_KEY and `pip install openai`
```

`claude_chat` rides your claude.ai subscription instead of an API key: it runs
`fetch()` against claude.ai's internal chat endpoints inside a real,
already-logged-in Chrome tab, over the DevTools Protocol (the same mechanism
the sibling `ai` repo's export scripts and council bot use). It needs Chrome
launched with `--remote-debugging-port=9222` (`CLAUDE_BROWSER_PORT` to
change), a claude.ai tab logged in inside that window, and `CLAUDE_ORG_ID` in
`.env`. Each call creates a scratch conversation, reads the streamed reply,
and deletes it. Two caveats: the endpoints are internal and undocumented (they
can drift), and heavy automated use of claude.ai sits uneasily with its terms
-- the per-cycle score budget keeps volume modest.

No vendor SDK is imported outside `discovery/providers/`. A capability a
provider lacks — OpenAI has no equivalent of Claude's server-side web search —
raises `UnsupportedCapability` and that collector is skipped like any other
failure, so `web_search` needs a Claude provider (`claude_chat` uses
claude.ai's own web_search tool; `anthropic` uses the API's).

## Telegram

Every push is either an **🚨 ALERT** (today: `stocks`'s market-move events —
sent the moment they clear the bar) or a **🔎 DISCOVERY** item (everything
else — held and sent later in a batch). A message looks like:

```
🔎 DISCOVERY  ·  🔥 91% interesting  ·  NBIS / Nebius

<title>

<why it's worth your time>

Why this matches me:
<what this has that generic coverage doesn't>

https://...
```

Four feedback buttons ride under every message — 🔥 Very interesting,
👍 Interesting, 👎 Not interesting, 🗑 Bad match:

```bash
python -m app run-once               # sends Alerts immediately as they clear the bar
python -m app digest                 # send the pending Discovery digest right now
python -m app listen                 # long-poll Telegram, record button presses (blocking)
python -m app listen --drain         # or: one bounded pass, for a scheduled task
```

`listen`/`listen --drain` and the collect/digest commands are independent —
one records feedback, the others collect/score/send; nothing here shares a
process. `listen` and `listen --drain` share the same persisted Telegram
offset, so a button press that arrives while nothing is listening is caught
by the next drain instead of being lost.

A failed send is recorded as not-delivered and retried on a later delivery
pass — after a cool-off (`DISCOVERY_SEND_RETRY_SECONDS`, default 30 minutes),
up to `DISCOVERY_SEND_MAX_ATTEMPTS` attempts in total (default 5) — so a
transient Telegram outage delays a push rather than losing it. A delivered
message is final: retries never duplicate it.

### Creating the bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, follow
   the prompts — you get a token (`TELEGRAM_BOT_TOKEN`).
2. Message your new bot anything (bots can't message you first), then visit
   `https://api.telegram.org/bot<token>/getUpdates` and read `message.chat.id`
   off the reply — that's `TELEGRAM_CHAT_ID`.
3. Put both in `.env`. Unset, pushes print to stdout instead of sending.

## Feedback

```bash
python -m app items --min-score 0.7          # ids and scores
python -m app feedback 42 fire --interest-id 2 --note "exactly what I wanted"
```

Verdicts: `fire` / `up` / `down` / `trash` — the same four as the Telegram
buttons, which record feedback the same way (`feedback_listener.listen`,
`db.add_feedback`). Every event stores the item, the interest, the score at
the time, the verdict, and a timestamp. Recent verdicts for an interest are
fed back into its scoring prompt as worked examples; nothing retrains or
re-ranks automatically. `python -m app stats` is where the feedback earns its
keep — it's what tells you whether the score tracks your verdicts.

## Teach

```bash
python -m app teach                          # interactive: label the highest-value queue
python -m app teach --list                   # print the ranked queue, no prompting
python -m app teach --explain --limit 20      # ranker vs recency-baseline readout
python -m app teach --send --dry-run          # push top items to Telegram (prints instead of sending)
```

Labeling everything scored is slow, and not every label teaches equally.
`teach` ranks already-scored, not-yet-labeled items by expected information
value — bar proximity, model self-uncertainty, and interest label scarcity
(see `discovery/teach.py`'s docstring for the formula) — and records verdicts
through the exact same `db.add_feedback` call the Telegram buttons use.
`--send` pushes the top of the queue to Telegram instead, reusing
`notify.format_message`/`feedback_keyboard`, so those labels come back
through the existing `listen`/`listen --drain` flow. `--explain` is the
acceptance-evidence command: it reports the ranked queue's `band_share` /
`mean_gap` / `mean_confidence` / `interests_covered` against the same pool
ordered by recency, honestly — including if recency wins.

## Stats

```bash
python -m app stats             # last 7 days
python -m app stats --days 30
```

The point of this command is to let you decide, after a week, whether the thing
is worth keeping. It reports:

- **The funnel** — candidates collected, how many survived dedup and the cheap
  pre-filter, how many reached the LLM, how many became notifications. A funnel
  that collects hundreds and notifies none means the bar is too high (or the
  collectors are fetching noise); one that notifies almost everything collected
  means the bar is too low.
- **Notifications per interest** — which interests are actually producing, and
  what share of their scored items clear their own `min_score`.
- **Feedback rates** — 🔥 / 👍 / 👎 / 🗑 as a share of what you rated, plus how
  much of what was sent you bothered to rate at all.
- **Average score per feedback verdict** — the one number that says whether the
  *scoring* works. If 🔥 items don't average clearly above 🗑 ones, the model is
  not measuring what interests you and `models.WEIGHTS` needs work; collecting
  harder will not fix it.
- **Estimated LLM/API cost** — priced from token counts and web-search requests
  recorded per model. A model with no published price still gets its token
  counts shown, and is excluded from the dollar total rather than guessed at.
  `claude_chat` usage is reported as call counts only: the claude.ai session
  carries no token metering and is covered by the subscription, so no dollar
  figure is invented for it.

Counters are written as the pipeline runs (dedup and the pre-filter destroy
their candidates, so the funnel can't be reconstructed after the fact) and
usage is drained from the provider at the end of each cycle. Both are keyed by
day, so a window is just a date filter.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLAUDE_ORG_ID` | *(none)* | **Required** for the default provider — your claude.ai organization id |
| `CLAUDE_BROWSER_PORT` | `9222` | Chrome DevTools port the default provider attaches to |
| `DISCOVERY_CHROME_LAUNCH_CMD` | *(none)* | Optional `cmd /d /c` command `run-once` runs ONCE if the provider preflight check finds Chrome/CDP down. Empty ⇒ never spawn anything. **Must be a detached form** (e.g. `start "" "C:\...\chrome.exe" --remote-debugging-port=9222`) — a non-detached command blocks run-once until the wait below is hit |
| `DISCOVERY_CHROME_LAUNCH_WAIT_SECONDS` | `15` | How long to wait after `DISCOVERY_CHROME_LAUNCH_CMD` before re-checking preflight (also bounds how long the launch command itself is allowed to run) |
| `ANTHROPIC_API_KEY` | *(none)* | Required only when `DISCOVERY_PROVIDER=anthropic` |
| `OPENAI_API_KEY` | *(none)* | Required only when `DISCOVERY_PROVIDER=openai` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(none)* | Unset ⇒ pushes print to stdout |
| `DISCOVERY_PROVIDER` | `claude_chat` | `claude_chat`, `anthropic` or `openai` |
| `DISCOVERY_MODEL` | per provider | `claude-opus-5` / `gpt-5` |
| `DISCOVERY_DB` | `discovery.db` | SQLite file (gitignored; rebuildable) |
| `DISCOVERY_INTERESTS` | `interests.json` | Interests file |
| `DISCOVERY_MAX_ITEMS` | `8` | Items per source per cycle |
| `DISCOVERY_MAX_SCORES` | `25` | Hard cap on LLM scoring calls per cycle. Anything over waits for the next cycle |
| `DISCOVERY_PERSONAL_STATE` | `personal_state.json` | Path to the `ai` repo's personal-state artifact — see [Personal-state contract](#personal-state-contract) |
| `DISCOVERY_INTERVAL_STOCKS` | `3600` | `run-once --source stocks` cadence, seconds (read by the OS scheduler) |
| `DISCOVERY_INTERVAL_WEB` | `14400` | `run-once --source web_search` cadence, seconds |
| `DISCOVERY_INTERVAL_YOUTUBE` | `14400` | `run-once --source youtube` cadence, seconds |
| `DISCOVERY_DIGEST_TIME` | `08:00` | Local time-of-day the `digest` job sends the Discovery digest |
| `DISCOVERY_DIGEST_MAX` | `10` | Discovery items per digest, highest score first |
| `DISCOVERY_MIN_MATCH` | `0.25` | Pre-filter: weakest interest match worth scoring |
| `DISCOVERY_MIN_TEXT_CHARS` | `120` | Pre-filter: least text worth sending to an LLM |

`--provider`, `--model` and `--db` override the environment for one run.

## Running it as an appliance

There is no in-process scheduler (see [How a cycle works](#how-a-cycle-works))
-- an OS scheduler has to call the commands above on their own cadence.
`ops/install_tasks.py` registers six one-purpose Windows Scheduled Tasks
(`internet-discovery-collect-stocks/-web/-youtube`, `-digest`, `-feedback`,
`-health`), one XML task per job, trigger intervals read straight from
`config.load()` so a `.env` change and a re-`--install` is all it takes to
reschedule.

**One manual prerequisite:** Chrome has to be running, in the same
interactive Windows session the tasks run in, launched with
`--remote-debugging-port=9222` and logged into claude.ai -- that's the
default provider's only way in, and it doesn't exist under a service
principal. `run-once` won't even try a collector without it (see the
provider preflight in [Commands](#commands)); `DISCOVERY_CHROME_LAUNCH_CMD`
can relaunch it once automatically, see [Configuration](#configuration).

```bash
python ops/install_tasks.py --dry-run              # print every task's XML + schtasks command, register nothing
python ops/install_tasks.py --install              # create/update all six tasks
python ops/install_tasks.py --status               # state, last run, last result, next run
python ops/install_tasks.py --uninstall            # delete only the six tasks this script created
python ops/install_tasks.py --uninstall --dry-run  # preview the deletion instead of running it
python ops/install_tasks.py --soak                 # register the one-shot 24h soak checkpoint, see ops/SOAK.md
```

`--dry-run` composes with `--install`, `--uninstall` and `--soak` (preview instead of
touching `schtasks`); it has nothing to preview against `--status`, which is
already read-only, so that combination is rejected.

Each task runs `ops/run.cmd`, which sets `PYTHONIOENCODING=utf-8`, `cd`s to
the repo root and runs `python -m app <args>`, appending stdout+stderr to
`logs\<args>-<YYYYMMDD>.log` (the log name comes from the full argument
list, not just the first one -- the three `run-once --source ...` collect
tasks all start with `run-once` and would otherwise collide on one
exclusively-locked file; gitignored, inbound-only -- never committed) and
propagating the exit code. `python -m app health` (or
`python -m app stats`, which includes the same HEALTH section) is the
fastest way to check the appliance is actually alive without digging through
logs.

## Tests

```bash
python test_discovery.py
```

252 tests, network fully stubbed — they never hit an LLM API, Telegram, or
Yahoo. The provider seam is the whole stub: a fake object with `complete_json`
and `search_json`.
