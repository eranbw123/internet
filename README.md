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
| `init` | Create/upgrade `discovery.db` and reconcile `interests.json` into it |
| `sync [--dry-run] [--force]` | Reconcile `interests.json` into a running database: upsert edits, deactivate entries the file dropped or marked `"active": false`, and cancel those interests' pending missions. Takes effect on the next cycle — no re-`init`, no manual DB write. `--dry-run` prints the plan; `--force` overrides the truncated-file guard — see [interests.json](#interestsjson) |
| `run-once [--source X]` | One collect → score → notify cycle (stocks/youtube; web discovery is scheduled via `web-tick` instead, see below). Gated by a provider preflight — exits 3 without touching a collector/LLM if Chrome/CDP is down |
| `web-tick` | One continuous Council-driven web discovery tick — replenish, lease and execute a fair slice of pending research missions through the real pipeline. Gated by the mission provider's own preflight, same exit-3 convention — see [Continuous web discovery](#continuous-web-discovery-council-missions) |
| `listen` | Long-polls Telegram for feedback buttons, blocking — interactive use |
| `listen --drain` | One bounded feedback pass instead of blocking — for a scheduled task |
| `digest` | Send the pending Discovery digest now |
| `discover <source>` | Run one collector across every interest and print what it found — never sends |
| `score` | Push one candidate through the real pipeline and print the verdict |
| `items` | List recently scored items |
| `feedback <id> <verdict>` | Rate an item from the CLI |
| `stats [--days N]` | Funnel, feedback rates, estimated cost and a HEALTH section — see [Stats](#stats) |
| `ui [--host] [--port] [--public]` | Serve the Observatory: a read-only Datasette UI + JSON API over the trace tables — see [Observatory](#observatory) |
| `interests [--layer L] [--why KEY] [--refresh]` | Layered interest state: list (owner rows first, `--layer` filters), `--why <key>` prints the append-only provenance chain, `--refresh` runs promotion/decay — a no-op unless `DISCOVERY_DYNAMIC_INTERESTS` is set — see [Layered interest state](#layered-interest-state) |
| `health [--notify]` | Job staleness, provider reachability, pending/abandoned sends; `--notify` alerts on degraded/recovery, rate-limited |
| `personal-state [--path]` | Print the sibling `ai` repo's personal-state artifact as this repo would read it — see [Personal-state contract](#personal-state-contract) |
| `teach [--list\|--explain\|--send]` | Label the highest information-value scored-but-unlabeled items — see [Teach](#teach) |
| `trace-fixture --db PATH` | Build the deterministic trace acceptance fixture (offline, fake providers) at `PATH`. `--db` is required — refuses (exit 2) rather than defaulting to the production `discovery.db`, since the fixture writes real interests/items/scores/feedback through production code paths — see [Trace backbone](#trace-backbone) |

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
place rather than duplicating it. Every interest loaded from this file is
layer `owner` — the layer that's immutable to automation; see
[Layered interest state](#layered-interest-state) for how the system can add
its own interests alongside these.

**This file is the source of truth in both directions.** Edit it, then run
`python -m app sync` — the change is live on the next cycle:

```bash
python -m app sync --dry-run    # what would change, writes nothing
python -m app sync              # apply it
```

| In the file | In the database |
| --- | --- |
| present, new | created and live |
| present, saying nothing about `active` | definition updated; **liveness left alone**, so an interest the decay sweep auto-paused stays paused |
| present with `"active": false` | **retired** — the definition is kept, so reviving it is one flag rather than re-authoring the entry |
| present with `"active": true` | forced live, overruling the sweep |
| present again after being retired | revived — re-adding an entry *is* the revival |
| removed entirely | **retired**, and its pending research missions are cancelled |

Retirement and revival are not written here directly: they go through the
interest lifecycle state machine in `discovery/offers.py`
(`active`/`decaying`/`paused`/`retired`), so there is one deactivation
mechanism in the engine and the `lifecycle` and `active` columns can never
disagree. Every actual change appends an `owner_sync` row to `interest_events`
(`python -m app interests --why <key>` prints the chain); an unchanged file
writes nothing at all, so `sync` is safe to run on a schedule. Retiring an
interest never deletes its row — its items, scores and provenance stay
queryable.

Because the file is the source of truth, a retirement made only in the
database (`offers.retire_interest()`) while the entry still sits in the file
saying nothing is undone by the next sync. `interest_sync.set_entry_active()`
is the call that writes the decision back to the file, and
`interest_sync.entry_writer()` is the callable `offers.accept(sync=...)`
takes — accepting an offer writes its entry into `interests.json`, syncs it
into the database, and activates the offer in one call.

Because a truncated or half-written file must not be able to retire the whole
engine, `sync` refuses any run that would deactivate more than half the active
interests (above 3 of them) and tells you to re-run with `--force` if you
meant it.

`min_score` is a **0–1** threshold on the final score. (It used to be 0–100;
anything above 1 is treated as the old scale and divided by 100, so a stale
`75` doesn't silently mean "never notify".)

## Layered interest state

Off by default (`DISCOVERY_DYNAMIC_INTERESTS`, unset) — with it off, `interests
--refresh` prints a no-op message and nothing derived is ever created,
queried, or costs an LLM/network call. Turned on, the system can propose its
own interests instead of only scoring against the ones you wrote in
`interests.json`.

Every interest has a `layer`: `owner` (from `interests.json`, always
present, never touched by automation — enforced at the SQLite level, not
just in code) or one of four automation-managed layers a term climbs
through as evidence accumulates — `exploratory` → `emerging` → `inferred` —
or falls to: `retired`. Promotion needs enough distinct-day observations and,
past a point, positive feedback; going quiet for a while demotes one rung,
and negative-feedback-dominant terms retire immediately. A retired term can
re-enter, but only at `exploratory` and at a higher bar than first entry, so
it can't flap in and out. `exploratory`/`emerging` are visible but inactive
(`active=0`, unscored, zero spend — purely for you to review); only
`inferred` interests are actually scored, at a `min_score` floor
(`DISCOVERY_DERIVED_MIN_SCORE`, `0.80`) at or above today's owner bars, and
against items owner collectors already fetched — turning one on never adds a
new collector call. At most `DISCOVERY_DERIVED_MAX_ACTIVE` (`5`) are active
at once; anything past the cap stays pending rather than being scored.

Every promotion, demotion, and retirement is appended to an
`interest_events` log — nothing is ever overwritten or deleted from it — so
`python -m app interests --why <key>` can show the full chain that led a
term to its current layer. `python -m app interests --refresh` runs one
promotion/decay pass; `python -m app interests [--layer L]` lists current
state, owner rows first.

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
| `web_search` | working | The provider's server-side web search returns candidate articles. Scheduled discovery no longer calls this directly -- see [Continuous web discovery](#continuous-web-discovery-council-missions); `discover web_search` / `run-once --source web_search` still work standalone |
| `stocks` | working | Notable price moves via `watch.py`'s Yahoo fetch |
| `youtube` | working | Recent uploads/search results; transcript available → one candidate **per segment**, otherwise one **video-level** candidate (title + description) |

A collector is one function — `collect(interest, cfg, provider, conn=None) -> list[CandidateItem]`
— registered in `discovery/collectors/__init__.py`. Collectors fetch and shape
only: they don't normalize, dedup, or judge relevance. One that raises is
logged and skipped; the rest of the cycle still runs. `conn` is read-only and
exists for one job: checking what's already stored so a collector can skip work
it would otherwise *pay* for (`stocks`' catalyst explanation, `youtube`'s
transcript fetch) only for dedup to discard it a moment later.

## Continuous web discovery (Council missions)

`python -m app web-tick` replaces the old periodic `run-once --source
web_search` batch as the scheduled path for web discovery (see [Running it
as an appliance](#running-it-as-an-appliance) -- the `collect-web` task now
runs this every `DISCOVERY_INTERVAL_WEB` seconds, default 60). Instead of one
static prompt per interest every few hours, a durable queue of
Council-generated research missions is continuously replenished and drained:

1. `discovery/council.py` simulates an "LLM Council" (five independent
   advisor personas → anonymized peer review → Chairman synthesis, ported
   from the sibling `ai` repo's `council_bot.py`) in one `complete_json`
   call, returning N genuinely distinct research missions for one interest
   (label + rationale + a complete, self-contained executor prompt). The
   Council only ever sees the interest's own definition, recent discovery
   frontier, recent feedback and mission history -- never `min_score`,
   scoring dimensions, or anything else it could be tempted to game.
2. Each tick reclaims stale mission leases, replenishes **at most one**
   owner interest whose `PENDING` mission count is below
   `DISCOVERY_MISSION_LOW_WATER`, then leases and executes a fair slice
   (`DISCOVERY_MISSIONS_PER_TICK`) of pending missions across owner
   interests -- round-robin, so one interest's queue never starves another.
   An interest whose most recent planning call just failed is skipped by
   replenish for `DISCOVERY_MISSION_RETRY_SECONDS`, so a broken Council
   can't burn a real provider call on it every single tick.
3. Every leased mission runs independently via the search-capable
   `DISCOVERY_MISSION_PROVIDER` (default `chatgpt_browser`); a failure in
   one mission is recorded and never stops the others. Discoveries flow
   through the exact same `pipeline.ingest()`/`deliver()` as every other
   collector -- same dedup, matching, scoring, budgets and Telegram format.
4. If Council generation fails `DISCOVERY_COUNCIL_MAX_CONSECUTIVE_FAILURES`
   times in a row for an interest, one `static-fallback` mission (the old
   `web_search.PROMPT`) is queued so that interest keeps producing while the
   Council is down.

All state lives in `search_generations`/`search_missions` (SQLite) -- a
tick is short-lived, idempotent and safe to overlap; a crash mid-tick just
leaves a mission `RUNNING` until its lease expires, at which point the next
tick reclaims it. There is still no in-process scheduler.

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
DISCOVERY_PROVIDER=claude_chat     # default, model claude-opus-5 -- claude.ai session, no API key
DISCOVERY_PROVIDER=chatgpt_browser # model latest-high -- chatgpt.com session, no API key
DISCOVERY_PROVIDER=anthropic       # direct Anthropic API, needs ANTHROPIC_API_KEY
DISCOVERY_PROVIDER=openai          # model gpt-5, needs OPENAI_API_KEY and `pip install openai`
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

`chatgpt_browser` drives chatgpt.com the same way -- no API key, no
`CLAUDE_ORG_ID`, just a logged-in chatgpt.com tab in the same Chrome
(`CHATGPT_BROWSER_PORT`, falling back to `CLAUDE_BROWSER_PORT`, default 9222).
Its default model is the sentinel `latest-high`, resolved live per call to
chatgpt.com's newest version at its High (max-reasoning) preset, so a new
model generation needs no code change; pin explicitly with a bare slug or
`slug:effort`. Sending a message is more involved than Claude's because
chatgpt.com gates its send behind a "sentinel" proof-of-work challenge the
read endpoints don't require -- solved in-page with an embedded SHA3-512.
Same caveats as `claude_chat` (undocumented endpoints, ToS-gray automated use).
-- the per-cycle score budget keeps volume modest.

No vendor SDK is imported outside `discovery/providers/`. A capability a
provider lacks — OpenAI has no equivalent of Claude's server-side web search —
raises `UnsupportedCapability` and that collector is skipped like any other
failure, so `web_search` needs a provider with search: `claude_chat` (claude.ai's
own web_search tool), `chatgpt_browser` (chatgpt.com's search, requested per
message) or `anthropic` (the API's) — not the direct `openai` provider.

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
- **Missions** — [Council mission](#continuous-web-discovery-council-missions)
  generation done/failed counts in the window, plus the mission queue's
  all-time status breakdown (pending/running/done/failed) — whether the
  Council is actually planning and whether missions are draining or piling up.
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
| `CLAUDE_BROWSER_PORT` | `9222` | Chrome DevTools port the browser providers attach to |
| `CHATGPT_BROWSER_PORT` | *(CLAUDE_BROWSER_PORT)* | Chrome DevTools port for `chatgpt_browser`; falls back to `CLAUDE_BROWSER_PORT`, then 9222 |
| `DISCOVERY_CHROME_LAUNCH_CMD` | *(none)* | Optional `cmd /d /c` command `run-once`/`web-tick` run ONCE if the provider preflight check finds Chrome/CDP down. Empty ⇒ never spawn anything. **Must be a detached form** (e.g. `start "" "C:\...\chrome.exe" --remote-debugging-port=9222`) — a non-detached command blocks run-once until the wait below is hit |
| `DISCOVERY_CHROME_LAUNCH_WAIT_SECONDS` | `15` | How long to wait after `DISCOVERY_CHROME_LAUNCH_CMD` before re-checking preflight (also bounds how long the launch command itself is allowed to run) |
| `ANTHROPIC_API_KEY` | *(none)* | Required only when `DISCOVERY_PROVIDER=anthropic` |
| `OPENAI_API_KEY` | *(none)* | Required only when `DISCOVERY_PROVIDER=openai` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(none)* | Unset ⇒ pushes print to stdout |
| `DISCOVERY_PROVIDER` | `claude_chat` | `claude_chat`, `chatgpt_browser`, `anthropic` or `openai` |
| `DISCOVERY_MODEL` | per provider | `claude-opus-5` / `latest-high` (chatgpt.com: newest version at its High/max-reasoning preset, resolved live; or a bare slug / `slug:effort` to pin) / `gpt-5` |
| `DISCOVERY_DB` | `discovery.db` | SQLite file (gitignored; rebuildable) |
| `DISCOVERY_INTERESTS` | `interests.json` | Interests file |
| `DISCOVERY_MAX_ITEMS` | `8` | Items per source per cycle |
| `DISCOVERY_MAX_SCORES` | `25` | Hard cap on LLM scoring calls per cycle. Anything over waits for the next cycle |
| `DISCOVERY_PERSONAL_STATE` | `personal_state.json` | Path to the `ai` repo's personal-state artifact — see [Personal-state contract](#personal-state-contract) |
| `DISCOVERY_DYNAMIC_INTERESTS` | *(off)* | `1`/`true` enables the layered interest state (`interests --refresh`, `discovery/interest_state.py`). Off by default: no derived interest is ever created |
| `DISCOVERY_DERIVED_MAX_ACTIVE` | `5` | Max derived interests promoted to `inferred` (active, scored) at once |
| `DISCOVERY_DERIVED_MIN_SCORE` | `0.80` | Floor `min_score` a derived interest is held to, at or above today's owner bars |
| `DISCOVERY_INTERVAL_STOCKS` | `3600` | `run-once --source stocks` cadence, seconds (read by the OS scheduler) |
| `DISCOVERY_INTERVAL_WEB` | `60` | `web-tick` cadence, seconds — see [Continuous web discovery](#continuous-web-discovery-council-missions) |
| `DISCOVERY_INTERVAL_YOUTUBE` | `14400` | `run-once --source youtube` cadence, seconds |
| `DISCOVERY_DIGEST_TIME` | `08:00` | Local time-of-day the `digest` job sends the Discovery digest |
| `DISCOVERY_DIGEST_MAX` | `10` | Discovery items per digest, highest score first |
| `DISCOVERY_MIN_MATCH` | `0.25` | Pre-filter: weakest interest match worth scoring |
| `DISCOVERY_MIN_TEXT_CHARS` | `120` | Pre-filter: least text worth sending to an LLM |
| `DISCOVERY_EXPLORE_MAX_SCORES` | `5` | Separate per-cycle LLM score cap for exploration (derived/inferred-interest) items — see [Exploration lane](#exploration-lane-step-10) |
| `DISCOVERY_MISSION_PROVIDER` | `chatgpt_browser` | Search-capable provider `web-tick` executes missions with (and plans missions with, via the Council) |
| `DISCOVERY_MISSION_MODEL` | per `DISCOVERY_MISSION_PROVIDER` | Model for the mission provider — same forms as `DISCOVERY_MODEL` |
| `DISCOVERY_COUNCIL_MISSIONS_PER_GENERATION` | `6` | Missions requested per Council planning call |
| `DISCOVERY_MISSION_LOW_WATER` | `3` | Replenish an interest's mission queue once its `PENDING` count drops below this |
| `DISCOVERY_MISSIONS_PER_TICK` | `2` | Missions leased + executed per `web-tick`, round-robined fairly across owner interests |
| `DISCOVERY_MISSION_MAX_SEARCHES` | `6` | `search_json`'s own max searches, per mission |
| `DISCOVERY_MISSION_MAX_RESULTS` | `6` | CandidateItems kept per mission |
| `DISCOVERY_COUNCIL_FRONTIER_ITEMS` | `15` | Recent candidate items shown to the Council as planning context |
| `DISCOVERY_COUNCIL_FEEDBACK_ITEMS` | `10` | Recent feedback rows shown to the Council |
| `DISCOVERY_COUNCIL_HISTORY_MISSIONS` | `12` | Recent past missions (label + rationale) shown to the Council, so it doesn't repeat an angle |
| `DISCOVERY_MISSION_LEASE_SECONDS` | `900` | How long a leased (`RUNNING`) mission holds its lease before a future tick reclaims it as stale |
| `DISCOVERY_MISSION_MAX_ATTEMPTS` | `3` | Attempts before a mission is retired to `FAILED` |
| `DISCOVERY_MISSION_RETRY_SECONDS` | `1800` | Cool-off before a failed mission is retried; also how long an interest whose latest Council planning call just failed is skipped by replenish |
| `DISCOVERY_COUNCIL_MAX_CONSECUTIVE_FAILURES` | `3` | Consecutive Council planning failures for one interest before the static fallback mission is queued |
| `DISCOVERY_TRACE` | `1` (on) | Enables the trace backbone (`discovery/trace.py`) — see [Trace backbone](#trace-backbone). `0`/`false` is the rollback lever: every Tracer method becomes a no-op and no `trace_*` table is written |
| `DISCOVERY_OBSERVATORY_BASE_URL` | *(empty)* | Base URL of a running `ui` instance — set it to make `feedback_keyboard()` append a "🔬 Open full trace" Telegram button; see [Observatory](#observatory) |
| `DISCOVERY_UI_TOKEN` | *(empty)* | Bearer token `ui --public` requires (refuses to start without it) — every route, ours and Datasette's native ones alike, is 403 without it |
| `DISCOVERY_NGROK_CMD` | *(empty)* | `cmd /d /c` command `ui --public` launches ngrok with (`{port}` substituted); also required for `--public` to start — see [Observatory](#observatory) |

`--provider`, `--model` and `--db` override the environment for one run.

## Trace backbone

Every `run-once`/`web-tick` cycle is recorded, at the same seams the pipeline
already has, into four append-only tables so a later question ("why didn't
this get notified?") can be answered by reading, not by adding print
statements:

- **`trace_runs`** — one row per command invocation (`kind`: `web-tick`,
  `run-once`, `digest`, `feedback`, `fixture`, ...).
- **`trace_nodes`** — one row per step (a candidate, a match, a score, a
  Council advisor, a mission, a Telegram send, ...). Nodes that correspond to
  a DB row carry `entity_type`/`entity_id` (`candidate_items`, `scores`,
  `search_missions`, `search_generations`, `interests`, `notifications`,
  `feedback`) so a UI can deep-link straight to it.
- **`trace_edges`** — `from_node_id -> to_node_id`, one of a fixed
  relationship vocabulary (`generated`, `selected`, `executed`, `returned`,
  `normalized_to`, `duplicate_of`, `matched`, `rejected`, `deferred`,
  `scored`, `cleared_threshold`, `rendered`, `sent`, `failed`, `retried_as`,
  `feedback_on`).
- **`model_calls`** — one row per provider call **attempt** (a JSON-retry or
  a connection-level reconnect each get their own row; nothing is ever
  overwritten), with the exact final prompt actually sent (after every
  framing/schema/retry suffix), the raw reply, the parsed result, and the
  validation outcome. Central: every provider funnels through
  `LLMProvider._emit_call()` (`discovery/providers/base.py`), so
  council.py/missions.py/scoring.py never write their own logging — they only
  set which node a provider call belongs to, via `tracer.calls(role, node_id)`.

A threshold node snapshots `final_score` and the interest's `min_score` **as
they were at scoring time** — a later change to an interest's bar never
rewrites what an old trace says happened.

Redaction (`discovery/trace.py`'s `redact`/`redact_json`): every environment
variable whose name matches `(?i)(token|secret|key|password|cookie|auth)` and
whose value is at least 8 characters has its literal *value* substituted with
`[REDACTED:<VARNAME>]` everywhere it would otherwise appear in a trace row
(config snapshots, prompts, responses) — the length floor keeps a short
secret-shaped value (e.g. `AUTH_MODE=on`) from substring-rewriting unrelated
stored text. Everything else — prompt/interest content — is stored byte-exact.
Two config fields are masked by NAME instead, independent of their value's
shape or length, before a config snapshot is stored: `ui_token` (the only
access credential in `ui --public`, and often short) and `ngrok_cmd` (a
free-form shell command that commonly embeds an inline `--authtoken`, so its
field name alone never matches the secret-name pattern above) both become
`[REDACTED:FIELD:<name>]` in `trace_runs.config_json`.

**Council deliberation and scoring reasoning** ride the SAME `complete_json`
call the missions/score already needed — no extra spend. The Council's
five-advisor analysis, anonymized peer review, aggregate ranking, rejected
angles and Chairman synthesis are requested alongside the missions array; the
missions array itself stays strictly validated exactly as before this step,
and a missing/malformed deliberation section is recorded as
`{"unavailable": true, "reason": ...}` rather than invented or treated as an
error. Scoring similarly returns (and stores, never scores on) its evidence,
alternative interpretation, and uncertainties.

`python -m app trace-fixture --db PATH` builds a small, structurally
deterministic fixture (one interest, one Council generation with three
missions, a duplicate, a prefilter rejection, a scoring failure + retry, a
below-bar score, and one delivered + feedback-rated discovery) against fake
providers — offline, for the Datasette plugin and React UI (see
[Observatory](#observatory)) to build and test against without a live
Chrome/CDP session. `--db` is
required; the command exits 2 rather than silently writing this fixture
data — including a real feedback row — into the production `discovery.db`.

## Running it as an appliance

There is no in-process scheduler (see [How a cycle works](#how-a-cycle-works))
-- an OS scheduler has to call the commands above on their own cadence.
`ops/install_tasks.py` registers six one-purpose Windows Scheduled Tasks
(`internet-discovery-collect-stocks/-web/-youtube`, `-digest`, `-feedback`,
`-health`), one XML task per job, trigger intervals read straight from
`config.load()` so a `.env` change and a re-`--install` is all it takes to
reschedule. `collect-web` runs `web-tick` (see [Continuous web
discovery](#continuous-web-discovery-council-missions)) on
`DISCOVERY_INTERVAL_WEB`'s cadence, default every 60 seconds -- not a
periodic batch collect.

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

## Observatory

**Launching it:** `ops\observatory.cmd` -- one command, no flags needed. It
`cd`s to the repo root and starts the real UI on `127.0.0.1:8010` (see
[Running it as an appliance](#running-it-as-an-appliance) for why `ops/*.cmd`
scripts always `cd` first). 8010, not the CLI's own 8001 default, because
that's the port a standing external ngrok tunnel already forwards to on this
machine -- override with `ops\observatory.cmd --port 9000` or
`ops\observatory.cmd --public` (any arg is passed straight through to `ui`,
last flag wins). This replaces pointing bare `datasette discovery.db` at the
port by hand, which serves the raw tables with none of this section's
auth/graph/redaction layer.

`python -m app ui [--host 127.0.0.1] [--port 8001] [--public]` serves the
trace backbone above as a browsable, queryable UI: an in-repo Datasette
plugin (`observatory/`) over `discovery.db`, bound to localhost by default.
`datasette` is the one sanctioned new dependency this step adds (see
`requirements.txt`) — it's imported lazily, only inside `observatory/` and
`ui`'s own command handler, so `discovery/` and `test_discovery.py` stay
importable and green on a machine that never installs it.

- **Pages/APIs** (`observatory/plugin.py`, `register_routes()`): `/observatory/`
  (serves the built React frontend — see
  [Observatory frontend](#observatory-frontend-observatoryfrontend)) and JSON
  APIs — `api/list?tab=discoveries|interests|generations|missions|failed`
  (paginated, `limit` capped at 50, filters for date range/interest/layer/
  source/provider/model/mission/sent/feedback verdict/failure stage/trace
  completeness, `search` spanning titles/URLs/summaries and linked
  `model_calls` prompts+responses), `api/graph?entity_type=&entity_id=` or
  `?run_id=` (the full connected trace graph reachable by following
  `trace_edges` in either direction — edges legitimately cross `trace_runs`,
  e.g. a web-tick's `threshold` node `rendered`-links to a later digest
  run's `render` node, which `feedback_on`-links to a still-later listener
  run's `feedback` node, so a single discovery's story usually spans 2-3
  runs), `api/children?node_id=` or `?group=` (lazy-loads a collapsed
  sibling group, capped at 500 rows), `api/node/<id>` (full inspector: exact
  input/output, every `model_calls` row with byte-exact prompts + raw/parsed
  responses, redacted
  run config, direct `/discovery/<table>/<pk>` row URLs), `api/interest/<key>`
  (definition, provenance, `interest_events`, generations/missions/
  discoveries/failures/feedback), `api/compare?a=&b=&kind=run|model_call`
  (line-level `difflib` prompt/response diffs, or added/removed/changed
  nodes+edges — nodes keyed by `(node_type, label, inbound_relationship,
  inbound_ordinal)` so same-labelled siblings stay distinct, edges keyed by
  `(from_key, to_key, relationship, ordinal)`), and
  `trace/score/<score_id>` (an HTML deep link — resolves the score's
  `threshold` trace node, serves the shell with a `focus` bootstrap script
  tag the frontend reads). **Known gap:** `api/compare`'s `kind` is
  `run|model_call` only — `kind=generation` (named alongside runs/model_calls
  in the step objective) isn't implemented and returns 400, same as any other
  unrecognized `kind`; a generation doesn't correlate 1:1 with a `trace_runs`
  row (one web-tick run can carry several interests' generations), so it
  can't just alias the `run` diff. Rejecting outright is deliberate — it's
  what replaced a silent wrong-answer fallback in an earlier repair — but a
  real generation-vs-generation diff is unimplemented, not yet attempted.
- **Swimlanes** (`observatory/db.py`'s `SWIMLANES`): every `node_type` maps to
  one of `interest-state`, `council`, `mission`, `candidate-pipeline`,
  `scoring`, `delivery-feedback`.
- **Collapsed groups**: sibling nodes sharing the same parent, relationship
  AND node_type (e.g. a Council generation's 5 advisor analyses) collapse to
  one `{group, child_count}` placeholder once there are more than 3 — never
  mixing different node types into one group, so a generation's 3 real
  mission branches never hide behind an advisor group's count. `api/children`
  loads the real rows only when asked.
- **Read-only, always**: Datasette is given `files=[cfg.db_path]` — no
  `immutables=`, so `discovery.db` keeps changing live while the UI is open —
  which is what makes Datasette open its own read connections as
  `file:...?mode=ro`; `observatory/db.py` additionally opens its own
  independent `mode=ro` connection per request. The plugin registers no
  write route. `default_allow_sql` stays on (native `/discovery/<table>`,
  `/discovery/<table>/<pk>` and `/discovery/<db>?sql=...` query pages all
  work — datasette 0.65's native SQL surface is `/{db}?sql=...`, not
  `/{db}/-/query`), but any write attempt through them is rejected before
  execution (Datasette's own "Statement must be a SELECT" check), not just
  by convention — proven by a table-row-count-unchanged test plus the
  rejection status code, not just the status code alone. The plugin's own
  `permission_allowed` hook also denies Datasette's write actions
  (insert/update/delete row, create/drop/alter table) outright in both
  modes — inert against 0.65 (none of those routes exist yet), hardening
  against an unpinned future Datasette version adding one.
- **Redaction, twice**: every API response is passed through
  `discovery.trace.redact_json` on the way OUT, independent of task 1's
  at-write redaction — defense in depth, proven with a secret planted
  directly into raw DB bytes (bypassing write-time redaction entirely).
- **Auth** (`DISCOVERY_UI_TOKEN`): the default (`ui`, no `--public`) is open —
  binding to `127.0.0.1` IS the boundary. `--public` refuses to start unless
  BOTH `DISCOVERY_UI_TOKEN` and `DISCOVERY_NGROK_CMD` are set (a `cmd /d /c`
  command, same convention as `DISCOVERY_CHROME_LAUNCH_CMD`; `{port}` in it
  is substituted). In public mode, a Datasette `actor_from_request`/
  `permission_allowed` hook pair becomes a single shared gate: only a
  correct `Authorization: Bearer <token>` (or `?token=`) resolves an actor,
  and only a resolved actor is granted anything — covering our own
  `/observatory/*` routes (checked explicitly) AND every native Datasette
  table/row/SQL page (gated for free, since core already calls
  `permission_allowed` before serving any of them). **Live tunnel
  verification is deferred to an operator session** — this worktree has no
  ngrok binary or network to verify the tunnel itself against; the auth
  boundary above is what the offline tests prove. Since the token in
  `--public` mode can only be carried as `?token=` (a plain URL button can't
  set a header), `ui` disables uvicorn's access log in that mode
  (`access_log=not args.public`) so the token never gets written to a log
  file on disk; private-mode logging is unchanged.
- **Telegram deep link** (`discovery/notify.py`): when `DISCOVERY_OBSERVATORY_BASE_URL`
  is set, `feedback_keyboard()` appends one `🔬 Open full trace` URL button
  (`<base>/observatory/trace/score/<id>`) as a third row, after the four
  existing feedback buttons — byte-identical `callback_data`. Unset (the
  default): byte-identical keyboard to before this button existed.
  **Known limitation: the button and `--public` don't compose on their
  own.** The emitted URL carries no token, and `--public` 403s anonymous
  requests to every route including `/observatory/trace/score/<id>` — so
  tapping the button from Telegram against a public (ngrok) base URL lands
  on a bare 403, not the trace. There is no login route or cookie flow.
  Today the button is only directly usable pointed at a private,
  operator-reachable `DISCOVERY_OBSERVATORY_BASE_URL` (e.g. a VPN/LAN host,
  or manually appending `?token=<DISCOVERY_UI_TOKEN>` to the tapped link in
  the browser once it 403s). Building a real browser-auth path (a
  token-setting entry route, or a signed short-lived link per notification)
  is deferred — same posture as the ngrok live-tunnel deferral above, not
  yet implemented.

Tests: `python test_observatory.py` — offline, via Datasette's own ASGI test
client (`Datasette(...).client`, `httpx` over `ASGITransport`, no socket, no
network) against a fixture db built by the real `discovery/trace_fixture.py`.
Skips every test with a loud message (not a failure) if `datasette` genuinely
isn't installed.

### Observatory frontend (`observatory/frontend/`)

React + TypeScript + React Flow (`@xyflow/react`) + ELK.js (`elkjs`), built
with Vite. Node/npm is a **build-time only** dependency — the built output is
committed to `observatory/static/` (deterministic, non-content-hashed
filenames), so serving it (`python -m app ui`) needs no Node at runtime.

```bash
cd observatory/frontend
npm install
npm run build       # emits into ../static (tsc -b type-check + vite build)
npm test             # vitest: graph assembly, edge labels, diffs, deep links
```

`npm run dev` (Vite dev server, hot reload) works against a running
`python -m app ui` backend for iteration — the dev server itself doesn't
serve the API, only proxy your fetches manually or run against
`http://localhost:8001` directly by adjusting `fetch`'s base if needed. Only
`observatory/frontend/{src,index.html,package.json,vite.config.ts,tsconfig*.json}`
are source; `node_modules/` and Vite's cache are gitignored.

**Mobile**: under a 480px viewport (iPhone width) the explorer becomes a
slide-out drawer (☰ toggle in the header) and the inspector becomes a
full-screen bottom sheet opened by tapping a node; the graph itself stays
touch-pan/pinch-zoom via React Flow's own gesture handling. A
`/observatory/trace/score/<id>` deep link lands directly on the graph with
the sent path highlighted, on both viewport classes. Live iPhone-device and
public-ngrok verification remain deferred to a live operator session — the
iPhone-*sized* viewport (390×844, via CDP `Emulation.setDeviceMetricsOverride`)
covered by the e2e test below is the in-repo evidence.

**E2E smoke test**: `python test_observatory_e2e.py` — stdlib only, drives a
real local headless Chrome over CDP (the same websocket approach
`discovery/providers/cdp.py` uses, pointed at a Chrome instance the test
launches itself rather than an existing authenticated tab). Builds the trace
fixture db, starts `python -m app ui` on an ephemeral localhost port, and at
both a desktop and an iPhone-width viewport: loads the app, selects the
fixture's successful discovery, asserts the duplicate/prefilter-rejection/
scoring-retry/below-threshold/sent branches are all present as real nodes,
expands/collapses a group, pans/zooms, opens the inspector and asserts the
displayed prompt is byte-equal to the fixture's own `model_calls` row,
exercises the copy button, and opens the score deep link and asserts the
sent path is highlighted. No network beyond localhost. Skips (loudly, not a
failure) only when no Chrome/Chromium binary is found — set
`DISCOVERY_UI_E2E_CHROME` to point at a non-standard install path. **A
missing `observatory/static/` build does NOT skip this test — it fails**,
since that's a real gap the test exists to catch, not an environment
limitation.

## Tests

```bash
python test_discovery.py
```

445 tests, network fully stubbed — they never hit an LLM API, Telegram, or
Yahoo. The provider seam is the whole stub: a fake object with `complete_json`
and `search_json`.

This task (observatory) adds 6 more of its own on top of that count —
`test_discovery.py` actually runs 458 in this branch; the `445` above is
`automation/integration`'s own count as of its last docs-sync commit and is
expected to be corrected by a follow-up sync once this branch lands, the
same way integration's own count was corrected before (see PROJECT_STATE.md
for the authoritative current total). Also run `python test_watch.py`
(10 tests, the Yahoo helper) and `python test_observatory.py` (65 tests,
offline via Datasette's own ASGI test client — see
[Observatory](#observatory)); both are separate suites, so
`test_discovery.py` stays importable and green on a machine without
`datasette` installed. `test_observatory.py` skips itself with a loud
message instead of failing if `datasette` isn't installed.

A fourth, separate suite — `python test_observatory_e2e.py` (real headless
Chrome over CDP) — is not part of this always-run list: it needs a built
`observatory/static/` bundle (see
[Observatory frontend](#observatory-frontend-observatoryfrontend)) to pass,
and only skips (rather than failing) when no Chrome binary exists.

## Provenance chain (step-08)

A personal-state seed's origin isn't just visible in `interests --why` — it's
walkable end to end in SQL, from a delivered notification back to the exact
artifact bytes that suggested the interest:

```sql
SELECT
  n.id                                            AS notification_id,
  s.id                                            AS score_id,
  s.final_score                                   AS score,
  ci.id                                            AS item_id,
  ci.title                                         AS item_title,
  it.key                                           AS interest_key,
  it.layer                                         AS interest_layer,
  ev.id                                            AS seed_event_id,
  json_extract(ev.evidence, '$.artifact_sha256')   AS artifact_sha256,
  json_extract(ev.evidence, '$.generated_at')      AS artifact_generated_at,
  json_extract(ev.evidence, '$.contract_version')  AS contract_version
FROM notifications n
JOIN scores s           ON s.id = n.score_id
JOIN candidate_items ci ON ci.id = s.item_id
JOIN interests it       ON it.id = s.interest_id
JOIN interest_events ev ON ev.interest_key = it.key AND ev.action = 'seed'
WHERE n.id = ?
```

`interest_events.evidence` is JSON, and every personal-state seed row (see
[Layered interest state](#layered-interest-state)) carries
`origin='personal_state'`, `artifact_sha256` (sha256 of the artifact file's
bytes, read fresh at seed time), the artifact's own `generated_at` and
`contract_version`, the `topic_key` that was seeded, and `seeded_at` — on
BOTH the interest's `provenance` column and this event row, so the chain
above resolves even after the interest itself has since promoted or decayed
away from its seeded state. `test_provenance_chain_query_resolves_every_hop`
in `test_discovery.py` runs this exact query against a fixture and asserts
every hop is non-empty.

### Real-data loop demo (live session only)

This repo's own worktree/CI runs have no `discovery.db` and no
`personal_state.json` — every seeding/leakage/promotion test above runs
against synthetic in-memory fixtures, never real conversation or corpus
data. The full loop, end to end, needs a live operator session (real
Chrome/CDP, Telegram, and the `ai` repo checked out alongside this one):

```bash
# in the ai repo -- produces the contract artifact
python personal_state.py --out personal_state.json

# in this repo -- point at it, turn the flag on, run the loop
set DISCOVERY_PERSONAL_STATE=C:\path\to\ai\personal_state.json
set DISCOVERY_DYNAMIC_INTERESTS=1
python -m app interests --refresh      # seeds top topics as derived:<term> exploratory rows
python -m app run-once                 # collects, matches, scores against active interests
python -m app digest                   # sends anything pending
python -m app listen --drain           # picks up any feedback-button presses
python -m app interests --why derived:<term>   # the full provenance chain, including the seed
```

Nothing here is run against production stores by this implementer session —
the command sequence above is documented, not executed.

## Exploration lane (step-10)

Exploitation (owner interests) and exploration (derived/inferred interests,
see [Layered interest state](#layered-interest-state)) are separated at the
scoring boundary, not just at promotion time. An item's lane is decided once,
by the strongest interest it matched: **explore iff its best match
(`matches[0]` from `matching.match_interests()`) is a non-owner interest** --
that's the interest whose feedback block, `min_score` bar and notification
attribution actually drive the score, so it's what should drive accounting
too. A weaker derived match alongside a stronger owner one still charges
exploitation and behaves exactly as before this step.

Each lane pays from its own `pipeline.Budget`: `DISCOVERY_MAX_SCORES` for
exploitation (unchanged) and `DISCOVERY_EXPLORE_MAX_SCORES` (`5`) for
exploration. With `DISCOVERY_DYNAMIC_INTERESTS` off the exploration budget is
constructed as zero, structurally -- even a stray active derived row can't
spend a score. An exhausted lane's items are deferred and picked up on a
later cycle exactly like today's single-budget overflow; the other lane is
never starved by it.

Exploration outcomes are counted under `explore_scored` / `explore_deferred`
/ `explore_errors` / `explore_notified`, entirely separate metric names from
their exploitation counterparts, so a struggling or noisy exploration lane
can never dilute the FUNNEL/NOTIFICATIONS PER INTEREST numbers `stats`
exists to report on. `stats.report()` grows an EXPLORATION section (only
when there's a non-owner interest, an `explore_*` metric, or the flag on) --
interest counts by layer, the explore_* funnel figures, and a "NOTIFICATIONS
PER DERIVED INTEREST" table shaped like the owner one above it. No new
threshold: a derived interest's `min_score` (already `derived_min_score`,
floor `0.80`) is what a derived score has to clear to notify at all --
distinct budgets and distinct metrics were the missing pieces, not a
distinct bar.

This worktree has no `discovery.db`, so every number above comes from
synthetic in-memory fixtures in `test_discovery.py`'s `ExplorationLaneTests`
-- not a real-corpus reading. Live readout once dynamic interests are
running for real: `python -m app stats --days 7`, EXPLORATION section.


## Interest offers (contract v2)

The `ai` repo can also publish an evidence-bearing **contract v2** artifact:
the same schema as the personal-state one plus a `candidates[]` array, each
candidate a proposed interest with the quotes -- and the conversation ids --
that produced it. `discovery/personal_state.py` reads both versions
(`SUPPORTED_VERSIONS = {1, 2}`); `discovery/offers.py` is what turns
candidates into offers you decide on.

```bash
python -m app offers --import                 # import the artifact (idempotent per sha256)
python -m app offers                          # the inbox: what you're being asked to decide
python -m app offers --why KEY                # quotes, conversations, every score term, event chain
python -m app offers --accept KEY             # prints the interests.json entry it becomes
python -m app offers --reject KEY             # and blocks its terms for 180 days
python -m app offers --snooze KEY             # ask me again in 30 days
python -m app offers --sweep                  # the timers: expiry, decay, auto-pause
python -m app offers --undo KEY               # one-click undo of an auto-pause
```

The artifact path comes from `DISCOVERY_INTEREST_CANDIDATES` (default
`interest_candidates.json` at the repo root -- gitignored, inbound only). An
import is a no-op after the first time it sees a given file's sha256, so a
re-run, a double copy, or a re-copied half-written file cannot duplicate an
offer.

**Ranking is arithmetic, never a model call.** `offers.py` holds no provider
and makes no network call: the model rates (`expected_yield`, and the
similarity of a candidate to each existing interest), and code ranks --
evidence strength .30, recurrence .15, recency .15, novelty .20, expected
yield .20, floored at .45 with a durability gate (3 conversations across 2
months, or a deep 2-conversation dive). At most five offers reach the inbox
per run, one slot reserved for the deliberately exploratory pick.

Dedup against what you already follow is **semantic, not exact-hash**: a key
that normalizes onto an existing interest is dropped, a candidate whose
signals overlap an active interest by half or more is attached to it as
evidence instead of being offered, and a candidate the producer scored >= .70
similar to something you already follow never becomes an offer -- in either
language.

**Decay, and the reversible auto-pause.** `--sweep` also runs the interest
half of the lifecycle. An interest with no item above its bar for 30 days
becomes `decaying` and raises a retirement offer; at 45 days it **auto-pauses
itself, announces that it did, and can be undone with one click**
(`--undo KEY`, which also restarts its silence clock and closes the
retirement offer). Two refusals to judge are built in: nothing is paused when
the pipeline itself has scored nothing recently (it can be paused for days --
that is the pipeline being quiet, not the interest), and nothing is paused
before at least five items have ever been attributed to it.

Accepting an offer does not itself write `interests.json` or the `interests`
table -- that is the sync/write-API path; `accept()` returns the entry and
takes an optional `sync` callable, and `offers.activate()` starts the
interest's lifecycle once its row exists.
