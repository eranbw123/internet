"""The core entities, as plain dataclasses, plus the ranking formula.

Only what actually flows between pipeline stages gets a class; notifications
and feedback are written and read straight through db.py.
"""
from dataclasses import dataclass, field

# What the LLM scores, all 0-1. `specificity` is scored and stored but is
# deliberately NOT in the weighted sum -- it is kept separate so the ranking
# formula can start using it (or any other dimension) without a re-score.
WEIGHTS = {
    "personal_relevance": 0.35,
    "novelty": 0.20,
    "depth": 0.15,
    "importance": 0.15,
    "surprise": 0.15,
}
DIMENSIONS = tuple(WEIGHTS) + ("specificity",)


def final_score(dimensions):
    """Weighted 0-1 score. Missing dimensions count as 0, out-of-range clamps."""
    total = sum(w * clamp01(dimensions.get(name, 0.0)) for name, w in WEIGHTS.items())
    return round(total, 4)


def clamp01(value):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Interest:
    key: str                      # stable slug, the identity across runs
    title: str
    description: str              # free text -- the scorer reads this verbatim
    positive_signals: list = field(default_factory=list)
    negative_signals: list = field(default_factory=list)
    min_score: float = 0.70       # threshold against final_score, 0-1
    sources: list = field(default_factory=list)
    source_config: dict = field(default_factory=dict)   # per-source knobs
    # Layered interest state (discovery/interest_state.py). Owner interests
    # (interests.json) are always layer="owner"; everything else is written
    # only by the guarded db.py helpers -- see OwnerInterestImmutable.
    layer: str = "owner"
    provenance: dict = field(default_factory=dict)      # JSON -- how this row came to be
    id: int = None


@dataclass
class CandidateItem:
    source: str                   # collector name: web_search | youtube | stocks
    type: str                     # article | video | price_move | ...
    title: str
    url: str
    text: str = ""                # body/transcript/summary the scorer reads
    author: str = None
    published_at: str = None      # ISO 8601
    metadata: dict = field(default_factory=dict)
    dedup_key: str = None         # defaults to the canonical url
    origin_interest: str = None   # interest key the collector was run for
    # Filled in by normalize.normalize(); dedup.py matches on these.
    url_hash: str = None
    title_hash: str = None
    content_hash: str = None
    id: int = None

    def key(self):
        return self.dedup_key or self.url


@dataclass
class ScoreResult:
    item_id: int
    interest_id: int              # the interest the scorer judged most relevant
    interest_key: str
    dimensions: dict              # all six, 0-1
    final_score: float
    confidence: float
    reason: str
    why_better_than_generic: str
    provider: str
    model: str
    prompt_hash: str = ""         # scoring.prompt_fingerprint(); "" on legacy rows
    id: int = None
