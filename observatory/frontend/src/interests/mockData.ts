/** Offer + edge fixtures for the mock client.
 *
 * Shapes match `discovery/offers.py` as shipped by PR H, field for field:
 * `evidence` rows are {date, quote, lang, depth, conversation_id},
 * `source_conversations` is the distinct ids across them, retirement offers
 * are namespaced `retire:<interest_key>` and carry their justification in
 * `score_terms` (the sweep's funnel snapshot) rather than in a column of their
 * own, and `exploratory` marks the run's one serendipity pick.
 *
 * These are hand-written rather than generated because the point of the inbox
 * is the PROVENANCE, and provenance is prose: real conversation titles from
 * the corpus, verbatim-sounding quotes, and a realistic Hebrew/English mix.
 * 28% of the owner's conversations are Hebrew-titled, so RTL quotes inside LTR
 * chrome are the normal case, not an edge case, and the fixtures make sure the
 * workspace is designed against that from the first render.
 *
 * The four live offers are the ones the design document's own mockup shows.
 * The two decided ones exist so the workspace can prove it never re-offers a
 * decision: `accepted` is a terminal state in PR H's TRANSITIONS table.
 */
import type { InterestEdge, Offer } from "./types";

/** One generation run's provenance, shared by every offer it produced. */
const ARTIFACT = "9c41f2b7a4e05c318d6f2a91be74c0d5e83b1f6a92c7d4e015a8b3c6f7d29e401";
const GENERATED_AT = "2026-08-17T19:12:04Z";

