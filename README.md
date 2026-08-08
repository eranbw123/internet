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
python -m app run                           # scheduler: stocks/web/youtube on their own
                                             # cadence, plus a daily digest (see below)
python -m app listen                        # separate process: records feedback-button presses
python -m app stats                         # is it finding anything you care about?
```

## Commands

| Command | What it does |
| --- | --- |
| `init` | Create/upgrade `discovery.db` and load `interests.json` |
| `run-once [--source X]` | One collect → score → notify cycle |
| `run` | The scheduler loop: per-collector cadence plus a daily digest |
| `listen` | Long-polls Telegram for feedback buttons. **Its own process**, alongside `run` |
| `digest` | Send the pending Discovery digest now |
| `discover <source>` | Run one collector across every interest and print what it found — never sends |
| `score` | Push one candidate through the real pipeline and print the verdict |
| `items` | List recently scored items |
| `feedback <id> <verdict>` | Rate an item from the CLI |
| `stats [--days N]` | Funnel, feedback rates and estimated cost — see [Stats](#stats) |

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

## Collectors

| Source | Status | What it does |
| --- | --- | --- |
| `web_search` | working | The provider's server-side web search returns candidate articles |
| `stocks` | working | Notable price moves via `watch.py`'s Yahoo fetch |
| `web` | working | LLM-generated queries, then the provider's web search, one candidate per URL |
| `youtube` | working | Recent uploads/search results, transcript split into overlapping time windows, one candidate **per segment** |

A collector is one function — `collect(interest, cfg, provider, conn=None) -> list[CandidateItem]`
— registered in `discovery/collectors/__init__.py`. Collectors fetch and shape
only: they don't normalize, dedup, or judge relevance. One that raises is
logged and skipped; the rest of the cycle still runs. `conn` is read-only and
exists for one job: checking what's already stored so a collector can skip work
it would otherwise *pay* for (`stocks`' catalyst explanation, `youtube`'s
transcript fetch) only for dedup to discard it a moment later.

### `youtube`

Needs `YOUTUBE_API_KEY` (video search/metadata) and, for transcripts,
`pip install youtube-transcript-api`. A video without an available transcript
in the requested language is logged and skipped, not an error — captions are
off for plenty of videos.

Because a 90-minute video can hide one great 6-minute discussion, this
collector doesn't score whole videos: it fetches the transcript, slides a
fixed, overlapping time window across it, and emits one `CandidateItem` per
window, each with its own start/end time and deep link
(`...watch?v=ID&t=<seconds>`). The existing per-item scoring and threshold
then decide which *segments* are worth a push — nothing extra to configure
for that part.

```json
"sources": ["youtube"],
"source_config": {
  "youtube": {
    "channels": ["UCxxxxxxxxxxxxxxxxxxxxxx"],
    "queries": ["orexin agonist podcast discussion"],
    "max_videos": 5,
    "recency_days": 14,
    "chunk_seconds": 360,
    "chunk_overlap_seconds": 60
  }
}
```

`channels` are channel ids, `queries` are search terms — either or both. All
knobs above are optional and shown at their defaults.

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
python -m app run                    # sends Alerts immediately, per-job schedule
python -m app digest                 # send the pending Discovery digest right now
python -m app listen                 # long-poll Telegram, record button presses (blocking)
```

`run` and `listen` are separate, both long-running processes — one sleeps on
a timer, the other blocks on Telegram's long poll; run both.

A failed send is recorded as not-delivered and retried on a later delivery
pass — after a 15-minute cool-off, up to 3 attempts in total — so a transient
Telegram outage delays a push rather than losing it. A delivered message is
final: retries never duplicate it.

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
| `ANTHROPIC_API_KEY` | *(none)* | Required only when `DISCOVERY_PROVIDER=anthropic` |
| `OPENAI_API_KEY` | *(none)* | Required only when `DISCOVERY_PROVIDER=openai` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | *(none)* | Unset ⇒ pushes print to stdout |
| `DISCOVERY_PROVIDER` | `claude_chat` | `claude_chat`, `anthropic` or `openai` |
| `DISCOVERY_MODEL` | per provider | `claude-opus-5` / `gpt-5` |
| `DISCOVERY_DB` | `discovery.db` | SQLite file (gitignored; rebuildable) |
| `DISCOVERY_INTERESTS` | `interests.json` | Interests file |
| `DISCOVERY_MAX_ITEMS` | `8` | Items per source per cycle |
| `DISCOVERY_MAX_SCORES` | `25` | Hard cap on LLM scoring calls per cycle. Anything over waits for the next cycle |
| `DISCOVERY_INTERVAL_STOCKS` | `3600` | `run`'s stocks job cadence, seconds |
| `DISCOVERY_INTERVAL_WEB` | `14400` | `run`'s web_search+web job cadence, seconds |
| `DISCOVERY_INTERVAL_YOUTUBE` | `14400` | `run`'s youtube job cadence, seconds |
| `DISCOVERY_DIGEST_TIME` | `08:00` | Local time-of-day `run` sends the Discovery digest |
| `DISCOVERY_DIGEST_MAX` | `10` | Discovery items per digest, highest score first |
| `DISCOVERY_MIN_MATCH` | `0.25` | Pre-filter: weakest interest match worth scoring |
| `DISCOVERY_MIN_TEXT_CHARS` | `120` | Pre-filter: least text worth sending to an LLM |

`--provider`, `--model` and `--db` override the environment for one run.

## Tests

```bash
python test_discovery.py
```

117 tests, network fully stubbed — they never hit an LLM API, Telegram, or
Yahoo. The provider seam is the whole stub: a fake object with `complete_json`
and `search_json`.
