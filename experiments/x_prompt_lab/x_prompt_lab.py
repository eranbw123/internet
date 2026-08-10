"""X discovery prompt lab -- one generation per invocation.

Usage (from anywhere; the script pins itself to the internet repo):
    python x_prompt_lab.py <interest_key>            # run next generation
    python x_prompt_lab.py <interest_key> --show     # print state summary

Per generation: 1 strategist complete_json + <=4 search_json-style calls +
1 council-judge complete_json. All state in state.json, every provider call
appended to runs.jsonl (full prompt + raw response). No repo writes, no
discovery.db access.
"""
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(r"C:\github\internet")
LAB = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from discovery.config import load as load_cfg              # noqa: E402
from discovery.providers import get_provider               # noqa: E402
from discovery.providers.base import ProviderError, parse_json_array  # noqa: E402
from discovery.providers.claude_chat import WEB_SEARCH_TOOLS          # noqa: E402

STATE_PATH = LAB / "state.json"
RUNS_PATH = LAB / "runs.jsonl"

BUDGET_CAP = 40
MAX_ANGLES = 4
MAX_TWEETS_PER_ANGLE = 8

TODAY = date.today().isoformat()

TWEET_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)",
    re.I,
)
PROFILE_RE = re.compile(
    r"https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/[A-Za-z0-9_]{1,15}/?$", re.I
)

INTERESTS = {
    "stocks_nbis": {
        "title": "Nebius (NBIS) stock news",
        "description": (
            "Market-moving news about Nebius Group (ticker NBIS): earnings and "
            "guidance, AI-datacenter / GPU-infrastructure deals and contracts, "
            "analyst upgrades/downgrades and price targets, large price moves and "
            "their catalysts, major-holder or insider activity. Closely related "
            "AI-infrastructure names (CoreWeave, Oracle, Microsoft capex) count "
            "when the news plausibly moves NBIS."
        ),
        "positive": "breaking headlines, primary-source announcements, squawk-style market headlines, analyst notes",
        "negative": "generic AI hype, week-old news, pump/promo content, technical-analysis chart posts",
    },
    "ai_news": {
        "title": "AI industry main news",
        "description": (
            "The day's most important AI-industry news as discussed on X: major "
            "model releases and benchmarks, announcements and statements from the "
            "big labs and their leaders (OpenAI, Anthropic, Google DeepMind, Meta, "
            "xAI, Scale, etc.), big funding rounds and M&A, chips/compute supply, "
            "and consequential AI policy. The mainstream top stories, personalised "
            "in topic only -- famous accounts are fine and expected."
        ),
        "positive": "primary announcements from key figures and orgs, the tweets everyone is quoting today",
        "negative": "tutorials, listicles, engagement-bait threads, old recycled takes",
    },
}

OUTPUT_CONTRACT = """

Return AT MOST {max_tweets} tweets as a JSON array, one object per tweet:
[{{"url": "<the tweet's own URL: https://x.com/<handle>/status/<digits>>",
   "author": "@handle",
   "text": "<the tweet's text, as close to verbatim as what you actually saw>",
   "date": "<tweet date YYYY-MM-DD, as claimed by the page you saw it on>",
   "why": "<one line: why this is main news for this interest>",
   "seen_at": "<URL of the page where you saw this tweet or its content>"}}]
Only include tweets whose status URL you actually saw in a search result or
cited page -- NEVER invent or guess URLs or numeric ids. If you found the
story but not the tweet's own URL, leave that story out. Return [] if nothing
solid turned up. Output only the JSON array, no other text."""

STRATEGIST_SYSTEM = (
    "You design web-search instructions that make a search-capable LLM find "
    "real, current, high-value tweets. You answer in strict JSON only."
)