export const MOCK_OFFERS: Offer[] = [
  {
    id: 101,
    key: "handheld-and-roguelike-gaming",
    kind: "new",
    title: "Handheld and roguelike gaming",
    description:
      "Steam Deck ecosystem and roguelike/soulslike progression - patches, performance guides, notable mods and releases, and the run-design ideas behind them.",
    positive_signals: [
      "steam deck", "proton compatibility", "roguelike run design", "binding of isaac",
      "elden ring", "handheld performance", "frame pacing", "deck verified",
    ],
    negative_signals: ["console sales figures", "esports roster news", "store discount roundup"],
    suggested_min_score: 0.7,
    suggested_sources: ["web_search"],
    parent_key: null,
    related_keys: [],
    score: 0.86,
    score_terms: {
      evidence_strength: 0.92,
      recurrence: 1.0,
      recency: 0.55,
      novelty: 0.93,
      expected_yield: 0.85,
    },
    evidence: [
      {
        date: "2026-06-28",
        quote:
          "I keep dying on the same Isaac challenge - is there a run order that makes the unlocks less painful, or am I just supposed to grind it?",
        lang: "en",
        depth: 0.74,
        conversation_id: "c-8841",
        conversation_title: "Isaac Best Challenge Unlocks",
      },
      {
        date: "2026-05-19",
        quote:
          "יש דרך להוריד את צריכת הסוללה של הסטים דק בלי לפגוע בפריימים? ניסיתי לנעול 40 והוא עדיין מתחמם",
        lang: "he",
        depth: 0.68,
        conversation_id: "c-8620",
        conversation_title: "סוללה וביצועים בסטים דק",
      },
      {
        date: "2026-04-02",
        quote:
          "Does Elden Ring multiplayer actually work between PC and Deck, or does the seamless coop mod break on every patch?",
        lang: "en",
        depth: 0.81,
        conversation_id: "c-8203",
        conversation_title: "Elden Ring Multiplayer PC Deck",
      },
      {
        date: "2026-02-14",
        quote:
          "What actually makes a roguelike run feel different each time - item pool size, or the order the rooms get generated?",
        lang: "en",
        depth: 0.88,
        conversation_id: "c-7788",
        conversation_title: "Binding of Isaac Steam Deck",
      },
      {
        // No title: PR H's importer stores conversation_id only, so the UI has
        // to stay readable when that is all there is. Kept in the fixture on
        // purpose so the fallback path is always on screen.
        date: "2025-12-30",
        quote: "Hades vs Dead Cells for someone who bounces off long runs - which respects my time more?",
        lang: "en",
        depth: 0.59,
        conversation_id: "c-7410",
      },
    ],
    source_conversations: ["c-7410", "c-7788", "c-8203", "c-8620", "c-8841"],
    durability: { n_convs: 42, active_months: 7, span_days: 218, recency_days: 51 },
    similarity: [
      { key: "complex-systems-emergent-behavior", sim: 0.08 },
      { key: "learning-memory", sim: 0.05 },
    ],
    exploratory: false,
    status: "offered",
    snoozed_until: null,
    artifact_sha256: ARTIFACT,
    generated_at: GENERATED_AT,
    created_at: GENERATED_AT,
    decided_at: null,
    decided_note: "",
  },
  {
    id: 102,
    key: "cognition-in-competitive-games",
    kind: "bridge",
    title: "Cognition in competitive games",
    description:
      "Where cognitive-load-working-memory meets gaming: decision quality under time pressure, working-memory limits, and fatigue effects in competitive play.",
    positive_signals: [
      "decision quality under time pressure", "working memory load", "cognitive fatigue",
      "expertise chunking", "hearthstone battlegrounds", "draft heuristics",
    ],
    negative_signals: ["tier list", "patch notes without analysis"],
    suggested_min_score: 0.74,
    suggested_sources: ["web_search"],
    parent_key: null,
    related_keys: ["cognitive-load-working-memory", "handheld-and-roguelike-gaming"],
    score: 0.71,
    score_terms: {
      evidence_strength: 0.80,
      recurrence: 0.75,
      recency: 0.7,
      novelty: 0.72,
      expected_yield: 0.55,
    },
    evidence: [
      {
        date: "2026-07-11",
        quote:
          "In Battlegrounds I play noticeably worse from the third game onwards. Is that a working-memory thing or just tilt?",
        lang: "en",
        depth: 0.86,
        conversation_id: "c-8902",
        conversation_title: "Cognition and Hearthstone Battlegrounds",
      },
      {
        date: "2026-06-05",
        quote:
          "למה אני מרגיש שזיכרון העבודה שלי נחלש אחרי כמה משחקים ברצף? זה מרגיש דומה לתחושה אחרי יום לימודים ארוך",
        lang: "he",
        depth: 0.79,
        conversation_id: "c-8735",
        conversation_title: "זיכרון עבודה ועייפות קוגניטיבית",
      },
      {
        date: "2026-03-22",
        quote:
          "Is there research on whether the curve you learn in a card game transfers to other decision-under-uncertainty tasks?",
        lang: "en",
        depth: 0.72,
        conversation_id: "c-8055",
        conversation_title: "Hearthstone BG Meta Curve",
      },
    ],
    source_conversations: ["c-8055", "c-8735", "c-8902"],
    durability: { n_convs: 9, active_months: 4, span_days: 132, recency_days: 37 },
    similarity: [
      { key: "cognitive-load-working-memory", sim: 0.44 },
      { key: "learning-memory", sim: 0.29 },
    ],
    exploratory: true,
    status: "offered",
    snoozed_until: null,
    artifact_sha256: ARTIFACT,
    generated_at: GENERATED_AT,
    created_at: GENERATED_AT,
    decided_at: null,
    decided_note: "",
  },
  {
    id: 103,
    key: "performance-supplements-evidence",
    kind: "new",
    title: "Performance supplements - trial-grade evidence",
    description:
      "Trial-grade evidence on magnesium forms, creatine, tyrosine and caffeine timing - effects on energy, sleep quality and cognition, with dose-response where it exists.",
    positive_signals: [
      "randomised trial", "dose-response", "magnesium glycinate", "creatine cognition",
      "tyrosine depletion", "caffeine timing", "washout period",
    ],
    negative_signals: ["supplement stack listicle", "influencer protocol", "affiliate review"],
    suggested_min_score: 0.72,
    suggested_sources: ["web_search"],
    parent_key: null,
    related_keys: [],
    score: 0.64,
    score_terms: {
      evidence_strength: 0.71,
      recurrence: 0.83,
      recency: 0.42,
      novelty: 0.69,
      expected_yield: 0.52,
    },
    evidence: [
      {
        date: "2026-05-02",
        quote:
          "Magnesium malate in the morning vs glycinate at night - does the form actually change anything, or is it all the same elemental dose?",
        lang: "en",
        depth: 0.77,
        conversation_id: "c-8511",
        conversation_title: "Magnesium Malate Timing",
      },
      {
        date: "2026-01-18",
        quote:
          "If tyrosine only helps under acute stress or sleep deprivation, is there any point taking it on a normal day?",
        lang: "en",
        depth: 0.83,
        conversation_id: "c-7602",
        conversation_title: "Tyrosine vs Caffeine Effects",
      },
      {
        date: "2025-11-09",
        quote:
          "כמה זמן מיץ תפוזים נשמר במקרר אחרי שפותחים אותו, והאם ויטמין C באמת מתפרק?",
        lang: "he",
        depth: 0.41,
        conversation_id: "c-7301",
        conversation_title: "מיץ תפוזים אחרי פתיחה",
      },
      {
        date: "2025-08-24",
        quote: "Does creatine do anything measurable for cognition in people who already sleep badly?",
        lang: "en",
        depth: 0.69,
        conversation_id: "c-6980",
        conversation_title: "Creatine Cognition Sleep",
      },
    ],
    source_conversations: ["c-6980", "c-7301", "c-7602", "c-8511"],
    durability: { n_convs: 12, active_months: 6, span_days: 548, recency_days: 108 },
    similarity: [
      { key: "hypersomnia-offlabel-pharmacology", sim: 0.31 },
      { key: "wakefulness-drug-safety-interactions", sim: 0.24 },
    ],
    exploratory: false,
    status: "offered",
    snoozed_until: null,
    artifact_sha256: ARTIFACT,
    generated_at: GENERATED_AT,
    created_at: GENERATED_AT,
    decided_at: null,
    decided_note: "",
  },
  {
    // Raised by the decay sweep, not the generator: no artifact, no evidence,
    // no rank -- its justification is the funnel snapshot in score_terms.
    id: 104,
    key: "retire:speculative-fiction-ideas",
    kind: "retire",
    title: "Retire 'speculative-fiction-ideas'?",
    description:
      "speculative-fiction-ideas has gone 47 days without a single item above its bar (36 collected, 36 scored, 0 above bar all-time).",
    positive_signals: [],
    negative_signals: [],
    suggested_min_score: null,
    suggested_sources: ["web_search"],
    parent_key: null,
    related_keys: ["speculative-fiction-ideas"],
    score: null,
    score_terms: {
      interest_key: "speculative-fiction-ideas",
      silent_days: 47,
      collected: 36,
      scored: 36,
      above_bar: 0,
    },
    evidence: [],
    source_conversations: [],
    durability: {},
    similarity: [],
    exploratory: false,
    status: "offered",
    snoozed_until: null,
    artifact_sha256: "",
    generated_at: "",
    created_at: "2026-08-17T03:00:00Z",
    decided_at: null,
    decided_note: "",
  },
  {
    id: 99,
    key: "ai-agent-tooling-ecosystem",
    kind: "new",
    title: "AI agent tooling ecosystem",
    description: "Frameworks, harnesses and evaluation for tool-using agents.",
    positive_signals: ["agent harness", "tool use evaluation", "mcp servers"],
    negative_signals: ["funding round announcement"],
    suggested_min_score: 0.74,
    suggested_sources: ["web_search"],
    parent_key: null,
    related_keys: [],
    score: 0.58,
    score_terms: {
      evidence_strength: 0.55, recurrence: 0.5, recency: 0.80, novelty: 0.51, expected_yield: 0.6,
    },
    evidence: [
      {
        date: "2026-07-30",
        quote: "Which agent frameworks actually let me swap the model without rewriting the tool layer?",
        lang: "en",
        depth: 0.66,
        conversation_id: "c-8955",
        conversation_title: "Agent Framework Comparison",
      },
    ],
    source_conversations: ["c-8955"],
    durability: { n_convs: 6, active_months: 3, span_days: 74, recency_days: 19 },
    similarity: [{ key: "ai-tutoring-learning", sim: 0.36 }],
    exploratory: false,
    status: "snoozed",
    snoozed_until: "2026-09-16",
    artifact_sha256: ARTIFACT,
    generated_at: GENERATED_AT,
    created_at: GENERATED_AT,
    decided_at: null,
    decided_note: "interesting but too close to what I already track",
  },
  {
    id: 88,
    key: "consumer-gpu-price-tracking",
    kind: "new",
    title: "Consumer GPU price tracking",
    description: "Street prices and availability for consumer graphics cards.",
    positive_signals: ["gpu street price", "restock"],
    negative_signals: [],
    suggested_min_score: 0.7,
    suggested_sources: ["web_search"],
    parent_key: null,
    related_keys: [],
    score: 0.49,
    score_terms: {
      evidence_strength: 0.42, recurrence: 0.25, recency: 0.7, novelty: 0.65, expected_yield: 0.45,
    },
    evidence: [
      {
        date: "2026-04-19",
        quote: "Is a 5070 worth it over a used 4070 Super right now, or should I wait out the pricing?",
        lang: "en",
        depth: 0.52,
        conversation_id: "c-8330",
        conversation_title: "GPU Upgrade Timing",
      },
    ],
    source_conversations: ["c-8330"],
    durability: { n_convs: 4, active_months: 2, span_days: 61, recency_days: 121 },
    similarity: [{ key: "memory-hbm-semiconductors", sim: 0.22 }],
    exploratory: false,
    status: "rejected",
    snoozed_until: null,
    artifact_sha256: ARTIFACT,
    generated_at: GENERATED_AT,
    created_at: GENERATED_AT,
    decided_at: "2026-08-17T20:41:00Z",
    decided_note: "shopping, not research",
  },
];

