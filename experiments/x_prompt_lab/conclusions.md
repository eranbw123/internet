# X discovery prompt lab — conclusions (2026-08-08)

Lab: 2 interests × 3 generations, 34 provider calls (cap 40), all via the
repo's default `claude_chat` provider (claude.ai web search, zero marginal
cost). Full data: `state.json` (every generation, tweet, score),
`runs.jsonl` (every prompt + raw response), `learnings.md` (notebook).

## 1. Verdict

**The method works.** Prompting alone — no scraping infrastructure, no API,
no X account — reliably produces real, on-topic, judge-ranked top tweets:

- **91/91 returned items across all 6 generations were valid
  `x.com/<handle>/status/<id>` URLs (100% link rate).** Zero profile links,
  zero article links, zero malformed URLs.
- **Zero observed hallucination.** The anti-invention output contract held:
  angles that found nothing returned `[]` (6 honest-empty runs) instead of
  fabricating. 15 distinct ids were independently re-found in separate
  conversations (identical 19-digit id + author + text) — hallucinations
  cannot collide on 19-digit ids, so re-finds are proof of realness.
- **Quality is the product shape wanted**: mainstream main news, personalised
  by topic. ai_news top-ranked = @openai/@sama/@gdb on the Astra
  designation (agreement 2–3×), @JeffDean's Discovery Loop, @lisasu's
  Taalas acquisition, @skhynix capex. stocks = Citi PT cut, Burry
  short rumor + denial, Q2 earnings date, Piper Sandler initiation.

## 2. Scorecard

| interest | gen | items | link_rate | unique | re-finds | agree>1 | age med/min | judge rel/imp |
|---|---|---|---|---|---|---|---|---|
| stocks_nbis | 1 | 9 | 1.00 | 7 | 0 | 2 | 2 / 2 | .80 / .55 |
| stocks_nbis | 2 | 7 | 1.00 | 5 | 4 | 2 | 3 / 2 | .98 / .62 |
| stocks_nbis | 3 (lean, 2 angles) | 5 | 1.00 | 3 | 3 | 2 | 2 / 2 | 1.0 / .68 |
| ai_news | 1 | 26 | 1.00 | 20 | 0 | 5 | 1 / 1 | .83 / .61 |
| ai_news | 2 | 24 | 1.00 | 17 | 9 | 5 | 1 / 1 | .85 / .59 |
| ai_news | 3 | 20 | 1.00 | 12 | 9 | 6 | 1 / **0** | .82 / .62 |

Realness-confirmed ids (seen 2+ times independently): stocks 4/8, ai 11/31.

## 3. Freshness finding → ALERT vs digest

Freshness is an **index property, not a prompt property** — explicit 24/48h
pressure did not beat the floor:
- Broad mainstream topics (AI news): median **D-1**, one D-0 by gen 3.
- Narrow ticker (fintwit): floor **D-2**, never better.

**Implication: the immediate-ALERT use case is NOT honest via this method.**
This is a daily-digest-grade source. (For true real-time squawk, the earlier
transport designs — third-party API or Telegram mirrors — remain the path.)

## 4. What works / what doesn't (the recipe)

Angle types by measured yield:
1. **Embed harvesting** (articles that embed tweets with canonical URLs:
   Techmeme-linked tech press; Benzinga/Investing.com-style finance press) —
   best performer everywhere, 6–8/8 valid tweets per run.
2. **Aggregator backtrace** (HN/Reddit/StockTwits threads submitting raw
   tweet URLs) — equal best on broad topics, first D-0 tweet.
3. **Dedicated sub-beat sweeps** (policy, personnel) — work on broad topics
   (6/6) once told other angles cover the headline beat.
4. **Named-account sweeps** — moderate (2–4), good precision.
5. **Nitter/mirror sweeps** — weak (2): public mirrors mostly dead.
6. **Primary-source IR / corporate capex / funding-deal hunts** — 0 items in
   5 attempts across both interests. Deal/capex/IR news lives in articles,
   not indexed tweets. Don't spend angles there.

Prompt elements that mattered (in the executor output contract):
- "NEVER invent or guess URLs or numeric ids; if you found the story but not
  the tweet's own URL, leave it out; return [] if nothing solid" — this is
  what produced 100%/0-hallucination. Keep verbatim.
- Explicit dates ("today is YYYY-MM-DD; prefer 08-07/08-08") beat relative
  phrasing ("recent").
- Per-story cap ("originating tweet + at most one substantive follow-up")
  fixes single-story dominance, the main quality failure mode.
- Naming concrete surfaces AND concrete accounts (the strategist does this
  per-interest — its whole value).

Well depth drives cadence: narrow ticker ≈ 6–8 distinct tweets/week,
saturates in 2 runs (gen-3 new yield = 0); broad topic refills ~8 new/day.

## 5. Production collector implications (`discovery/collectors/x.py`)

- Flow per interest per cycle: 1 strategist `complete_json` (cached per
  interest, regenerate ~weekly or on feedback, NOT per cycle) → 2–4 angle
  `search_json` calls → regex gate + classify → merge on status id →
  agreement count in metadata → emit. The council judge is NOT needed in
  production — scoring.py already judges; the lab judge existed to steer
  iteration.
- `CandidateItem`: `source="x"`, `type="tweet"`, `dedup_key=<status id>`,
  url canonical `https://x.com/<handle>/status/<id>`, text=tweet text,
  metadata: agreement, seen_at, angle label, claimed date. Author is
  claimed-not-verified (one cross-gen attribution mismatch observed).
- Pipeline touches (from the earlier transport design, unchanged):
  register in `collectors/__init__.py`; add `"x"` to
  `matching.SHORT_FORM_SOURCES` (else the 120-char pre-filter kills every
  tweet); interval config. NO ALERT wiring — digest only (see §3).
- Seen-dedup doubles as the saturation meter: a cycle with ~100% re-finds
  can back off its interval (narrow interests), mirroring the existing
  rescore-backoff pattern.
- Cost per cycle at lean shape: 2–3 provider calls per interest — within
  the existing call-budget discipline. No new secrets, no new dependencies.

## 6. Portability

- **youtube**: its Stage-1 discovery prompt is a hand-written, static
  version of exactly this. Porting = swap the static PROMPT for a cached
  strategist output + add the id-agreement signal across angles. The
  embed/aggregator insight transfers (videos are found via pages that embed
  them).
- **instagram**: expect much worse — IG content is barely indexed by web
  search and has no embed-with-canonical-URL culture comparable to tweets.
  Test with the same lab harness (it is interest- and platform-agnostic:
  swap the output contract's URL regex).

## 7. Threats to validity

- Two-day sample, one news cycle; a quiet week may look different.
- Realness confirmed only for the 15 re-found ids; the rest are
  format-valid and plausible but unverified BY DESIGN (user's call — regex
  gate only). The 100% author+text match on all re-finds is strong but
  indirect evidence for the rest.
- Judge scores come from the same model family that searched (shared bias).
- claude.ai endpoints remain undocumented/ToS-gray, same caveat as the
  provider itself carries.