STRATEGIST_PROMPT = """\
Today is {today}.

We are building an X/Twitter discovery source for a personalized news feed.
Target interest:
{title}
{description}
Worth surfacing: {positive}
Not worth surfacing: {negative}

A separate executor LLM with iterative web search will run each prompt you
write. It must come back with REAL tweets -- actual
https://x.com/<handle>/status/<digits> URLs -- that are the MAIN NEWS for this
interest, as fresh as possible. X itself is mostly closed to crawlers, so the
executor only sees tweets indirectly through whatever the web-search index
exposes; reason about which surfaces of the web actually expose tweet URLs
and tweet content, and aim your prompts there.

Design {k} search prompts, each attacking the problem from a genuinely
different angle. Be creative and SPECIFIC TO THIS INTEREST -- name real
accounts, tickers, people, sites, communities where useful. For each angle
give a short label, a one-line rationale, and the full prompt text the
executor will receive. Do not include output-format instructions; the
executor gets a fixed output contract appended automatically.
{history_block}
Output ONLY this JSON object:
{{"angles": [{{"label": "...", "rationale": "...", "prompt": "..."}}]}}"""

HISTORY_TEMPLATE = """
Previous generations of this lab and their measured results
(link_rate = fraction of returned items whose URL was a real-looking
x.com/.../status/<id> link; ages in days from claimed tweet dates; judge
scores are 0-1; "agreement" = how many independent angles surfaced the same
tweet id, our strongest realness/importance signal):

{history_json}

Orchestrator guidance for this generation: {guidance}

Keep what worked, refine or replace what failed. Reuse an angle label when
you are refining that same angle."""

JUDGE_SYSTEM = (
    "You run an internal council deliberation to judge candidate tweets for a "
    "personalized news feed. All deliberation happens internally in your "
    "reasoning; your visible output is ONLY the final JSON object."
)

JUDGE_PROMPT = """\
Today is {today}. Interest:
{title}: {description}

Candidate tweets, deduplicated across several independent search prompts
("agreement" = how many of those prompts independently surfaced this tweet):

{tweets_json}

Run a three-stage council internally, in the style of an LLM Council:

STAGE 1 -- three judges score every tweet independently, each giving 0-1 for
relevance (does this matter for THIS interest as described) and 0-1 for
importance (is this the main news on the topic right now, not a side remark):
- The News Editor: would this lead today's briefing on the topic?
- The Skeptic: punish stale items, engagement bait, vague paraphrases that
  smell invented, and text that reads like an article summary rather than an
  actual tweet.
- The Owner's Proxy: read the interest description literally; judge personal
  usefulness to its owner.

STAGE 2 -- anonymize the three scorings as A/B/C; each judge reviews the
others' scores and flags disagreements.

STAGE 3 -- as Chairman, settle disagreements and produce consolidated scores.

Output ONLY this JSON object (indices refer to the candidate list above):
{{"tweets": [{{"i": <index>, "relevance": <0-1>, "importance": <0-1>,
   "note": "<one short line>"}}],
 "verdict": "<2-3 sentences: what this batch says about how next
   generation's search prompts should change>"}}"""

JUDGE_SCHEMA = {
    "type": "object",
    "required": ["tweets", "verdict"],
    "properties": {"tweets": {"type": "array"}, "verdict": {"type": "string"}},
}
STRATEGIST_SCHEMA = {
    "type": "object",
    "required": ["angles"],
    "properties": {"angles": {"type": "array"}},
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"budget_used": 0, "interests": {}}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


def log_run(record):
    with RUNS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def spend(state, n=1):
    state["budget_used"] += n
    if state["budget_used"] > BUDGET_CAP:
        save_state(state)
        raise SystemExit(f"BUDGET CAP {BUDGET_CAP} exceeded ({state['budget_used']})")


def classify(item):
    """(kind, status_id or None). Kinds: tweet|profile_link|article_link|no_url."""
    url = str(item.get("url") or "").strip()
    if not url:
        return "no_url", None
    m = TWEET_RE.search(url)
    if m:
        return "tweet", m.group(2)
    if PROFILE_RE.match(url):
        return "profile_link", None
    return "article_link", None


def age_days(claimed):
    try:
        d = date.fromisoformat(str(claimed)[:10])
        return (date.today() - d).days
    except ValueError:
        return None