/** interest_edges fixtures for the connections view.
 *
 * Weights are lift-normalised, never raw co-match counts: with 97% of items
 * matching two or more interests (mean 10.7), raw co-occurrence measures the
 * matcher's looseness rather than any relationship. The live DB's top raw pair
 * -- personal-knowledge-graphs / conversation-memory-compression at 578 shared
 * items -- is exactly that artefact, and appears here with a big shared_items
 * count next to a modest lift, which is the honest picture of it.
 */
export const MOCK_EDGES: InterestEdge[] = [
  {
    a: "personal-knowledge-graphs", b: "conversation-memory-compression",
    kind: "co_engagement", weight: 0.42,
    evidence: { lift: 1.9, shared_items: 578, note: "high overlap, mostly shared vocabulary" },
    computed_at: GENERATED_AT,
  },
  {
    a: "conversation-memory-compression", b: "recommender-personalization-interest-models",
    kind: "semantic", weight: 0.66, evidence: { lift: 2.8, shared_items: 214 },
    computed_at: GENERATED_AT,
  },
  {
    a: "minimal-knowledge-representations", b: "personal-knowledge-graphs",
    kind: "semantic", weight: 0.58, evidence: { lift: 2.4, shared_items: 143 },
    computed_at: GENERATED_AT,
  },
  {
    a: "nbis-nebius", b: "ai-neocloud-datacenter-economics",
    kind: "co_engagement", weight: 0.81,
    evidence: { lift: 4.6, shared_items: 302, note: "strongest measured pair" },
    computed_at: GENERATED_AT,
  },
  {
    a: "ai-neocloud-datacenter-economics", b: "memory-hbm-semiconductors",
    kind: "co_engagement", weight: 0.63, evidence: { lift: 2.6, shared_items: 188 },
    computed_at: GENERATED_AT,
  },
  {
    a: "memory-hbm-semiconductors", b: "optical-networking-cpo",
    kind: "semantic", weight: 0.47, evidence: { lift: 2.1, shared_items: 96 },
    computed_at: GENERATED_AT,
  },
  {
    a: "narcolepsy-eds", b: "orexin-hypocretin-agonists",
    kind: "co_engagement", weight: 0.88, evidence: { lift: 5.2, shared_items: 341 },
    computed_at: GENERATED_AT,
  },
  {
    a: "narcolepsy-eds", b: "hypersomnia-offlabel-pharmacology",
    kind: "co_engagement", weight: 0.79, evidence: { lift: 4.1, shared_items: 288 },
    computed_at: GENERATED_AT,
  },
  {
    a: "hypersomnia-offlabel-pharmacology", b: "wakefulness-drug-safety-interactions",
    kind: "semantic", weight: 0.61, evidence: { lift: 2.5, shared_items: 152 },
    computed_at: GENERATED_AT,
  },
  {
    a: "narcolepsy-eds", b: "sleep-diagnostics-biomarkers",
    kind: "semantic", weight: 0.55, evidence: { lift: 2.3, shared_items: 131 },
    computed_at: GENERATED_AT,
  },
  {
    a: "cognitive-disengagement-attention", b: "arousal-initiation-hypoactivity",
    kind: "co_engagement", weight: 0.72, evidence: { lift: 3.4, shared_items: 197 },
    computed_at: GENERATED_AT,
  },
  {
    a: "cognitive-load-working-memory", b: "learning-memory",
    kind: "semantic", weight: 0.64, evidence: { lift: 2.7, shared_items: 176 },
    computed_at: GENERATED_AT,
  },
  {
    a: "attraction-courtship", b: "interpersonal-microdynamics",
    kind: "co_engagement", weight: 0.7, evidence: { lift: 3.2, shared_items: 205 },
    computed_at: GENERATED_AT,
  },
  {
    a: "interpersonal-microdynamics", b: "mimicry-synchrony-suggestibility",
    kind: "semantic", weight: 0.59, evidence: { lift: 2.4, shared_items: 148 },
    computed_at: GENERATED_AT,
  },
  {
    a: "status-dominance-negotiation", b: "interpersonal-microdynamics",
    kind: "semantic", weight: 0.52, evidence: { lift: 2.2, shared_items: 119 },
    computed_at: GENERATED_AT,
  },
  {
    a: "social-work-clinical-training-israel", b: "behavioral-sciences-student-life-israel",
    kind: "co_engagement", weight: 0.68, evidence: { lift: 3.0, shared_items: 164 },
    computed_at: GENERATED_AT,
  },
  {
    a: "trauma-emdr-processing", b: "social-work-clinical-training-israel",
    kind: "semantic", weight: 0.5, evidence: { lift: 2.0, shared_items: 108 },
    computed_at: GENERATED_AT,
  },
  {
    a: "weird-science-cross-domain", b: "complex-systems-emergent-behavior",
    kind: "semantic", weight: 0.57, evidence: { lift: 2.3, shared_items: 127 },
    computed_at: GENERATED_AT,
  },
  {
    a: "ai-tutoring-learning", b: "learning-memory",
    kind: "semantic", weight: 0.53, evidence: { lift: 2.1, shared_items: 114 },
    computed_at: GENERATED_AT,
  },
  {
    a: "physical-ai-robotics", b: "asymmetric-tech-supply-chains",
    kind: "co_engagement", weight: 0.45, evidence: { lift: 1.9, shared_items: 87 },
    computed_at: GENERATED_AT,
  },
  {
    // The bridge offer's own edge, recorded against both parents. This is
    // where "lift 3.1 between parents" in the inbox comes from -- the offer
    // row itself carries no lift column, so the inbox reads it from here.
    a: "cognitive-load-working-memory", b: "handheld-and-roguelike-gaming",
    kind: "bridge_offer", weight: 0.71,
    evidence: {
      lift: 3.1,
      note: "proposed by offer cognition-in-competitive-games",
      quotes: [
        {
          date: "2026-07-11",
          quote: "In Battlegrounds I play noticeably worse from the third game onwards.",
          lang: "en",
          depth: 0.86,
          conversation_id: "c-8902",
          conversation_title: "Cognition and Hearthstone Battlegrounds",
        },
      ],
    },
    computed_at: GENERATED_AT,
  },
];
