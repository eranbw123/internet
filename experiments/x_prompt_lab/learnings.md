# Lab notebook — X discovery prompt lab (2026-08-08)

## stocks_nbis — generation 1 (runs: strategist+4 search+judge, budget 6)

Strategist (unseeded) invented 4 angles: financial-media embed harvesting,
named squawk/fintwit account sweep, primary-source IR→tweet chain,
comparable-neocloud spillover.

Numbers: 9 items, ALL 9 valid status URLs (link_rate 1.0 on productive
angles), 7 unique, 2 with agreement=2. Ages: mostly 2–3 days, one 9–10d.
Two angles returned honest `[]` — no hallucinated fallback, the output
contract's "never invent URLs, return [] instead" held.

Learnings:
1. **The method works at the mechanical level.** Web search does expose real
   tweet-status URLs for fintwit content, mostly via embed/quote surfaces.
   Zero fabricated-looking links; zero profile/article misclassifications.
2. **Embed harvesting >> primary-source hunting.** The angle aimed at
   company-IR tweets found nothing; the angle aimed at *where tweets get
   embedded* (news articles, aggregators) found 6/6. Where tweets are
   re-published matters more than who wrote them.
3. **Agreement ≠ veracity — judge caught it.** The 2x-agreement tweets were
   a Burry-short rumor and its denial: cross-angle agreement measured
   virality. Keep agreement as an *importance* signal, stop treating it as
   realness-only.
4. **Freshness floor so far: ~2 days.** No same-day tweets in gen 1. To test
   in gen 2 with explicit 48h pressure.
5. Honest-empty is cheap but still costs a search call — strategist should
   get feedback to kill/replace barren angle types.

Gen-2 guidance for stocks: keep embed-harvest + squawk sweep (refine for
freshness: demand last-48h, ask for the exact embed surfaces that worked);
replace the two barren angles; per judge: weight scheduled events (Aug 12
Q2 print), analyst actions, GPU/datacenter contract news; pair rumor-type
claims with rebuttal search.

## ai_news — generation 1 (budget after: 12/40)

4 angles, three productive (8+8+8 items, all valid status URLs), one thin
(nitter/mirror sweep: 2). 20 unique, 5 with agreement>1. Ages: nearly all
**1 day** — mainstream AI news is materially fresher in the index than
fintwit (2–3d). Top of ranking = primary announcements from @openai, @sama,
@gdb (agreement 2–3x), plus @JeffDean, @lisasu, @skhynix. This IS the "main
news personalised" product shape.

Learnings:
6. **Freshness is topic-dependent**: AI news D-1 achievable; fintwit D-2.
   Same-day (D-0) not yet observed anywhere.
7. **Single-story dominance is the failure mode of good angles**: one
   dramatic story (OpenAI Astra) consumed ~1/3 of the batch via near-
   duplicate takes. Judge prescribes per-story caps + sub-beat sweeps
   (chips/M&A/capex, personnel moves) + de-prioritizing paraphrase accounts.
8. **Nitter/mirror angle underperforms embed/aggregator angles** (2 vs 8
   items) — consistent with public mirrors being mostly dead; the live
   surfaces are news-article embeds and aggregators.
9. Agreement clusters (2–3x) land precisely on the objectively-top story —
   as an importance signal it works; realness still unverified by design.

## stocks_nbis — generation 2 (budget after: 18/40)

Guided (freshness pressure, barren angles replaced, rumor-rebuttal pairing).
Result: THINNER than gen 1 — 7 items, 5 unique, ages 2–7d; 48h pressure did
not produce fresher tweets; new "contract/capex read-through" angle: honest [].

10. **Cross-generation id overlap is the realness jackpot.** 4 of gen-2's 5
    tweets are exact re-finds of gen-1 status ids (same 19-digit id, same
    author, same text) from independent conversations. Hallucinations cannot
    collide on 19-digit ids → the overlapping tweets are REAL. Re-running a
    prompt suite is a zero-network verifier.
11. **Narrow-ticker well is shallow, refinement can't fill it.** One week of
    NBIS has ~6–8 distinct indexed tweets, and the loop mostly re-harvests
    them. Production: for narrow interests the marginal-new yield per cycle
    is low → daily cadence, dedup does the rest; don't expect same-day.
12. Freshness pressure in prompts does NOT beat the index: fintwit floor
    stays ~D-2 regardless of instructions. Freshness is an index property,
    not a prompt property.

## Checkpoint (18/40 spent + 6 in flight)
Branches all alive; no dead spend. Remaining plan: ai_news gen 2 (in
flight), then lean stocks gen 3 (2 angles — production-shape cost test +
second overlap measurement), full ai_news gen 3. Projected total ≈ 34/40.

## ai_news — generation 2 (budget after: 24/40)

Guided. 24 items (all valid links again), 17 unique, 5 agreement>1, ages
still ~D-1. Sub-beat sweeps: personnel/policy/safety worked (8/8); chips/
capex/M&A nearly empty (1) — that beat's tweets apparently aren't indexed,
mirroring the stocks contract/capex angle failing twice. Pattern: **corporate
capex/deal news lives in articles, not indexable tweets.**

13. **Broad-topic wells refill daily**: 9/17 re-finds (realness confirmed for
    those), 8 genuinely new (Hassabis GDM shuffle, Meta release, GPT-5.6,
    AMD/Taalas close). Production cadence for broad interests: daily+.
14. One overlapping id had mismatched authors across generations →
    attribution is claimed-not-verified even when the id is real. Treat
    author as soft metadata downstream.
15. Judge's recurring ask across both interests: primary announcement + max
    one follow-up per story — a per-story cap belongs in the production
    output contract, not in post-processing.

## stocks_nbis — generation 3, lean production-shape test (budget after: 28/40)

2 angles only (merged best performers). 5 items, 3 unique, ages 2–3d,
**3/3 re-finds, 0 new** vs gens 1–2.

16. **Lean config validated**: 2 search calls re-captured the core stories
    that 4 calls found — for narrow interests, production needs only
    1 strategist + 2 searches (+1 judge) per cycle.
17. **Saturation is measurable**: new-tweet yield went 7 → 1 → 0 across
    generations on a shallow well. In production, the seen-dedup rate IS the
    saturation meter; a cycle returning ~all re-finds can auto-extend its
    interval (backoff like the existing rescore backoff).
18. Judge verdicts have converged/looped for stocks (same asks each round:
    primary sourcing, claim+correction pairing) — the remaining gap is index
    content, not prompt craft. Diminishing returns past ~2 generations.

## Gen-2 guidance issued
stocks: keep embed-harvest+squawk (48h pressure, name working surfaces),
replace barren angles, weight scheduled events/analyst actions/contracts,
rumor→rebuttal pairing, cap dupes at 2.
ai_news (planned): per-story cap 2, drop paraphrase accounts in favor of
primary tweets, dedicated chips/M&A/capex + personnel sweeps, 72h window,
keep tech-press embed + aggregator backtrace angles.