def call(provider, state, call_type, interest_key, gen, label, fn):
    """One budgeted provider call with one retry; logs prompt+raw via fn's record."""
    for attempt in (1, 2):
        spend(state, 1)
        try:
            return fn()
        except ProviderError as e:
            log_run({
                "ts": now(), "call": call_type, "interest": interest_key,
                "gen": gen, "angle": label, "attempt": attempt, "error": str(e),
            })
            if attempt == 2:
                return None
            time.sleep(5)


def run_generation(interest_key, guidance):
    interest = INTERESTS[interest_key]
    state = load_state()
    hist = state["interests"].setdefault(interest_key, {"generations": []})
    gen = len(hist["generations"]) + 1
    print(f"=== {interest_key} generation {gen} (budget used: {state['budget_used']}/{BUDGET_CAP}) ===")

    cfg = load_cfg()
    provider = get_provider(cfg)

    # --- strategist ---------------------------------------------------------
    history_block = ""
    if hist["generations"]:
        compact = []
        for g in hist["generations"]:
            compact.append({
                "generation": g["gen"],
                "angles": [
                    {k: a[k] for k in ("label", "prompt", "n_items", "n_tweets",
                                       "link_rate", "failures", "ages_days",
                                       "judge_relevance_mean", "judge_importance_mean")
                     if k in a}
                    for a in g["scorecard"]
                ],
                "judge_verdict": g.get("verdict", ""),
                "best_tweets": [
                    {"author": t.get("author"), "text": (t.get("text") or "")[:150],
                     "date": t.get("date"), "agreement": t.get("agreement"),
                     "relevance": t.get("relevance"), "importance": t.get("importance")}
                    for t in sorted(g.get("tweets", []),
                                    key=lambda t: -(t.get("importance") or 0))[:5]
                ],
            })
        history_block = HISTORY_TEMPLATE.format(
            history_json=json.dumps(compact, ensure_ascii=False, indent=1),
            guidance=guidance or "(none)",
        )

    sprompt = STRATEGIST_PROMPT.format(
        today=TODAY, k=MAX_ANGLES, history_block=history_block, **interest
    )
    result = call(provider, state, "strategist", interest_key, gen, None,
                  lambda: provider.complete_json(STRATEGIST_SYSTEM, sprompt, STRATEGIST_SCHEMA))
    if result is None:
        save_state(state)
        raise SystemExit("strategist failed twice; stopping")
    angles = [a for a in result.get("angles", [])
              if isinstance(a, dict) and a.get("prompt") and a.get("label")][:MAX_ANGLES]
    log_run({"ts": now(), "call": "strategist", "interest": interest_key, "gen": gen,
             "prompt": sprompt, "raw": result})
    print(f"strategist proposed {len(angles)} angles: {[a['label'] for a in angles]}")

    # --- execute angles -----------------------------------------------------
    scorecard, by_id = [], {}
    for angle in angles:
        label = angle["label"]
        full = angle["prompt"] + OUTPUT_CONTRACT.format(max_tweets=MAX_TWEETS_PER_ANGLE) \
            + "\n\nUse web search (at most 5 searches)."

        def one_search(p=full):
            raw = provider._completion(p, tools=WEB_SEARCH_TOOLS, timeout=420)
            return raw

        raw = call(provider, state, "search", interest_key, gen, label, one_search)
        if raw is None:
            scorecard.append({"label": label, "prompt": angle["prompt"],
                              "rationale": angle.get("rationale", ""), "error": "failed twice"})
            continue
        items = parse_json_array(raw)
        log_run({"ts": now(), "call": "search", "interest": interest_key, "gen": gen,
                 "angle": label, "prompt": full, "raw": raw, "n_items": len(items)})

        failures = {"profile_link": 0, "article_link": 0, "no_url": 0}
        tweets, ages = [], []
        for it in items:
            if not isinstance(it, dict):
                continue
            kind, sid = classify(it)
            if kind == "tweet":
                a = age_days(it.get("date"))
                if a is not None:
                    ages.append(a)
                tweets.append((sid, it))
            else:
                failures[kind] += 1
        parse_note = None
        if not items:
            parse_note = "empty_or_unparseable"
            snippet = raw.strip()[:200]
            log_run({"ts": now(), "call": "search_empty_note", "interest": interest_key,
                     "gen": gen, "angle": label, "raw_head": snippet})
        for sid, it in tweets:
            entry = by_id.setdefault(sid, {
                "id": sid, "url": it.get("url"), "author": it.get("author"),
                "text": it.get("text"), "date": it.get("date"),
                "why": it.get("why"), "seen_at": it.get("seen_at"), "angles": [],
            })
            if label not in entry["angles"]:
                entry["angles"].append(label)
        scorecard.append({
            "label": label, "prompt": angle["prompt"],
            "rationale": angle.get("rationale", ""),
            "n_items": len(items), "n_tweets": len(tweets),
            "link_rate": round(len(tweets) / len(items), 2) if items else 0.0,
            "failures": failures, "ages_days": sorted(ages), "note": parse_note,
        })
        print(f"  [{label}] items={len(items)} tweets={len(tweets)} "
              f"failures={failures} ages={sorted(ages)}")

    unique = list(by_id.values())
    for t in unique:
        t["agreement"] = len(t["angles"])
    print(f"unique tweets this generation: {len(unique)} "
          f"(agreement>1: {sum(1 for t in unique if t['agreement'] > 1)})")

    # --- council judge ------------------------------------------------------
    verdict = ""
    if unique:
        listing = [
            {"i": i, "author": t.get("author"), "text": t.get("text"),
             "date": t.get("date"), "agreement": t["agreement"]}
            for i, t in enumerate(unique)
        ]
        jprompt = JUDGE_PROMPT.format(
            today=TODAY, title=interest["title"], description=interest["description"],
            tweets_json=json.dumps(listing, ensure_ascii=False, indent=1),
        )
        judged = call(provider, state, "judge", interest_key, gen, None,
                      lambda: provider.complete_json(JUDGE_SYSTEM, jprompt, JUDGE_SCHEMA))
        if judged:
            log_run({"ts": now(), "call": "judge", "interest": interest_key, "gen": gen,
                     "prompt": jprompt, "raw": judged})
            verdict = str(judged.get("verdict", ""))
            for row in judged.get("tweets", []):
                if isinstance(row, dict) and isinstance(row.get("i"), int) and 0 <= row["i"] < len(unique):
                    unique[row["i"]]["relevance"] = row.get("relevance")
                    unique[row["i"]]["importance"] = row.get("importance")
                    unique[row["i"]]["note"] = row.get("note")
    # per-angle judge means
    for a in scorecard:
        scored = [t for t in unique
                  if a["label"] in t["angles"] and isinstance(t.get("relevance"), (int, float))]
        if scored:
            a["judge_relevance_mean"] = round(sum(t["relevance"] for t in scored) / len(scored), 2)
            a["judge_importance_mean"] = round(sum(t["importance"] for t in scored) / len(scored), 2)

    hist["generations"].append({
        "gen": gen, "ts": now(), "guidance": guidance,
        "scorecard": scorecard, "tweets": unique, "verdict": verdict,
    })
    save_state(state)
    print(f"--- verdict: {verdict}")
    print(f"budget used: {state['budget_used']}/{BUDGET_CAP}")


def show(interest_key):
    state = load_state()
    print(json.dumps(state["interests"].get(interest_key, {}), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 2 or sys.argv[1] not in list(INTERESTS) + ["budget"]:
        raise SystemExit(f"usage: x_prompt_lab.py {{{'|'.join(INTERESTS)}}} [--show] [--guidance TEXT]")
    if sys.argv[1] == "budget":
        print(load_state()["budget_used"])
    elif "--show" in sys.argv:
        show(sys.argv[1])
    else:
        guidance = ""
        if "--guidance" in sys.argv:
            guidance = sys.argv[sys.argv.index("--guidance") + 1]
        if "--max-angles" in sys.argv:
            MAX_ANGLES = int(sys.argv[sys.argv.index("--max-angles") + 1])
        run_generation(sys.argv[1], guidance)
